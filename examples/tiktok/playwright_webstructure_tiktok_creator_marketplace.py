#!/usr/bin/env python3
"""Public TikTok creator marketplace crawler example using Playwright.

This is a lightweight, shareable example intended for manual sessions that
already have access to the marketplace via storage state or an interactive
login in the launched browser.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import List


LOGGER = logging.getLogger("playwright_webstructure_tiktok_creator_marketplace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape visible TikTok creator marketplace cards.")
    parser.add_argument(
        "--url",
        required=True,
        help="Creator marketplace page URL.",
    )
    parser.add_argument(
        "--output",
        default="output/tiktok/creator_marketplace.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--storage-state",
        help="Optional Playwright storage state JSON for an authenticated session.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode.",
    )
    parser.add_argument(
        "--search-keyword",
        help="Optional keyword to fill into a visible search box before scraping.",
    )
    parser.add_argument(
        "--scroll-rounds",
        type=int,
        default=8,
        help="Number of scroll rounds to load more cards.",
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=1500,
        help="Pause in milliseconds between actions.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30000,
        help="Navigation timeout in milliseconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum number of rows to keep.",
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


def write_rows(path: str, rows: List[dict]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "creator_name",
        "handle",
        "followers",
        "likes",
        "engagement_rate",
        "category",
        "profile_url",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def extract_cards(page) -> List[dict]:
    return page.evaluate(
        """
        () => {
          const containerSelectors = [
            '[data-e2e*="creator-card"]',
            '[class*="creator-card"]',
            '[class*="CreatorCard"]',
            '[data-uid*="creator-card"]',
            '[class*="card"]'
          ];
          const candidateCards = [];
          for (const selector of containerSelectors) {
            for (const node of document.querySelectorAll(selector)) {
              if (node instanceof HTMLElement && !candidateCards.includes(node)) {
                candidateCards.push(node);
              }
            }
          }
          const pickText = (root, selectors) => {
            for (const selector of selectors) {
              const node = root.querySelector(selector);
              if (node && node.textContent) {
                const text = node.textContent.replace(/\\s+/g, ' ').trim();
                if (text) return text;
              }
            }
            return '';
          };
          const rows = [];
          for (const card of candidateCards) {
            const profileLink = card.querySelector('a[href*="/creator"], a[href*="/user/"], a[href*="tiktok.com"]');
            const row = {
              creator_name: pickText(card, [
                '[data-e2e*="creator-name"]',
                '[class*="name"]',
                'h3',
                'h4',
                'strong'
              ]),
              handle: pickText(card, [
                '[data-e2e*="creator-account"]',
                '[class*="handle"]',
                '[class*="account"]'
              ]),
              followers: pickText(card, [
                '[data-e2e*="followers"]',
                '[class*="followers"]',
                '[class*="fans"]'
              ]),
              likes: pickText(card, [
                '[data-e2e*="likes"]',
                '[class*="likes"]',
                '[class*="gmv"]'
              ]),
              engagement_rate: pickText(card, [
                '[data-e2e*="engagement"]',
                '[class*="engagement"]',
                '[class*="rate"]'
              ]),
              category: pickText(card, [
                '[data-e2e*="category"]',
                '[class*="category"]',
                '[class*="tag"]'
              ]),
              profile_url: profileLink ? profileLink.href : ''
            };
            if (row.creator_name || row.handle || row.profile_url) {
              rows.push(row);
            }
          }
          return rows;
        }
        """
    )


def maybe_search(page, keyword: str, pause_ms: int) -> None:
    search_selectors = [
        'input[placeholder*="Search"]',
        'input[placeholder*="search"]',
        'input[type="search"]',
        'input',
    ]
    for selector in search_selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            locator.click(timeout=3000)
            locator.fill(keyword, timeout=3000)
            locator.press("Enter")
            page.wait_for_timeout(pause_ms)
            LOGGER.info("Applied search keyword with selector %s", selector)
            return
        except Exception:
            continue
    LOGGER.warning("No visible search box matched the provided keyword.")


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    sync_playwright = require_playwright()
    all_rows: List[dict] = []
    seen_keys = set()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=args.headless)
        context_kwargs = {}
        if args.storage_state:
            context_kwargs["storage_state"] = args.storage_state
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        page.wait_for_timeout(args.pause_ms)

        if args.search_keyword:
            maybe_search(page, args.search_keyword, args.pause_ms)

        for round_index in range(args.scroll_rounds + 1):
            rows = extract_cards(page)
            for row in rows:
                key = (row.get("profile_url", ""), row.get("handle", ""), row.get("creator_name", ""))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_rows.append(row)
                if len(all_rows) >= args.limit:
                    break
            LOGGER.info("Collected %s visible creator rows after round %s", len(all_rows), round_index)
            if len(all_rows) >= args.limit:
                break
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(args.pause_ms)

        browser.close()

    write_rows(args.output, all_rows[: args.limit])
    json_path = Path(args.output).with_suffix(".json")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(all_rows[: args.limit], ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
