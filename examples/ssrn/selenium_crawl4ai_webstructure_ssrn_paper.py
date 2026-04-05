#!/usr/bin/env python3
"""Public SSRN crawler example combining Selenium and crawl4ai."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


LOGGER = logging.getLogger("selenium_crawl4ai_webstructure_ssrn_paper")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Consolidated SSRN paper crawler.")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["list", "detail", "all"],
        help="Pipeline stage to run.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Category CSV for list/all mode, or paper list CSV for detail mode.",
    )
    parser.add_argument(
        "--output",
        default="output/ssrn",
        help="Output directory for generated CSV and JSON files.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Selenium in headless mode.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maximum pages per SSRN category.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between requests or page loads.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Request and page wait timeout in seconds.",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent string used for Selenium and HTTP requests.",
    )
    parser.add_argument(
        "--start-date",
        help="Optional inclusive filter for paper post dates, format YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        help="Optional inclusive filter for paper post dates, format YYYY-MM-DD.",
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


def ensure_output_dir(output_dir: str) -> Path:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_csv_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[Dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def merge_unique_rows(rows: Iterable[Dict[str, object]], key_fields: Sequence[str]) -> List[Dict[str, object]]:
    seen = set()
    result: List[Dict[str, object]] = []
    for row in rows:
        key = tuple(str(row.get(field, "")) for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def require_selenium():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError as exc:
        raise SystemExit(
            "List mode requires selenium and webdriver-manager. "
            "Install them with: pip install selenium webdriver-manager"
        ) from exc
    return webdriver, Options, Service, By, WebDriverWait, EC, ChromeDriverManager


def build_page_url(first_page_url: str, page: int) -> str:
    if "page=" in first_page_url:
        return re.sub(r"page=\d+", f"page={page}", first_page_url)
    separator = "&" if "?" in first_page_url else "?"
    return f"{first_page_url}{separator}page={page}"


def parse_post_time(raw: str) -> Optional[datetime]:
    cleaned = raw.strip()
    for fmt in ("%d %b %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def list_mode(args: argparse.Namespace, output_dir: Path) -> Path:
    webdriver, Options, Service, By, WebDriverWait, EC, ChromeDriverManager = require_selenium()
    rows = read_csv_rows(args.input)
    if not rows:
        raise SystemExit(f"No rows found in {args.input}")

    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    options = Options()
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={args.user_agent}")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    output_rows: List[Dict[str, object]] = []
    try:
        for row in rows:
            field = row.get("Field", "")
            area = row.get("Area", "")
            category = row.get("Category", "")
            first_page_url = row.get("URL", "")
            if not first_page_url:
                LOGGER.warning("Skip category row without URL: %s", row)
                continue
            LOGGER.info("Category: %s", category or first_page_url)
            discovered_max_page = args.max_pages
            stop_category = False
            for page in range(1, args.max_pages + 1):
                current_url = build_page_url(first_page_url, page)
                page_loaded = False
                for attempt in range(1, 4):
                    try:
                        driver.get(current_url)
                        WebDriverWait(driver, args.timeout).until(
                            EC.presence_of_element_located(
                                (By.XPATH, '//*[@id="network-papers"]//ol/li[1]//a')
                            )
                        )
                        page_loaded = True
                        break
                    except Exception as exc:  # pragma: no cover - browser state is runtime dependent
                        LOGGER.warning(
                            "Failed to load %s on attempt %s/3: %s",
                            current_url,
                            attempt,
                            exc,
                        )
                        time.sleep(args.delay)
                if not page_loaded:
                    break

                if page == 1:
                    page_numbers = []
                    for anchor in driver.find_elements(By.XPATH, '//*[@id="network-papers"]//a'):
                        text = anchor.text.strip()
                        if text.isdigit():
                            page_numbers.append(int(text))
                    if page_numbers:
                        discovered_max_page = min(max(page_numbers), args.max_pages)

                if page > discovered_max_page:
                    break

                page_rows = driver.find_elements(By.XPATH, '//*[@id="network-papers"]//ol/li')
                if not page_rows:
                    break

                for index, _ in enumerate(page_rows, start=1):
                    try:
                        title_xpath = f'//*[@id="network-papers"]//ol/li[{index}]//div/div/div[1]/div[1]/a'
                        title_elem = driver.find_element(By.XPATH, title_xpath)
                        title = title_elem.text.strip()
                        paper_url = title_elem.get_attribute("href")
                        post_time = ""
                        candidate_xpaths = [
                            f'//*[@id="network-papers"]//ol/li[{index}]//div/div/div[1]/div[2]/span[2]',
                            f'//*[@id="network-papers"]//ol/li[{index}]//div/div/div[1]/div[3]/span[2]',
                            f'//*[@id="network-papers"]//ol/li[{index}]//div/div/div[1]/div[3]/span',
                            f'//*[@id="network-papers"]//ol/li[{index}]//div/div/div[1]/div[2]/span',
                        ]
                        for candidate_xpath in candidate_xpaths:
                            try:
                                node = driver.find_element(By.XPATH, candidate_xpath)
                                post_time = driver.execute_script(
                                    """
                                    const elem = arguments[0];
                                    if (!elem) return "";
                                    if (elem.childNodes.length > 1) {
                                      return (elem.childNodes[1].textContent || "").trim();
                                    }
                                    return (elem.textContent || "").trim();
                                    """,
                                    node,
                                )
                                if post_time:
                                    break
                            except Exception:
                                continue

                        if post_time and post_time.endswith("2020"):
                            stop_category = True
                            LOGGER.info("Stop category %s after reaching year 2020", category)
                            break

                        parsed_post_time = parse_post_time(post_time)
                        if start_date and (not parsed_post_time or parsed_post_time < start_date):
                            continue
                        if end_date and (not parsed_post_time or parsed_post_time > end_date):
                            continue

                        output_rows.append(
                            {
                                "Field": field,
                                "Area": area,
                                "Category": category,
                                "URL": first_page_url,
                                "Title": title,
                                "PostTime": post_time,
                                "PaperURL": paper_url,
                            }
                        )
                    except Exception as exc:  # pragma: no cover - DOM shape is runtime dependent
                        LOGGER.debug("Skip paper row %s on %s: %s", index, current_url, exc)
                        continue
                if stop_category:
                    break
                time.sleep(args.delay)
    finally:
        driver.quit()

    output_rows = merge_unique_rows(output_rows, ["PaperURL"])
    output_path = output_dir / "paper_list.csv"
    write_csv(
        output_path,
        output_rows,
        ["Field", "Area", "Category", "URL", "Title", "PostTime", "PaperURL"],
    )
    return output_path


def require_bs4():
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SystemExit(
            "Detail mode requires beautifulsoup4. Install it with: pip install beautifulsoup4"
        ) from exc
    return BeautifulSoup


async def crawl_with_crawl4ai(url: str) -> str:
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError as exc:
        raise RuntimeError("crawl4ai is not installed") from exc
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)
        return result.html


def fetch_html(url: str, user_agent: str, timeout: int, delay: float) -> str:
    try:
        html = asyncio.run(crawl_with_crawl4ai(url))
        if html:
            time.sleep(delay)
            return html
    except Exception as exc:
        LOGGER.debug("crawl4ai fallback for %s: %s", url, exc)
    try:
        import requests
    except ImportError as exc:
        raise SystemExit(
            "Detail mode requires crawl4ai or requests. Install one of them with: "
            "pip install crawl4ai requests"
        ) from exc
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    response.raise_for_status()
    time.sleep(delay)
    return response.text


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def find_text_with_regex(soup, pattern: str) -> str:
    node = soup.find(string=re.compile(pattern, re.IGNORECASE))
    return clean_text(node) if node else ""


def parse_author_papers_from_soup(soup) -> List[Dict[str, str]]:
    papers: List[Dict[str, str]] = []
    for div in soup.find_all("div", class_=lambda cls: cls and "trow" in cls and "abs" in cls):
        title_tag = div.select_one("h3 a.title")
        if not title_tag:
            continue
        note_spans = div.select("div.note.note-list span")
        authors_block = div.select_one("div.authors-list")
        authors = clean_text(authors_block.get_text(" ", strip=True)) if authors_block else ""
        downloads_block = div.find("div", class_="downloads")
        citations_block = div.find("div", class_="citations")
        download_text = clean_text(downloads_block.get_text(" ", strip=True)) if downloads_block else ""
        citations_text = clean_text(citations_block.get_text(" ", strip=True)) if citations_block else ""
        papers.append(
            {
                "TitleIn": clean_text(title_tag.get_text(" ", strip=True)),
                "NoteList": ", ".join(clean_text(span.get_text(" ", strip=True)) for span in note_spans),
                "AuthorName": authors,
                "DownLoadNumIn": download_text,
                "CitationNumIn": citations_text,
            }
        )
    return papers


def parse_author_profile(author_url: str, user_agent: str, timeout: int, delay: float) -> Dict[str, object]:
    BeautifulSoup = require_bs4()
    html = fetch_html(author_url, user_agent, timeout, delay)
    soup = BeautifulSoup(html, "html.parser")
    affiliations: List[str] = []
    for block in soup.find_all("div", class_="block-quote"):
        org = block.find("h2")
        info_block = block.find("div", class_="info")
        role = ""
        if info_block and info_block.find("h4"):
            role = clean_text(info_block.find("h4").get_text(" ", strip=True))
        if org:
            org_text = clean_text(org.get_text(" ", strip=True))
            affiliations.append(f"{org_text}, {role}" if role else org_text)

    scholarly_papers = ""
    label = soup.find("span", class_="lbl", string=re.compile("SCHOLARLY PAPERS", re.IGNORECASE))
    if label:
        next_h1 = label.find_next("h1")
        scholarly_papers = clean_text(next_h1.get_text(" ", strip=True)) if next_h1 else ""

    total_citations = ""
    main_text = clean_text(soup.get_text(" ", strip=True))
    citations_match = re.search(r"TOTAL CITATIONS\s+([0-9,]+)", main_text, re.IGNORECASE)
    if citations_match:
        total_citations = citations_match.group(1)

    return {
        "affiliations": affiliations,
        "scholarly_papers": scholarly_papers or "N/A",
        "total_citations": total_citations or "N/A",
        "papers": parse_author_papers_from_soup(soup),
    }


def parse_ssrn_paper(
    paper_url: str,
    user_agent: str,
    timeout: int,
    delay: float,
) -> Dict[str, object]:
    BeautifulSoup = require_bs4()
    html = fetch_html(paper_url, user_agent, timeout, delay)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("h1")
    title = clean_text(title_tag.get_text(" ", strip=True)) if title_tag else "N/A"

    posted_text = ""
    for text_node in soup.stripped_strings:
        if text_node.startswith("Posted:"):
            posted_text = clean_text(text_node)
            break

    keywords = "N/A"
    abstract = "N/A"
    for paragraph in soup.find_all("p"):
        text = clean_text(paragraph.get_text(" ", strip=True))
        if text.lower().startswith("keywords:"):
            keywords = text.split(":", 1)[1].strip() or "N/A"
        if text.lower().startswith("abstract"):
            parts = text.split(" ", 1)
            abstract = parts[1].strip() if len(parts) > 1 else "N/A"
    if abstract == "N/A":
        abstract_block = soup.find("div", id=re.compile("abstract", re.IGNORECASE))
        if abstract_block:
            abstract = clean_text(abstract_block.get_text(" ", strip=True))

    authors: List[Dict[str, object]] = []
    author_ids: List[str] = []
    author_names: List[str] = []
    institutions: List[str] = []
    for link in soup.select('a[href*="per_id="]'):
        href = link.get("href", "")
        author_name = clean_text(link.get_text(" ", strip=True))
        if not author_name or not href:
            continue
        author_match = re.search(r"per_id=(\d+)", href)
        author_id = author_match.group(1) if author_match else "N/A"
        author_url = href if href.startswith("http") else f"https://papers.ssrn.com{href}"
        author_profile = parse_author_profile(author_url, user_agent, timeout, delay)
        authors.append(
            {
                "id": author_id,
                "name": author_name,
                "Affiliations": author_profile["affiliations"],
                "ScholarlyPapers": author_profile["scholarly_papers"],
                "TotalCitations": author_profile["total_citations"],
                "AuthorPaper": author_profile["papers"],
            }
        )
        author_ids.append(author_id)
        author_names.append(author_name)
        institutions.extend(author_profile["affiliations"])

    stat_values = {
        "AbstractViews": "N/A",
        "Downloads": "N/A",
        "Rank": "N/A",
        "References": "N/A",
        "Citations": "N/A",
    }
    for stat in soup.find_all("div", class_=re.compile(r"\bstat\b")):
        label = stat.find("div", class_=re.compile(r"\blbl\b"))
        number = stat.find("div", class_=re.compile(r"\bnumber\b"))
        if not label or not number:
            continue
        label_text = clean_text(label.get_text(" ", strip=True)).lower()
        number_text = clean_text(number.get_text(" ", strip=True))
        if "abstract views" in label_text:
            stat_values["AbstractViews"] = number_text
        elif "downloads" in label_text:
            stat_values["Downloads"] = number_text
        elif "rank" in label_text:
            stat_values["Rank"] = number_text

    references_anchor = soup.find("a", href="#paper-references-widget")
    citations_anchor = soup.find("a", href="#paper-citations-widget")
    if references_anchor and references_anchor.find("span"):
        stat_values["References"] = clean_text(references_anchor.find("span").get_text(" ", strip=True))
    if citations_anchor and citations_anchor.find("span"):
        stat_values["Citations"] = clean_text(citations_anchor.find("span").get_text(" ", strip=True))

    return {
        "Title_Scraped": title,
        "PostTime_Scraped": posted_text or "N/A",
        "Abstract": abstract,
        "Keywords": keywords,
        "AuthorIDs": ";".join(author_ids),
        "AuthorNames": "; ".join(author_names),
        "Institutions": "; ".join(sorted(set(institutions))) if institutions else "N/A",
        "Authors": authors,
        **stat_values,
    }


def merge_author_records(author_records: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    merged: Dict[str, Dict[str, object]] = {}
    for author in author_records:
        author_id = str(author.get("id", "N/A"))
        current = merged.setdefault(
            author_id,
            {
                "AuthorID": author_id,
                "Author": author.get("name", "N/A"),
                "Affiliations": author.get("Affiliations", []),
                "ScholarlyPapers": author.get("ScholarlyPapers", "N/A"),
                "TotalCitations": author.get("TotalCitations", "N/A"),
                "AuthorPaper": [],
            },
        )
        current["AuthorPaper"].extend(author.get("AuthorPaper", []))
        affiliations = list(current.get("Affiliations", []))
        affiliations.extend(author.get("Affiliations", []))
        current["Affiliations"] = sorted(dict.fromkeys(affiliations))
    return list(merged.values())


def detail_mode(args: argparse.Namespace, output_dir: Path) -> None:
    paper_rows = read_csv_rows(args.input)
    if not paper_rows:
        raise SystemExit(f"No paper rows found in {args.input}")

    detail_rows: List[Dict[str, object]] = []
    author_records: List[Dict[str, object]] = []
    for row in paper_rows:
        paper_url = row.get("PaperURL", "")
        if not paper_url:
            LOGGER.warning("Skip paper row without PaperURL: %s", row)
            continue
        LOGGER.info("Paper detail: %s", paper_url)
        try:
            paper_data = parse_ssrn_paper(paper_url, args.user_agent, args.timeout, args.delay)
        except Exception as exc:  # pragma: no cover - network shape is runtime dependent
            LOGGER.warning("Failed to parse %s: %s", paper_url, exc)
            continue
        detail_rows.append(
            {
                "Field": row.get("Field", ""),
                "Area": row.get("Area", ""),
                "Category": row.get("Category", ""),
                "Title": row.get("Title", ""),
                "PostTime": row.get("PostTime", ""),
                "PaperURL": paper_url,
                **{key: value for key, value in paper_data.items() if key != "Authors"},
            }
        )
        author_records.extend(paper_data["Authors"])

    detail_rows = merge_unique_rows(detail_rows, ["PaperURL"])
    author_rows = merge_author_records(author_records)
    write_csv(
        output_dir / "paper_detail.csv",
        detail_rows,
        [
            "Field",
            "Area",
            "Category",
            "Title",
            "PostTime",
            "PaperURL",
            "Title_Scraped",
            "PostTime_Scraped",
            "Abstract",
            "Keywords",
            "AbstractViews",
            "Downloads",
            "Rank",
            "References",
            "Citations",
            "AuthorIDs",
            "AuthorNames",
            "Institutions",
        ],
    )
    with open(output_dir / "author_info.json", "w", encoding="utf-8") as handle:
        json.dump(author_rows, handle, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    output_dir = ensure_output_dir(args.output)
    if args.mode == "list":
        list_mode(args, output_dir)
        return 0
    if args.mode == "detail":
        detail_mode(args, output_dir)
        return 0

    list_output = list_mode(args, output_dir)
    args = argparse.Namespace(**vars(args))
    args.input = str(list_output)
    detail_mode(args, output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
