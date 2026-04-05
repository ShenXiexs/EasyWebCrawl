#!/usr/bin/env python3
"""Public Reddit enrichment example using PRAW."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


LOGGER = logging.getLogger("praw_api_reddit_submission_enrich")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich Reddit submission IDs with PRAW.")
    parser.add_argument(
        "--input",
        required=True,
        help="Input CSV path. The file must contain an 'id' column.",
    )
    parser.add_argument(
        "--output",
        default="output/reddit/reddit_submission_enrich.csv",
        help="Output CSV path.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=1.0,
        help="Delay between requests in seconds.",
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


def build_reddit_client():
    client_id = os.getenv("REDDIT_CLIENT_ID", "").strip()
    client_secret = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    user_agent = os.getenv("REDDIT_USER_AGENT", "").strip()
    if not all([client_id, client_secret, user_agent]):
        raise SystemExit(
            "Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT before running this script."
        )
    try:
        import praw
    except ImportError as exc:
        raise SystemExit("Install praw first: pip install praw") from exc
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
    )


def read_input_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit(f"No rows found in {path}")
    if "id" not in rows[0]:
        raise SystemExit(f"Input CSV must contain an 'id' column: {path}")
    return rows


def write_rows(path: str, rows: List[Dict[str, object]]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "id",
        "title",
        "subreddit",
        "author",
        "created_utc",
        "stickied",
        "score",
        "num_comments",
        "num_crossposts",
        "permalink",
        "url",
    ]
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    reddit = build_reddit_client()
    input_rows = read_input_rows(args.input)

    output_rows: List[Dict[str, object]] = []
    for row in input_rows:
        submission_id = str(row["id"]).strip()
        if not submission_id:
            continue
        LOGGER.info("Fetching %s", submission_id)
        try:
            submission = reddit.submission(id=submission_id)
            output_rows.append(
                {
                    "id": submission.id,
                    "title": submission.title,
                    "subreddit": submission.subreddit.display_name,
                    "author": str(submission.author),
                    "created_utc": datetime.fromtimestamp(
                        submission.created_utc,
                        tz=timezone.utc,
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "stickied": submission.stickied,
                    "score": submission.score,
                    "num_comments": submission.num_comments,
                    "num_crossposts": submission.num_crossposts,
                    "permalink": f"https://www.reddit.com{submission.permalink}",
                    "url": submission.url,
                }
            )
        except Exception as exc:  # pragma: no cover - API responses are runtime dependent
            LOGGER.warning("Failed to fetch %s: %s", submission_id, exc)
        time.sleep(args.sleep)

    write_rows(args.output, output_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
