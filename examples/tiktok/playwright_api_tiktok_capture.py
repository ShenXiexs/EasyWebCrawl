#!/usr/bin/env python3
"""Capture JSON API responses from a page with Playwright + CDP."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


LOGGER = logging.getLogger("playwright_api_tiktok_capture")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture matching JSON API responses from a page.")
    parser.add_argument("--target-url", required=True, help="Page URL to open before listening for network traffic.")
    parser.add_argument("--url-includes", required=True, help="Substring used to match target API requests.")
    parser.add_argument("--method", default="", help="Optional HTTP method filter such as GET or POST.")
    parser.add_argument(
        "--output",
        default="output/tiktok/captured_api_responses.json",
        help="Output JSON path.",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--initial-wait-ms", type=int, default=5000, help="Wait after initial navigation.")
    parser.add_argument("--after-scroll-wait-ms", type=int, default=1200, help="Wait after each scroll round.")
    parser.add_argument("--scroll-step-px", type=int, default=1200, help="Scroll distance per round.")
    parser.add_argument("--max-scroll-rounds", type=int, default=8, help="Maximum number of scroll rounds.")
    parser.add_argument("--max-idle-rounds", type=int, default=3, help="Stop after this many zero-delta rounds.")
    parser.add_argument(
        "--scroll-container-selector",
        help="Optional selector for a scrollable element. Defaults to the page scroll container.",
    )
    parser.add_argument(
        "--storage-state",
        help="Optional Playwright storage state JSON for authenticated sessions.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install playwright first: pip install playwright") from exc
    return sync_playwright


def read_has_more_flag(payload) -> Optional[bool]:
    candidate_paths = [
        ["data", "has_more"],
        ["pagination", "has_more"],
        ["next_pagination", "has_more"],
        ["has_more"],
    ]
    for path in candidate_paths:
        current = payload
        for segment in path:
            if not isinstance(current, dict) or segment not in current:
                current = None
                break
            current = current[segment]
        if isinstance(current, bool):
            return current
    return None


def scroll_once(page, selector: Optional[str], step_px: int) -> bool:
    return bool(
        page.evaluate(
            """
            ({ selector, stepPx }) => {
              const target = selector
                ? document.querySelector(selector)
                : (document.scrollingElement || document.documentElement);
              if (!(target instanceof HTMLElement)) return false;
              const beforeTop = target.scrollTop;
              const maxTop = Math.max(0, target.scrollHeight - target.clientHeight);
              const nextTop = Math.min(maxTop, beforeTop + stepPx);
              target.scrollTo({ top: nextTop, behavior: 'auto' });
              return nextTop > beforeTop;
            }
            """,
            {"selector": selector, "stepPx": step_px},
        )
    )


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    method = args.method.strip().upper()
    sync_playwright = require_playwright()

    responses: List[Dict[str, object]] = []
    matched_request_ids = set()
    processed_request_ids = set()
    request_url_by_id: Dict[str, str] = {}
    request_method_by_id: Dict[str, str] = {}
    reached_end_by_api = {"value": False}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context_kwargs = {}
        if args.storage_state:
            context_kwargs["storage_state"] = args.storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")

        def on_request_will_be_sent(params):
            request_id = str(params.get("requestId", ""))
            request = params.get("request", {})
            url = str(request.get("url", ""))
            current_method = str(request.get("method", "")).upper()
            if not request_id or args.url_includes not in url:
                return
            if method and current_method != method:
                return
            matched_request_ids.add(request_id)
            request_url_by_id[request_id] = url
            request_method_by_id[request_id] = current_method
            LOGGER.info("Matched request %s %s", current_method, url)

        def on_response_received(params):
            request_id = str(params.get("requestId", ""))
            if request_id not in matched_request_ids:
                return
            response = params.get("response", {})
            mime_type = str(response.get("mimeType", ""))
            resource_type = str(params.get("type", ""))
            is_json_like = resource_type in {"XHR", "Fetch"} or "json" in mime_type.lower()
            if not is_json_like:
                matched_request_ids.discard(request_id)
                request_url_by_id.pop(request_id, None)
                request_method_by_id.pop(request_id, None)

        def on_loading_finished(params):
            request_id = str(params.get("requestId", ""))
            if request_id not in matched_request_ids or request_id in processed_request_ids:
                return
            processed_request_ids.add(request_id)
            try:
                body_response = cdp.send("Network.getResponseBody", {"requestId": request_id})
                raw_body = body_response.get("body", "")
                if body_response.get("base64Encoded"):
                    import base64

                    raw_body = base64.b64decode(raw_body).decode("utf-8", errors="replace")
                payload = json.loads(raw_body)
                has_more = read_has_more_flag(payload)
                if has_more is False:
                    reached_end_by_api["value"] = True
                responses.append(
                    {
                        "url": request_url_by_id.get(request_id, ""),
                        "method": request_method_by_id.get(request_id, ""),
                        "captured_at": datetime.utcnow().isoformat() + "Z",
                        "has_more": has_more,
                        "body": payload,
                    }
                )
                LOGGER.info("Captured %s responses", len(responses))
            except Exception as exc:  # pragma: no cover - network events are runtime dependent
                LOGGER.warning("Failed to capture response body for %s: %s", request_id, exc)
            finally:
                matched_request_ids.discard(request_id)
                request_url_by_id.pop(request_id, None)
                request_method_by_id.pop(request_id, None)

        cdp.on("Network.requestWillBeSent", on_request_will_be_sent)
        cdp.on("Network.responseReceived", on_response_received)
        cdp.on("Network.loadingFinished", on_loading_finished)

        page.goto(args.target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(args.initial_wait_ms)

        previous_count = len(responses)
        idle_rounds = 0
        for round_index in range(args.max_scroll_rounds):
            if reached_end_by_api["value"]:
                LOGGER.info("API reported has_more=false at round %s", round_index + 1)
                break
            moved = scroll_once(page, args.scroll_container_selector, args.scroll_step_px)
            page.wait_for_timeout(args.after_scroll_wait_ms)
            current_count = len(responses)
            if current_count > previous_count:
                previous_count = current_count
                idle_rounds = 0
            else:
                idle_rounds += 1
            LOGGER.info(
                "Scroll round %s moved=%s responses=%s idle=%s/%s",
                round_index + 1,
                moved,
                current_count,
                idle_rounds,
                args.max_idle_rounds,
            )
            if not moved or idle_rounds >= args.max_idle_rounds:
                break

        page.wait_for_timeout(args.after_scroll_wait_ms)
        browser.close()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_payload = {
        "target_url": args.target_url,
        "url_includes": args.url_includes,
        "method": method or None,
        "initial_wait_ms": args.initial_wait_ms,
        "after_scroll_wait_ms": args.after_scroll_wait_ms,
        "scroll_step_px": args.scroll_step_px,
        "max_scroll_rounds": args.max_scroll_rounds,
        "max_idle_rounds": args.max_idle_rounds,
        "reached_end_by_api": reached_end_by_api["value"],
        "responses_captured": len(responses),
        "responses": responses,
    }
    output_path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
