import fs from 'node:fs/promises'
import path from 'node:path'
import { chromium } from 'playwright'

const readEnv = (key, fallback = '') => String(process.env[key] ?? fallback).trim()

const TARGET_URL = readEnv('TARGET_URL')
const URL_INCLUDES = readEnv('URL_INCLUDES')
const METHOD = readEnv('METHOD').toUpperCase()
const OUTPUT_DIR = readEnv('OUTPUT_DIR', path.resolve(process.cwd(), 'data/api-response-captures'))
const OUTPUT_FILE = readEnv('OUTPUT_FILE')
const SCROLL_CONTAINER_SELECTOR = readEnv('SCROLL_CONTAINER_SELECTOR')
const HEADLESS = readEnv('HEADLESS', 'false') === 'true'
const INITIAL_WAIT_MS = Math.max(0, Number(readEnv('INITIAL_WAIT_MS', '5000')) || 5000)
const AFTER_SCROLL_WAIT_MS = Math.max(0, Number(readEnv('AFTER_SCROLL_WAIT_MS', '1200')) || 1200)
const SCROLL_STEP_PX = Math.max(50, Number(readEnv('SCROLL_STEP_PX', '1200')) || 1200)
const MAX_SCROLL_ROUNDS = Math.max(0, Number(readEnv('MAX_SCROLL_ROUNDS', '0')) || 0)
const MAX_IDLE_ROUNDS = Math.max(1, Number(readEnv('MAX_IDLE_ROUNDS', '3')) || 3)

if (!TARGET_URL) {
  console.error('Missing TARGET_URL')
  process.exit(1)
}

if (!URL_INCLUDES) {
  console.error('Missing URL_INCLUDES')
  process.exit(1)
}

const sleep = async (ms) => {
  await new Promise((resolve) => setTimeout(resolve, ms))
}

const readHasMoreFlag = (payload) => {
  const candidatePaths = ['data.has_more', 'pagination.has_more', 'next_pagination.has_more', 'has_more']
  for (const candidatePath of candidatePaths) {
    const value = candidatePath.split('.').reduce((current, segment) => {
      if (current === null || current === undefined) return undefined
      if (typeof current !== 'object') return undefined
      return current[segment]
    }, payload)
    if (typeof value === 'boolean') {
      return value
    }
  }
  return undefined
}

const scrollOnce = async (page) => {
  return page.evaluate(
    ({ selector, stepPx }) => {
      const target =
        selector && document.querySelector(selector) instanceof HTMLElement
          ? document.querySelector(selector)
          : document.scrollingElement || document.documentElement
      if (!(target instanceof HTMLElement)) {
        return false
      }

      const beforeTop = target.scrollTop
      const maxTop = Math.max(0, target.scrollHeight - target.clientHeight)
      const nextTop = Math.min(maxTop, beforeTop + stepPx)
      target.scrollTo({ top: nextTop, behavior: 'auto' })
      return nextTop > beforeTop
    },
    {
      selector: SCROLL_CONTAINER_SELECTOR || '',
      stepPx: SCROLL_STEP_PX
    }
  )
}

