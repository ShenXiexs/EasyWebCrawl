import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
import re
from datetime import datetime
import time
from retrying import retry

@retry(wait_random_min=200, wait_random_max=500, stop_max_attempt_number=10000)
def get_total_pages(soup):
    pagination_summary = soup.find('span', class_='pagination__item pagination__summary')
    if pagination_summary:
        match = re.search(r'of (\d+)', pagination_summary.get_text(strip=True))
        if match:
            return int(match.group(1))
    return 1

def get_contests_from_page(soup):
    contests = []
    for item in soup.find_all('div', class_='content-listing__item'):
        link = item.find('a', class_='listing-details__title__link') or \
               item.find('a', class_='content-listing__item__link-overlay')
        if not link:
            continue

        href = link['href']
        contest_url = f"https://99designs.hk{href}/entries?groupby=designer"
        cidm = re.search(r'contests/[^/]+-(\d+)', href)
        contest_id = int(cidm.group(1)) if cidm else None
        name = link.get_text(strip=True)

        reward_tag = item.find('div', class_='ribbon__text')
        reward = reward_tag.get_text(strip=True) if reward_tag else None

        tags = []
        blind = 0
        for sec in item.find_all('div', class_='listing-details__section'):
            for span in sec.find_all('span', class_='listing-details__pill'):
                t = span.get_text(strip=True)
                tags.append(t)
                if t.lower() == 'blind':
                    blind = 1

        current_ideas = 0
        for stat in item.find_all('div', class_='listing-details__stat-item'):
            lbl = stat.find('span', class_='listing-details__stat__label')
            if lbl:
                m = re.search(r'(\d+)\s+designs', lbl.get_text(strip=True))
                if m:
                    current_ideas = int(m.group(1))

        contests.append({
            'ContestID': contest_id,
            'ContestURL': contest_url,
            'ContestName': name,
            'Reward': reward,
            'Blind': blind,
            'Tags': ','.join(tags) if tags else None,
            'CurrentIdeas': current_ideas,
            'CrawlTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    return contests

def get_next_page_url(soup):
    nxt = soup.find('span', class_='pagination__item pagination--next')
    if nxt and (a := nxt.find('a', class_='pagination__button')):
        href = a.get('href')
        if href:
            return f"https://99designs.hk{href}"
    return None

def scrape_contests(base_url, output_dir, max_pages=100):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/70.0.3538.25 Safari/537.36'
    }

    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = os.path.join(output_dir, f"Contest_URL_{timestamp}.csv")

    current_url = base_url
    total_pages = None
    current_page = 1

    while current_url:
        if current_page > max_pages:
            print(f"Reached max_pages={max_pages}, stopping.")
            break

        print(f"[Page {current_page}] GET {current_url}")
        # --- 加入三次重试，每次失败间隔 2 秒 ---
        for attempt in range(1, 4):
            try:
                resp = requests.get(current_url, headers=headers, timeout=10)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, 'lxml')
                break  # 成功就跳出重试循环
            except Exception as e:
                print(f"  Attempt {attempt} failed: {e}")
                if attempt < 3:
                    time.sleep(2)
                else:
                    print("  Failed 3 times, skipping this page.")
                    soup = None
        if soup is None:
            # 跳到下一页或退出
            next_url = get_next_page_url(BeautifulSoup('', 'lxml'))
            if next_url:
                current_url = next_url
                current_page += 1
                continue
            else:
                break
        # -----------------------------------------

        if total_pages is None:
            total_pages = get_total_pages(soup)
            print(f"Total pages reported: {total_pages}")

        contests = get_contests_from_page(soup)
        print(f"  → Retrieved {len(contests)} contests")

        df_page = pd.DataFrame(contests)
        df_page.sort_values('ContestID', ascending=False, inplace=True)
        df_page.to_csv(
            csv_path,
            mode='a',
            header=not os.path.exists(csv_path),
            index=False,
            encoding='utf-8'
        )
        print(f"  → Appended page {current_page} data to CSV")

        next_url = get_next_page_url(soup)
        if not next_url:
            print("No more pages found, exiting.")
            break

        current_url = next_url
        current_page += 1

    print(f"Done. Data saved to {csv_path}")

if __name__ == "__main__":
    url = ("https://99designs.hk/logo-design/contests?"
           "sort=start-date%3Adesc&status=won&entry-level=0&"
           "mid-level=0&top-level=0&guaranteed=only&language=en&"
           "dir=desc&order=start-date")
    out_dir = "/Users/samxie/Research/CrowdDeleRej/Data/ContestList"
    scrape_contests(url, out_dir, max_pages=500)
