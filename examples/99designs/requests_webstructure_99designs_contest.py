#!/usr/bin/env python3
"""Public 99designs crawler example using requests + BeautifulSoup.

This script consolidates three legacy workflows:
1. contest list pages
2. contest brief pages
3. contest entry pages and designer profile pages
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


LOGGER = logging.getLogger("requests_webstructure_99designs_contest")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
BRIEF_STYLE_FIELDS = [
    "ClassicModern",
    "MatureYouthful",
    "FeminineMasculine",
    "PlayfulSophisticated",
    "EconomicalLuxurious",
    "GeometricOrganic",
    "AbstractLiteral",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consolidated 99designs crawler example.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["list", "brief", "entries", "all"],
        help="Pipeline stage to run.",
    )
    parser.add_argument(
        "--url",
        help="A list page URL for list/all mode, or a single contest entries URL for brief/entries mode.",
    )
    parser.add_argument(
        "--input",
        help="CSV or TXT file containing contest URLs. For CSV, ContestURL/URL/url columns are supported.",
    )
    parser.add_argument(
        "--output",
        default="output/99designs",
        help="Output directory for CSV and optional image downloads.",
    )
    parser.add_argument(
        "--cookies-file",
        help="Path to a JSON file containing cookies as an object.",
    )
    parser.add_argument(
        "--headers-file",
        help="Path to a JSON file containing headers as an object.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maximum list or entries pages to crawl.",
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="Download brief reference images and entry images.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Base delay between HTTP requests in seconds.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds.",
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


def require_requests_bs4():
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SystemExit(
            "This script requires 'requests' and 'beautifulsoup4'. "
            "Install them with: pip install requests beautifulsoup4"
        ) from exc
    return requests, BeautifulSoup


def load_json_mapping(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit(f"Expected JSON object in {path}")
    return {str(key): str(value) for key, value in data.items()}


def build_session(headers: Dict[str, str]):
    requests, _ = require_requests_bs4()
    session = requests.Session()
    if "user-agent" not in {key.lower() for key in headers}:
        headers = {"User-Agent": DEFAULT_USER_AGENT, **headers}
    session.headers.update(headers)
    return session


def looks_like_waf(html_text: str) -> bool:
    if not html_text:
        return False
    lowered = html_text.lower()
    return (
        "token.awswaf.com" in lowered
        or "challenge.js" in lowered
        or 'id="challenge-container"' in lowered
        or "verify that you're not a robot" in lowered
    )


def fetch_response_text(
    session,
    url: str,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
    retries: int = 4,
) -> str:
    requests, _ = require_requests_bs4()
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if attempt > 1:
                sleep_for = delay * (2 ** (attempt - 2)) + random.uniform(0.3, 1.2)
                time.sleep(min(sleep_for, 15))
            response = session.get(url, cookies=cookies, timeout=timeout)
            response.raise_for_status()
            if looks_like_waf(response.text):
                raise requests.HTTPError("Detected possible WAF challenge page")
            return response.text
        except Exception as exc:  # pragma: no cover - network errors are data dependent
            last_error = exc
            LOGGER.warning("Request attempt %s/%s failed for %s: %s", attempt, retries, url, exc)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def fetch_binary(
    session,
    url: str,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
    retries: int = 4,
) -> bytes:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            if attempt > 1:
                time.sleep(min(delay * attempt, 10))
            response = session.get(url, cookies=cookies, timeout=timeout)
            response.raise_for_status()
            return response.content
        except Exception as exc:  # pragma: no cover - network errors are data dependent
            last_error = exc
            LOGGER.warning("Binary request attempt %s/%s failed for %s: %s", attempt, retries, url, exc)
    raise RuntimeError(f"Failed to download {url}: {last_error}")


def append_query_parameter(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_items[key] = value
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def extract_contest_id(url: str) -> str:
    match = re.search(r"contests/[^/]+-(\d+)", url)
    return match.group(1) if match else "N/A"


def read_contest_urls(input_path: str) -> List[str]:
    path = Path(input_path)
    if not path.exists():
        raise SystemExit(f"Input file does not exist: {input_path}")
    if path.suffix.lower() == ".txt":
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if not rows:
        return []
    candidate_fields = ["ContestURL", "URL", "url"]
    for field in candidate_fields:
        if field in rows[0]:
            return [row[field].strip() for row in rows if row.get(field, "").strip()]
    raise SystemExit(f"Could not find any of {candidate_fields} in {input_path}")


def ensure_output_dir(output_dir: str) -> Path:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_csv(path: Path, rows: Sequence[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def dedupe_rows(rows: Iterable[Dict[str, object]], key_fields: Sequence[str]) -> List[Dict[str, object]]:
    seen = set()
    unique_rows: List[Dict[str, object]] = []
    for row in rows:
        key = tuple(str(row.get(field, "")) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(dict(row))
    return unique_rows


def list_mode(
    base_url: str,
    session,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
    max_pages: int,
) -> List[Dict[str, object]]:
    _, BeautifulSoup = require_requests_bs4()
    current_url = base_url
    results: List[Dict[str, object]] = []
    page_index = 1
    while current_url and page_index <= max_pages:
        LOGGER.info("List page %s: %s", page_index, current_url)
        html = fetch_response_text(session, current_url, cookies, timeout, delay)
        soup = BeautifulSoup(html, "html.parser")
        for item in soup.find_all("div", class_="content-listing__item"):
            link = item.find("a", class_="listing-details__title__link") or item.find(
                "a", class_="content-listing__item__link-overlay"
            )
            if not link or not link.get("href"):
                continue
            href = str(link["href"])
            contest_url = urljoin(current_url, href)
            contest_entries_url = contest_url.rstrip("/") + "/entries?groupby=designer"
            reward_tag = item.find("div", class_="ribbon__text")
            tags: List[str] = []
            blind = 0
            for section in item.find_all("div", class_="listing-details__section"):
                for tag in section.find_all("span", class_="listing-details__pill"):
                    value = tag.get_text(strip=True)
                    tags.append(value)
                    if value.lower() == "blind":
                        blind = 1
            current_ideas = 0
            for stat in item.find_all("div", class_="listing-details__stat-item"):
                label = stat.find("span", class_="listing-details__stat__label")
                if not label:
                    continue
                match = re.search(r"(\d+)\s+designs", label.get_text(strip=True))
                if match:
                    current_ideas = int(match.group(1))
                    break
            results.append(
                {
                    "ContestID": extract_contest_id(contest_entries_url),
                    "ContestName": link.get_text(strip=True),
                    "ContestURL": contest_entries_url,
                    "Reward": reward_tag.get_text(strip=True) if reward_tag else "",
                    "Blind": blind,
                    "Tags": ",".join(tags),
                    "CurrentIdeas": current_ideas,
                }
            )
        next_anchor = soup.select_one("span.pagination--next a.pagination__button")
        current_url = urljoin(current_url, next_anchor["href"]) if next_anchor and next_anchor.get("href") else ""
        page_index += 1
        time.sleep(delay)
    return dedupe_rows(results, ["ContestID"])


def parse_brief_data(html: str, contest_id: str) -> Dict[str, object]:
    _, BeautifulSoup = require_requests_bs4()
    soup = BeautifulSoup(html, "html.parser")
    price_usd = "N/A"
    package_level = "N/A"
    price_block = soup.find("div", id="header-price-data")
    if price_block and price_block.has_attr("data-initial-props"):
        raw = str(price_block["data-initial-props"]).replace("&quot;", '"')
        try:
            props = json.loads(raw)
            purchase_price = str(props.get("purchasePrice", "N/A"))
            price_usd = purchase_price.replace("US$", "")
            package_level = str(props.get("packageName", "N/A"))
        except json.JSONDecodeError:
            LOGGER.debug("Failed to decode brief header JSON for contest %s", contest_id)

    style_defaults = {field: "N/A" for field in BRIEF_STYLE_FIELDS}
    style_pattern = re.compile(
        r'&quot;(classicModern|matureYouthful|feminineMasculine|playfulSophisticated|economicalLuxurious|geometricOrganic|abstractLiteral)&quot;:(-?\d)'
    )
    for attr, value in style_pattern.findall(html):
        mapping = {
            "classicModern": "ClassicModern",
            "matureYouthful": "MatureYouthful",
            "feminineMasculine": "FeminineMasculine",
            "playfulSophisticated": "PlayfulSophisticated",
            "economicalLuxurious": "EconomicalLuxurious",
            "geometricOrganic": "GeometricOrganic",
            "abstractLiteral": "AbstractLiteral",
        }
        style_defaults[mapping[attr]] = value

    guarantee_text = soup.find("div", attrs={"data-meta-guarantee-tooltip-content": True})
    fasttrack_text = soup.find(
        "div",
        string=re.compile(
            r"Following the open round, the client will select a winning design. There is no refinement stage."
        ),
    )
    blind_tag = soup.find("span", class_="meta-item__label", string="Blind")
    industry_match = re.search(r'industry&quot;:\{&quot;value&quot;:&quot;([^"&]+)&quot;', html)
    notes_match = re.search(r'notes&quot;:\{&quot;value&quot;:&quot;(.*?)&quot;', html)
    public_ids = re.findall(r'&quot;publicId&quot;:&quot;([A-Za-z0-9]+)&quot;', html)
    reference_match = re.search(
        r'References&quot;,&quot;elements&quot;:\{&quot;attachments&quot;:\{&quot;value&quot;:\[\{&quot;publicId&quot;:&quot;([A-Za-z0-9]+)&quot;',
        html,
    )
    reference_id = reference_match.group(1) if reference_match else None
    reference_count = 0
    inspiration_count = 0
    for public_id in public_ids:
        if reference_id and public_id == reference_id:
            reference_count += 1
        else:
            inspiration_count += 1

    return {
        "ContestID": contest_id,
        "PriceUSD": price_usd,
        "PackageLevel": package_level,
        "Guarantee": 1
        if guarantee_text and "guaranteed to award the prize" in guarantee_text.get_text(" ", strip=True)
        else 0,
        "Blind": 1 if blind_tag else 0,
        "Fasttrack": 1 if fasttrack_text else 0,
        "Industry": industry_match.group(1) if industry_match else "N/A",
        "OtherNotes": notes_match.group(1).replace("&quot;", '"') if notes_match else "N/A",
        "Inspiration": inspiration_count,
        "Reference": reference_count,
        **style_defaults,
    }


def download_brief_reference_images(
    session,
    brief_html: str,
    contest_id: str,
    output_dir: Path,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
) -> None:
    public_ids = re.findall(r'&quot;publicId&quot;:&quot;([A-Za-z0-9]+)&quot;', brief_html)
    if not public_ids:
        return
    reference_dir = output_dir / contest_id / "brief_images"
    reference_dir.mkdir(parents=True, exist_ok=True)
    for index, public_id in enumerate(public_ids, start=1):
        target = reference_dir / f"{index:02d}_{public_id}.png"
        if target.exists():
            continue
        download_url = f"https://99designs.hk/contests/{contest_id}/brief/download/{public_id}"
        try:
            target.write_bytes(fetch_binary(session, download_url, cookies, timeout, delay))
        except Exception as exc:  # pragma: no cover - network errors are data dependent
            LOGGER.warning("Failed to download brief image %s: %s", download_url, exc)


def brief_mode(
    contest_urls: Sequence[str],
    session,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
    download_images: bool,
    output_dir: Path,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for contest_url in contest_urls:
        contest_id = extract_contest_id(contest_url)
        brief_url = contest_url.replace("/entries", "/brief")
        LOGGER.info("Brief: %s", brief_url)
        html = fetch_response_text(session, brief_url, cookies, timeout, delay)
        rows.append(parse_brief_data(html, contest_id))
        if download_images:
            download_brief_reference_images(session, html, contest_id, output_dir, cookies, timeout, delay)
        time.sleep(delay)
    return dedupe_rows(rows, ["ContestID"])


def parse_user_profile(html: str) -> Dict[str, object]:
    _, BeautifulSoup = require_requests_bs4()
    soup = BeautifulSoup(html, "html.parser")
    aggregate = soup.find("span", itemprop="aggregateRating")
    rating_value = ""
    review_count = ""
    if aggregate:
        rating_span = aggregate.find("span", itemprop="ratingValue")
        review_span = aggregate.find("span", itemprop="reviewCount")
        rating_value = rating_span.get_text(strip=True) if rating_span else ""
        review_count = review_span.get_text(strip=True) if review_span else ""

    def read_stat(title_pattern: str) -> str:
        block = soup.find("div", class_="stats-panel__item--first", title=re.compile(title_pattern))
        if not block:
            block = soup.find("div", class_="stats-panel__item", title=re.compile(title_pattern))
        if not block:
            return ""
        value_block = block.find("div", class_="stats-panel__item__value")
        return value_block.get_text(strip=True) if value_block else ""

    tags = [tag.get_text(strip=True) for tag in soup.select("div.profile__tag-section span.pill.pill--tag")]
    languages_header = soup.find("h3", class_="heading heading--size4", string=re.compile(r"Languages"))
    languages: List[str] = []
    if languages_header:
        group = languages_header.find_next("div", class_="pill-group")
        if group:
            languages = [tag.get_text(strip=True) for tag in group.select("span.pill.pill--tag")]

    certifications: List[str] = []
    for tag in soup.select("span.pill.pill--tag.pill--certification"):
        certifications.append(tag.get_text(strip=True))
    for item in soup.select("div.pill-group__item[title]"):
        pill = item.find("span", class_=re.compile("pill"))
        if pill:
            certifications.append(pill.get_text(strip=True))

    member_since = soup.find("span", class_="subtle-text", string=re.compile(r"Member since:"))
    return {
        "AggregateRating": rating_value or "N/A",
        "AggregateReviews": review_count or "N/A",
        "StartDate": member_since.get_text(strip=True).replace("Member since:", "").strip()
        if member_since
        else "N/A",
        "ContestsWon": read_stat(r"contest prize awards") or "N/A",
        "RunnerUp": read_stat(r"contest finalist") or "N/A",
        "OnetoOne": read_stat(r"1-to-1 Projects completed") or "N/A",
        "RepeatClients": read_stat(r"clients who hired this designer") or "N/A",
        "UserTag": ", ".join(tags) if tags else "N/A",
        "Certifications": ", ".join(dict.fromkeys(certifications)) if certifications else "N/A",
        "Languages": ", ".join(languages) if languages else "N/A",
    }


def fetch_real_image_and_create_time(
    session,
    entry_url: str,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
) -> Tuple[str, str]:
    _, BeautifulSoup = require_requests_bs4()
    html = fetch_response_text(session, entry_url, cookies, timeout, delay)
    soup = BeautifulSoup(html, "html.parser")
    image_tag = soup.find("link", rel="image_src")
    image_url = image_tag.get("href") if image_tag else "N/A"
    create_time_match = re.search(r'"timeCreatedString":"([^"]+)"', html)
    create_time = create_time_match.group(1) if create_time_match else "N/A"
    return image_url, create_time


def download_entry_image(
    session,
    image_url: str,
    target_path: Path,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        return
    target_path.write_bytes(fetch_binary(session, image_url, cookies, timeout, delay))


def extract_entry_cards(soup) -> List[object]:
    owner_tags = soup.find_all(class_=lambda cls: cls in ["entry-owner__id", "entry-owner__id-link"])
    entry_divs = []
    seen_ids = set()
    for tag in owner_tags:
        entry_div = tag.find_parent("div", class_=re.compile(r"^entry\b"))
        if not entry_div:
            continue
        entry_id = entry_div.get("id")
        if not entry_id or entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        entry_divs.append(entry_div)
    return entry_divs


def entries_mode(
    contest_urls: Sequence[str],
    session,
    cookies: Dict[str, str],
    timeout: int,
    delay: float,
    max_pages: int,
    download_images: bool,
    output_dir: Path,
) -> List[Dict[str, object]]:
    _, BeautifulSoup = require_requests_bs4()
    entry_rows: List[Dict[str, object]] = []
    brief_cache: Dict[str, Dict[str, object]] = {}
    profile_cache: Dict[str, Dict[str, object]] = {}
    for contest_url in contest_urls:
        contest_id = extract_contest_id(contest_url)
        if contest_id not in brief_cache:
            brief_html = fetch_response_text(session, contest_url.replace("/entries", "/brief"), cookies, timeout, delay)
            brief_cache[contest_id] = parse_brief_data(brief_html, contest_id)
        brief_data = brief_cache[contest_id]
        for page in range(1, max_pages + 1):
            page_url = append_query_parameter(contest_url, "page", str(page))
            LOGGER.info("Entries page %s for contest %s", page, contest_id)
            html = fetch_response_text(session, page_url, cookies, timeout, delay)
            soup = BeautifulSoup(html, "html.parser")
            cards = extract_entry_cards(soup)
            if not cards:
                if page == 1:
                    LOGGER.warning("No entry cards found for %s", contest_url)
                break
            for card in cards:
                entry_id = str(card.get("id", "")).replace("entry-", "")
                designer_tag = card.find("a", class_="entry-owner__designer-name-link")
                user_name = designer_tag.get_text(strip=True) if designer_tag else "N/A"
                user_url = urljoin(page_url, str(designer_tag["href"]).rstrip("/") + "/about") if designer_tag and designer_tag.get("href") else "N/A"
                profile_data = {
                    "AggregateRating": "N/A",
                    "AggregateReviews": "N/A",
                    "StartDate": "N/A",
                    "ContestsWon": "N/A",
                    "RunnerUp": "N/A",
                    "OnetoOne": "N/A",
                    "RepeatClients": "N/A",
                    "UserTag": "N/A",
                    "Certifications": "N/A",
                    "Languages": "N/A",
                }
                if user_url != "N/A":
                    if user_url not in profile_cache:
                        try:
                            profile_html = fetch_response_text(session, user_url, cookies, timeout, delay)
                            profile_cache[user_url] = parse_user_profile(profile_html)
                        except Exception as exc:  # pragma: no cover - network errors are data dependent
                            LOGGER.warning("Failed to fetch profile %s: %s", user_url, exc)
                            profile_cache[user_url] = dict(profile_data)
                    profile_data = profile_cache[user_url]
                status = ""
                status_overlay = card.find("div", class_="entry__image__status-overlay")
                if status_overlay:
                    for block in status_overlay.find_all("div", class_="entry-status-overlay"):
                        if block.has_attr("data-hidden"):
                            continue
                        title_span = block.find("span", class_="entry-status-overlay__title")
                        if title_span:
                            status = title_span.get_text(strip=True)
                            break
                rating_tag = card.find("input", attrs={"checked": "checked"})
                rating = rating_tag.get("value", "N/A") if rating_tag else "N/A"
                winner = 1 if card.find("div", attrs={"data-entry-status": "winner"}) else 0
                design_id = card.get("data-design-id", "N/A")
                user_id = card.get("data-user-id", "N/A")
                entry_link = card.find("a", class_="entry__image__inner")
                full_entry_url = urljoin(page_url, entry_link["href"]) if entry_link and entry_link.get("href") else ""
                image_url = "N/A"
                create_time = "N/A"
                if full_entry_url:
                    try:
                        image_url, create_time = fetch_real_image_and_create_time(
                            session,
                            full_entry_url,
                            cookies,
                            timeout,
                            delay,
                        )
                    except Exception as exc:  # pragma: no cover - network errors are data dependent
                        LOGGER.warning("Failed to fetch entry details %s: %s", full_entry_url, exc)
                if download_images and image_url not in {"", "N/A"}:
                    image_dir = output_dir / contest_id / "entry_images"
                    file_name = f"{entry_id}_{user_id}.png"
                    try:
                        download_entry_image(session, image_url, image_dir / file_name, cookies, timeout, delay)
                    except Exception as exc:  # pragma: no cover - network errors are data dependent
                        LOGGER.warning("Failed to download entry image %s: %s", image_url, exc)
                entry_rows.append(
                    {
                        "ContestID": contest_id,
                        "PriceUSD": brief_data["PriceUSD"],
                        "PackageLevel": brief_data["PackageLevel"],
                        "Guarantee": brief_data["Guarantee"],
                        "Blind": brief_data["Blind"],
                        "Fasttrack": brief_data["Fasttrack"],
                        "Industry": brief_data["Industry"],
                        "OtherNotes": brief_data["OtherNotes"],
                        "Inspiration": brief_data["Inspiration"],
                        "Reference": brief_data["Reference"],
                        "CreateTime": create_time,
                        "DesignID": design_id,
                        "Entry": entry_id,
                        "Rating": rating,
                        "Winner": winner,
                        "ImageURL": image_url,
                        "UserID": user_id,
                        "UserName": user_name,
                        "UserURL": user_url,
                        **profile_data,
                        **{field: brief_data[field] for field in BRIEF_STYLE_FIELDS},
                        "Status": status or "N/A",
                    }
                )
            time.sleep(delay)
    return dedupe_rows(entry_rows, ["ContestID", "Entry"])


def collect_contest_urls(args) -> List[str]:
    if args.input:
        urls = read_contest_urls(args.input)
        if urls:
            return urls
    if args.url:
        return [args.url]
    raise SystemExit("Provide either --url or --input")


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    cookies = load_json_mapping(args.cookies_file)
    headers = load_json_mapping(args.headers_file)
    session = build_session(headers)
    output_dir = ensure_output_dir(args.output)

    if args.mode == "list":
        if not args.url:
            raise SystemExit("--url is required for --mode list")
        list_rows = list_mode(args.url, session, cookies, args.timeout, args.delay, args.max_pages)
        write_csv(
            output_dir / "contest_list.csv",
            list_rows,
            ["ContestID", "ContestName", "ContestURL", "Reward", "Blind", "Tags", "CurrentIdeas"],
        )
        return 0

    contest_urls: List[str]
    if args.mode == "all":
        if not args.url:
            raise SystemExit("--url is required for --mode all")
        list_rows = list_mode(args.url, session, cookies, args.timeout, args.delay, args.max_pages)
        write_csv(
            output_dir / "contest_list.csv",
            list_rows,
            ["ContestID", "ContestName", "ContestURL", "Reward", "Blind", "Tags", "CurrentIdeas"],
        )
        contest_urls = [str(row["ContestURL"]) for row in list_rows]
    else:
        contest_urls = collect_contest_urls(args)

    if args.mode in {"brief", "all"}:
        brief_rows = brief_mode(
            contest_urls,
            session,
            cookies,
            args.timeout,
            args.delay,
            args.download_images,
            output_dir,
        )
        write_csv(
            output_dir / "contest_brief.csv",
            brief_rows,
            [
                "ContestID",
                "PriceUSD",
                "PackageLevel",
                "Guarantee",
                "Blind",
                "Fasttrack",
                "Industry",
                "OtherNotes",
                "Inspiration",
                "Reference",
                *BRIEF_STYLE_FIELDS,
            ],
        )
        if args.mode == "brief":
            return 0

    if args.mode in {"entries", "all"}:
        entry_rows = entries_mode(
            contest_urls,
            session,
            cookies,
            args.timeout,
            args.delay,
            args.max_pages,
            args.download_images,
            output_dir,
        )
        write_csv(
            output_dir / "contest_entries.csv",
            entry_rows,
            [
                "ContestID",
                "PriceUSD",
                "PackageLevel",
                "Guarantee",
                "Blind",
                "Fasttrack",
                "Industry",
                "OtherNotes",
                "Inspiration",
                "Reference",
                "CreateTime",
                "DesignID",
                "Entry",
                "Rating",
                "Winner",
                "ImageURL",
                "UserID",
                "UserName",
                "UserURL",
                "AggregateRating",
                "AggregateReviews",
                "StartDate",
                "ContestsWon",
                "RunnerUp",
                "OnetoOne",
                "RepeatClients",
                "UserTag",
                "Certifications",
                "Languages",
                *BRIEF_STYLE_FIELDS,
                "Status",
            ],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