const main = async () => {
  const browser = await chromium.launch({ headless: HEADLESS })
  const page = await browser.newPage()
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Network.enable')

  const responses = []
  const matchedRequestIds = new Set()
  const processedRequestIds = new Set()
  const requestUrlById = new Map()
  const requestMethodById = new Map()
  const pendingTasks = new Set()
  let reachedEndByApi = false

  cdp.on('Network.requestWillBeSent', (params) => {
    const requestId = String(params?.requestId || '')
    const request = params?.request || {}
    const url = String(request.url || '')
    const currentMethod = String(request.method || '').trim().toUpperCase()
    const matchedMethod = !METHOD || currentMethod === METHOD

    if (requestId && url.includes(URL_INCLUDES) && matchedMethod) {
      matchedRequestIds.add(requestId)
      requestUrlById.set(requestId, url)
      requestMethodById.set(requestId, currentMethod)
      console.log(`[MATCH] method=${currentMethod} url=${url}`)
    }
  })

  cdp.on('Network.responseReceived', (params) => {
    const requestId = String(params?.requestId || '')
    if (!requestId || !matchedRequestIds.has(requestId)) {
      return
    }

    const response = params?.response || {}
    const mimeType = String(response.mimeType || '')
    const resourceType = String(params?.type || '')
    const isJsonLike = resourceType === 'XHR' || resourceType === 'Fetch' || mimeType.includes('json')

    if (!isJsonLike) {
      matchedRequestIds.delete(requestId)
      requestUrlById.delete(requestId)
      requestMethodById.delete(requestId)
      console.log(
        `[SKIP] non-json response type=${resourceType || '(empty)'} mime=${mimeType || '(empty)'}`
      )
    }
  })

  cdp.on('Network.loadingFinished', (params) => {
    const requestId = String(params?.requestId || '')
    if (!requestId || !matchedRequestIds.has(requestId) || processedRequestIds.has(requestId)) {
      return
    }

    processedRequestIds.add(requestId)
    const task = (async () => {
      try {
        const responseBody = await cdp.send('Network.getResponseBody', { requestId })
        const rawBody = responseBody.base64Encoded
          ? Buffer.from(String(responseBody.body || ''), 'base64').toString('utf8')
          : String(responseBody.body || '')

        if (!rawBody) return

        const parsedBody = JSON.parse(rawBody)
        const hasMore = readHasMoreFlag(parsedBody)
        if (hasMore === false) {
          reachedEndByApi = true
        }

        const captured = {
          url: requestUrlById.get(requestId) || '',
          method: requestMethodById.get(requestId) || '',
          captured_at: new Date().toISOString(),
          has_more: hasMore,
          body: parsedBody
        }
        responses.push(captured)
        console.log(
          `[CAPTURED ${responses.length}] method=${captured.method} url=${captured.url} has_more=${String(hasMore)}`
        )
      } catch (error) {
        console.warn(
          `[CAPTURE ERROR] url=${requestUrlById.get(requestId) || ''} message=${error instanceof Error ? error.message : String(error)}`
        )
      }
    })()

    pendingTasks.add(task)
    void task.finally(() => {
      pendingTasks.delete(task)
      matchedRequestIds.delete(requestId)
      requestUrlById.delete(requestId)
      requestMethodById.delete(requestId)
    })
  })

  console.log(`[OPEN] ${TARGET_URL}`)
  await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 60000 })
  console.log(`[WAIT] initial=${INITIAL_WAIT_MS}ms`)
  await sleep(INITIAL_WAIT_MS)

  let previousResponseCount = responses.length
  let idleRounds = 0

  for (let round = 0; round < MAX_SCROLL_ROUNDS; round += 1) {
    if (reachedEndByApi) {
      console.log(`[STOP] has_more=false round=${round + 1}`)
      break
    }

    const moved = await scrollOnce(page)
    await sleep(AFTER_SCROLL_WAIT_MS)

    const currentResponseCount = responses.length
    if (currentResponseCount > previousResponseCount) {
      console.log(
        `[SCROLL ${round + 1}] moved=${moved} delta=${currentResponseCount - previousResponseCount} total=${currentResponseCount}`
      )
      previousResponseCount = currentResponseCount
      idleRounds = 0
      continue
    }

    idleRounds += 1
    console.log(`[SCROLL ${round + 1}] moved=${moved} delta=0 idle=${idleRounds}/${MAX_IDLE_ROUNDS}`)
    if (!moved || idleRounds >= MAX_IDLE_ROUNDS) {
      console.log(`[STOP] idle rounds reached at round=${round + 1}`)
      break
    }
  }

  await Promise.allSettled(Array.from(pendingTasks))

  const timestamp = new Date().toISOString().replace(/[:.]/g, '-')
  const outputFile = OUTPUT_FILE || path.join(OUTPUT_DIR, `captured_api_responses_${timestamp}.json`)

  await fs.mkdir(path.dirname(outputFile), { recursive: true })
  await fs.writeFile(
    outputFile,
    JSON.stringify(
      {
        target_url: TARGET_URL,
        url_includes: URL_INCLUDES,
        method: METHOD || null,
        initial_wait_ms: INITIAL_WAIT_MS,
        after_scroll_wait_ms: AFTER_SCROLL_WAIT_MS,
        scroll_step_px: SCROLL_STEP_PX,
        max_scroll_rounds: MAX_SCROLL_ROUNDS,
        reached_end_by_api: reachedEndByApi,
        responses_captured: responses.length,
        responses
      },
      null,
      2
    ),
    'utf8'
  )

  console.log(`[DONE] responses=${responses.length} output=${outputFile}`)
  await cdp.detach().catch(() => undefined)
  await browser.close()
}

void main().catch((error) => {
  console.error(error)
  process.exit(1)
})
