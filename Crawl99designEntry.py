import pandas as pd
from retrying import retry
import json
import requests
from bs4 import BeautifulSoup
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import random
import logging
from requests.exceptions import RequestException, ProxyError, ConnectionError, Timeout, HTTPError, SSLError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 设置日志
logger = logging.getLogger(__name__)

def create_robust_session():
    """创建一个具有重试机制的requests session"""
    session = requests.Session()
    
    # 配置重试策略
    retry_strategy = Retry(
        total=5,  # 总重试次数
        backoff_factor=1,  # 退避因子
        status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的HTTP状态码
        allowed_methods=["HEAD", "GET", "OPTIONS"]  # 允许重试的HTTP方法
    )
    
    # 创建HTTP适配器
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # 设置超时
    session.timeout = 30
    
    return session

class WAFChallengeError(Exception):
    """Raised when AWS WAF challenge page is detected."""
    pass

def _looks_like_waf(html_text: str) -> bool:
    """检测返回内容是否是 AWS WAF 挑战页"""
    if not html_text:
        return False
    t = html_text.lower()
    # 这些标记来自你贴出来的 challenge 页
    return (
        'token.awswaf.com' in t or
        'challenge.js' in t or
        'id="challenge-container"' in t or
        "we need to verify that you're not a robot" in t
    )

def safe_request(session, url, cookies, headers, max_retries=5, base_delay=2, waf_max_retries=5):
    """
    安全的HTTP请求函数，专门处理网络连接问题 + AWS WAF 挑战识别
    - 网络类异常：最多重试 max_retries 次
    - WAF 挑战：最多重试 waf_max_retries 次（与 max_retries 独立）
    """
    retryable_exceptions = (
        ProxyError, 
        ConnectionError, 
        Timeout, 
        HTTPError,
        SSLError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ConnectionError,
        RequestException,
        WAFChallengeError,  # 把 WAF 也视为可重试
    )

    waf_attempts = 0

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.5, 1.5)
                delay = min(delay, 30)
                logger.warning(f"请求重试 #{attempt + 1}, 等待 {delay:.1f} 秒... -> {url}")
                time.sleep(delay)

            response = session.get(url, cookies=cookies, headers=headers, timeout=30)
            response.raise_for_status()

            # —— 新增：检测是否为 WAF 挑战页
            if _looks_like_waf(response.text):
                waf_attempts += 1
                logger.error(
                    f"检测到 AWS WAF 挑战页 (第 {waf_attempts}/{waf_max_retries} 次): {url} | 片段: {response.text[:120]!r}"
                )
                if waf_attempts >= waf_max_retries:
                    # 到达 WAF 上限，终止
                    raise WAFChallengeError(f"AWS WAF challenge persisted after {waf_max_retries} attempts for {url}")
                # 未达上限，继续走下一轮重试（通过抛异常进入 except 分支）
                raise WAFChallengeError("AWS WAF challenge (will retry)")

            return response

        except retryable_exceptions as e:
            # 特别处理连接重置
            if isinstance(e, (ConnectionError, RequestException)) and (
                "Connection reset by peer" in str(e) or "ConnectionResetError" in str(e)
            ):
                logger.warning("检测到连接重置，增加额外延迟...")
                time.sleep(random.uniform(3, 8))

            logger.warning(f"请求失败 (尝试 {attempt + 1}/{max_retries}): {url}")
            logger.warning(f"错误类型: {type(e).__name__}, 详情: {str(e)}")

            if attempt == max_retries - 1:
                logger.error(f"所有重试均失败: {url}")
                raise
        except Exception as e:
            logger.error(f"不可重试的错误: {url}, {type(e).__name__}: {str(e)}")
            raise
    raise RequestException(f"请求失败，已重试 {max_retries} 次: {url}")

# 改进的重试装饰器，专门处理代理错误
@retry(
    retry_on_exception=lambda x: isinstance(x, (ProxyError, ConnectionError, Timeout, SSLError)),
    wait_random_min=2000, 
    wait_random_max=8000, 
    stop_max_attempt_number=15
)
def get_real_image_url(entry_url, cookies, headers, session):
    """Fetch the real image URL and creation time by visiting the entry page."""
    response = safe_request(session, entry_url, cookies, headers)
    soup = BeautifulSoup(response.text, 'lxml')

    # Find the real image URL in the <link rel="image_src"> tag
    image_link_tag = soup.find('link', rel='image_src')
    image_url = image_link_tag.get('href') if image_link_tag else None

    # Find the creation time in the page's JavaScript data
    time_created_match = re.search(r'"timeCreatedString":"([^"]+)"', response.text)
    create_time = time_created_match.group(1) if time_created_match else 'N/A'

    if not image_url:
        logger.warning(f"No image found on page: {entry_url}")
    return image_url, create_time

@retry(
    retry_on_exception=lambda x: isinstance(x, (ProxyError, ConnectionError, Timeout, SSLError)),
    wait_random_min=1000, 
    wait_random_max=3000, 
    stop_max_attempt_number=10
)
def get_total_pages(url, cookies, headers, session):
    """Fetch the total number of pages from the pagination summary."""
    response = safe_request(session, url, cookies, headers)
    soup = BeautifulSoup(response.text, 'lxml')

    # Find the pagination summary element
    pagination_summary = soup.find('span', class_='pagination__summary')
    if pagination_summary:
        # Extract the total number of pages using regex
        total_pages_text = pagination_summary.get_text()
        match = re.search(r'of (\d+)', total_pages_text)
        if match:
            return int(match.group(1))  # Return the total number of pages
        else:
            logger.warning("Could not extract total pages.")
            return 1
    else:
        logger.warning("No pagination summary found.")
        return 1  # If no pagination is found, assume it's a single page

def get_contest_id(url):
    """Extract Contest ID from the URL."""
    match = re.search(r'contests/[^/]+-(\d+)/entries', url)
    if match:
        return match.group(1)
    else:
        return 'N/A'

def extract_rating(entry):
    """Extract the rating value from the star rating system."""
    rating_tag = entry.find('input', {'checked': 'checked'})
    if rating_tag and 'value' in rating_tag.attrs:
        return rating_tag['value']
    return 'N/A'  # Return N/A if no rating found

def extract_winner(entry):
    """Check if the entry has the 'winner' status."""
    winner_tag = entry.find('div', {'data-entry-status': 'winner'})
    return 1 if winner_tag else 0

@retry(
    retry_on_exception=lambda x: isinstance(x, (ProxyError, ConnectionError, Timeout, SSLError)),
    wait_random_min=2000, 
    wait_random_max=5000, 
    stop_max_attempt_number=10
)
def get_brief_info(brief_url, cookies, headers, contest_id, output_dir, session):
    """Fetch the brief page with improved error handling."""
    response = safe_request(session, brief_url, cookies, headers)
    soup = BeautifulSoup(response.text, 'lxml')

    # Extract the purchasePrice and packageName from the brief page
    header_price_data = soup.find('div', id='header-price-data')
    if header_price_data and 'data-initial-props' in header_price_data.attrs:
        data_props_str = header_price_data['data-initial-props']
        try:
            data_props = json.loads(data_props_str.replace('&quot;', '"'))
            price_usd = data_props.get('purchasePrice', 'N/A').replace('US$', '')
            package_level = data_props.get('packageName', 'N/A')
        except json.JSONDecodeError:
            logger.error("Error decoding JSON from data-initial-props")
            price_usd, package_level = 'N/A', 'N/A'
    else:
        price_usd, package_level = 'N/A', 'N/A'

    # Extract Guarantee information
    guarantee_tag = soup.find('div', {'data-meta-guarantee-tooltip-content': True})
    guarantee = 1 if guarantee_tag and "The client has guaranteed to award the prize." in guarantee_tag.text else 0

    # Extract FastTrack information
    fasttrack_tag = soup.find('div', text=re.compile(
        r'Following the open round, the client will select a winning design. There is no refinement stage.'))
    fasttrack = 1 if fasttrack_tag else 0

    # Extract Industry information
    industry_match = re.search(r'industry&quot;:\{&quot;value&quot;:&quot;([a-zA-Z]+)&quot;', response.text)
    industry = industry_match.group(1) if industry_match else 'N/A'

    # Extract Blind information
    blind_tag = soup.find('span', class_='meta-item__label', text='Blind')
    blind = 1 if blind_tag else 0

    # Extract OtherNote information
    notes_match = re.search(r'notes&quot;:\{&quot;value&quot;:&quot;(.*?)&quot;', response.text)
    other_note = notes_match.group(1).replace('&quot;', '"') if notes_match else 'N/A'

    # Extract design style attributes
    style_pattern = re.compile(
        r'&quot;(classicModern|matureYouthful|feminineMasculine|playfulSophisticated|economicalLuxurious|geometricOrganic|abstractLiteral)&quot;:(-?\d)')
    style_values = {
        "classicModern": 'N/A',
        "matureYouthful": 'N/A',
        "feminineMasculine": 'N/A',
        "playfulSophisticated": 'N/A',
        "economicalLuxurious": 'N/A',
        "geometricOrganic": 'N/A',
        "abstractLiteral": 'N/A'
    }

    style_matches = style_pattern.findall(response.text)
    for match in style_matches:
        attribute, value = match
        style_values[attribute] = value

    # Extract the publicId for the image download
    public_id_matches = re.findall(r'&quot;publicId&quot;:&quot;([a-zA-Z0-9]+)&quot;', response.text)
    if not public_id_matches:
        logger.warning(f"No images found for contest {contest_id}.")
        return price_usd, package_level, guarantee, fasttrack, industry, blind, other_note, 0, 0, style_values

    # Check for reference public IDs
    references_section = re.search(
        r'References&quot;,&quot;elements&quot;:\{&quot;attachments&quot;:\{&quot;value&quot;:\[\{&quot;publicId&quot;:&quot;([a-zA-Z0-9]+)&quot;',
        response.text)
    references_public_ids = references_section.group(1) if references_section else None

    # Create RefImage folder if it doesn't exist
    ref_image_dir = os.path.join(output_dir, 'RefImage')
    os.makedirs(ref_image_dir, exist_ok=True)

    # Track inspiration and reference counts
    inspiration_count, reference_count = 0, 0

    # Load progress from the previous download attempt
    progress_file = os.path.join(output_dir, f'{contest_id}_download_progress.txt')
    if os.path.exists(progress_file):
        with open(progress_file, 'r') as f:
            downloaded_images = set(f.read().splitlines())
    else:
        downloaded_images = set()

    # Download images with logic and improved error handling
    for idx, public_id in enumerate(public_id_matches, start=1):
        if public_id in downloaded_images:
            logger.info(f"Skipping already downloaded image: {public_id}")
            continue

        # Determine the image type and filename
        if references_public_ids and public_id == references_public_ids:
            img_filename = f"Ref_{idx}_{contest_id}.png"
            reference_count += 1
        else:
            img_filename = f"Inspiration_{idx}_{contest_id}.png"
            inspiration_count += 1

        # Construct the download URL and image path
        download_url = f"https://99designs.hk/contests/{contest_id}/brief/download/{public_id}"
        img_filepath = os.path.join(ref_image_dir, img_filename)

        # Download the image with improved retry logic
        download_success = False
        for attempt in range(8):  # 增加重试次数
            try:
                # 使用safe_request函数下载图片
                img_response = safe_request(session, download_url, cookies, headers, max_retries=3)
                
                with open(img_filepath, 'wb') as img_file:
                    img_file.write(img_response.content)
                
                logger.info(f"Image {img_filename} downloaded successfully: {download_url}")
                
                # After successful download, append to progress file
                with open(progress_file, 'a') as f:
                    f.write(public_id + '\n')
                
                download_success = True
                break  # Exit retry loop on success
                
            except Exception as e:
                logger.warning(f"Failed to download image {img_filename} (attempt {attempt + 1}/8): {e}")
                
                if attempt < 7:  # 还有重试机会
                    delay = (2 ** attempt) + random.uniform(1, 3)
                    delay = min(delay, 30)  # 最大延迟30秒
                    time.sleep(delay)
                else:
                    logger.error(f"Aborting download for image {img_filename} after {attempt + 1} attempts.")
                    break
        
        if not download_success:
            logger.error(f"Failed to download image {img_filename}, continuing with next image...")

    return price_usd, package_level, guarantee, fasttrack, industry, blind, other_note, inspiration_count, reference_count, style_values

@retry(
    retry_on_exception=lambda x: isinstance(x, (ProxyError, ConnectionError, Timeout, SSLError)),
    wait_random_min=1500, 
    wait_random_max=4000, 
    stop_max_attempt_number=8
)

def get_user_profile_info(user_url, cookies_user, headers_user, session):
    """Fetch and extract user profile details with improved error handling."""

    response = safe_request(session, user_url, cookies_user, headers_user)
    soup = BeautifulSoup(response.text, 'lxml')
    # Extract AggregateRating and AggregateReviews
    aggregate_info = soup.find('span', itemprop='aggregateRating')
    if aggregate_info:
        rating_value = aggregate_info.find('span', itemprop='ratingValue').text.strip()
        review_count = aggregate_info.find('span', itemprop='reviewCount').text.strip()
    else:
        rating_value = 'N/A'
        review_count = 'N/A'

    # Extract StartDate
    start_date_tag = soup.find('span', class_='subtle-text', text=re.compile(r'Member since:'))
    start_date = start_date_tag.text.strip().replace('Member since:', '').strip() if start_date_tag else 'N/A'

    # Extract ContestsWon
    contests_won = soup.find('div', class_='stats-panel__item--first',
                             title=re.compile('Total number of contest prize awards'))
    contests_won = contests_won.find('div', class_='stats-panel__item__value').text.strip() if contests_won else 'N/A'

    # Extract RunnerUp
    runner_up = soup.find('div', class_='stats-panel__item',
                          title=re.compile('Total times named as a contest finalist'))
    runner_up = runner_up.find('div', class_='stats-panel__item__value').text.strip() if runner_up else 'N/A'

    # Extract OnetoOne
    oneto_one = soup.find('div', class_='stats-panel__item',
                          title=re.compile('Total number of 1-to-1 Projects completed'))
    oneto_one = oneto_one.find('div', class_='stats-panel__item__value').text.strip() if oneto_one else 'N/A'

    # Extract RepeatClients
    repeat_clients = soup.find('div', class_='stats-panel__item',
                               title=re.compile('Total number of clients who hired this designer'))
    repeat_clients = repeat_clients.find('div',
                                         class_='stats-panel__item__value').text.strip() if repeat_clients else 'N/A'

    # Extract UserTag (from a specific section, excluding languages)
    user_tags_section = soup.find('div', class_='profile__tag-section')
    if user_tags_section:
        user_tags = user_tags_section.find_all('span', class_='pill pill--tag')
        user_tag_list = [tag.text.strip() for tag in user_tags]
        user_tag = ', '.join(user_tag_list) if user_tag_list else 'N/A'
    else:
        user_tag = 'N/A'

    # Extract Languages (if the Languages section exists)
    languages_section = soup.find('h3', class_='heading heading--size4', text=re.compile(r'Languages'))
    if languages_section:
        # Look for the next 'pill-group' after the 'Languages' heading
        languages_group = languages_section.find_next('div', class_='pill-group')
        if languages_group:
            language_tags = languages_group.find_all('span', class_='pill pill--tag')
            language_list = [lang.text.strip() for lang in language_tags]
            languages = ', '.join(language_list) if language_list else 'N/A'
        else:
            languages = 'N/A'
    else:
        languages = 'N/A'

    # Extract Certifications (based on tooltip descriptions and certification tags)
    certifications = []

    # Extract all pill--certification items (e.g. 'Print', 'Brand guide')
    certification_tags = soup.find_all('span', class_='pill pill--tag pill--certification')
    for tag in certification_tags:
        certifications.append(tag.text.strip())  # Add 'Print', 'Brand guide', etc.

    # Extract certification levels (based on tooltip descriptions)
    certification_items = soup.find_all('div', class_='pill-group__item', attrs={'data-tooltip': True, 'title': True})
    for item in certification_items:
        tooltip_title = item.get('title', '')
        span_text = item.find('span', class_=re.compile('pill')).text.strip()
        if 'New or developing professionals on 99designs' in tooltip_title:
            certifications.append(span_text)  # Entry Level
        elif 'Professionals on 99designs that are skilled in the essentials of design' in tooltip_title:
            certifications.append(span_text)  # Mid Level
        elif 'Professionals that have built trust on 99designs with their expert skills and creativity' in tooltip_title:
            certifications.append(span_text)  # Top Level

    # Join all certifications with a comma
    certifications = ', '.join(certifications) if certifications else 'N/A'

    return rating_value, review_count, start_date, contests_won, runner_up, oneto_one, repeat_clients, user_tag, certifications, languages

def download_images(url, output_dir, csv_filename, nonactive=False):
    
    cookies = {
    '_99vid': '0191e08c-aa05-a7d3-e823-8d36d3f2f358',
    '_99sid': '0191e08c-aa05-a7d3-e823-8d36df73d7f0',
    'contests_session_data': 'M0hyeCn8FhAkjvoGfBF43O5iCkjCK7S1YzOiaqdOZ1t4T0njwfnZjLu%2FcpJDvSZx--cf714bd29f171fd2d620c503c027248763144d5f',
    'ajs_anonymous_id': '0191e08c-aa05-a7d3-e823-8d36df73d7f0',
    '_tt_enable_cookie': '1',
    '__ssid': '0566933993846243a707d16d8fd37fb',
    'QuantumMetricUserID': 'b550e6947216aecefa22871cb8f8f0c7',
    '__zlcmid': '1Ntn2a9zF2h7mpc',
    '99previouslyauthenticated': 'true',
    'ajs_user_id': '9411255',
    'ki_r': '',
    'IR_PI': '8cc97b74-7025-11ef-b5f5-17bb1b54210d%7C1728123903608',
    '_ttp': '66R5dF03YRZBQIjWoMJ-3_vEcRN.tt.1',
    '_fbp': 'fb.1.1756457548149.17103444377146716',
    '_gcl_au': '1.1.292253385.1756457548',
    'aws-waf-token': 'a809c0d0-7433-4e7e-91b3-bf122e6e0894:EQoAqCE+0wY7AAAA:0lkWm4LCNgHXY+3BQjK5oqOo754rZj622gczteSVDyu1/zrY3V5YiHCL4xgqtOl62gGlQtcfxml9rJW7FEVbnlgBD33RPtVP3MwZdZj9xW/7aggy1zVOWkuZDGUjvDCeicIx/l0REYsdNXjbCsBOH2bCy+iR1Acv9Lrp4oCUGxOgJJwsI4l3yDki5ES46JY=',
    '_99csrf': 'e985e77b3e66c16b5552c1d84730384e78e8c73f7cd13eab9603d635ac6f1e6f',
    '99segment_sessionidx': '65',
    '99segment_session': '*',
    '_gid': 'GA1.2.1022332047.1758531617',
    'IR_gbd': '99designs.hk',
    'IR_3172': '1758531617016%7C0%7C1758531617016%7C%7C',
    '_ga_Z6XV646S1S': 'GS2.1.s1758531617$o50$g0$t1758531617$j60$l0$h0',
    '_ga': 'GA1.1.173363251.1726049203',
    '_uetsid': '8f2972e0979211f0b14ea908de77c601',
    '_uetvid': '8c2c1350702511ef90486520c41e52ce',
    '_ga_1P5BPD7T0D': 'GS2.2.s1758531617$o45$g0$t1758531617$j60$l0$h0',
    'ttcsid': '1758531618065::UsnZgyTGZi9yh9nA3UJO.4.1758531618065.0',
    'ki_t': '1726049203587%3B1758531618226%3B1758531618226%3B20%3B304',
    'ttcsid_CE8LLDBC77U8PGLVCNBG': '1758531618065::On3jbjD39st_yQJ1BIjz.4.1758531618273.0',
    'QuantumMetricSessionID': '1c4a31049331db6c14c7e77a0c00daf0',
    '_dd_s': 'rum=0&expire=1758532744480',
    }

    headers = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
    'priority': 'u=0, i',
    'sec-ch-ua': '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"macOS"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'none',
    'sec-fetch-user': '?1',
    'upgrade-insecure-requests': '1',
    'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    # 'cookie': '_99vid=0191e08c-aa05-a7d3-e823-8d36d3f2f358; _99sid=0191e08c-aa05-a7d3-e823-8d36df73d7f0; contests_session_data=M0hyeCn8FhAkjvoGfBF43O5iCkjCK7S1YzOiaqdOZ1t4T0njwfnZjLu%2FcpJDvSZx--cf714bd29f171fd2d620c503c027248763144d5f; ajs_anonymous_id=0191e08c-aa05-a7d3-e823-8d36df73d7f0; _tt_enable_cookie=1; __ssid=0566933993846243a707d16d8fd37fb; QuantumMetricUserID=b550e6947216aecefa22871cb8f8f0c7; __zlcmid=1Ntn2a9zF2h7mpc; 99previouslyauthenticated=true; ajs_user_id=9411255; ki_r=; IR_PI=8cc97b74-7025-11ef-b5f5-17bb1b54210d%7C1728123903608; _ttp=66R5dF03YRZBQIjWoMJ-3_vEcRN.tt.1; _fbp=fb.1.1756457548149.17103444377146716; _gcl_au=1.1.292253385.1756457548; aws-waf-token=a809c0d0-7433-4e7e-91b3-bf122e6e0894:EQoAqCE+0wY7AAAA:0lkWm4LCNgHXY+3BQjK5oqOo754rZj622gczteSVDyu1/zrY3V5YiHCL4xgqtOl62gGlQtcfxml9rJW7FEVbnlgBD33RPtVP3MwZdZj9xW/7aggy1zVOWkuZDGUjvDCeicIx/l0REYsdNXjbCsBOH2bCy+iR1Acv9Lrp4oCUGxOgJJwsI4l3yDki5ES46JY=; _99csrf=e985e77b3e66c16b5552c1d84730384e78e8c73f7cd13eab9603d635ac6f1e6f; 99segment_sessionidx=65; 99segment_session=*; _gid=GA1.2.1022332047.1758531617; IR_gbd=99designs.hk; IR_3172=1758531617016%7C0%7C1758531617016%7C%7C; _ga_Z6XV646S1S=GS2.1.s1758531617$o50$g0$t1758531617$j60$l0$h0; _ga=GA1.1.173363251.1726049203; _uetsid=8f2972e0979211f0b14ea908de77c601; _uetvid=8c2c1350702511ef90486520c41e52ce; _ga_1P5BPD7T0D=GS2.2.s1758531617$o45$g0$t1758531617$j60$l0$h0; ttcsid=1758531618065::UsnZgyTGZi9yh9nA3UJO.4.1758531618065.0; ki_t=1726049203587%3B1758531618226%3B1758531618226%3B20%3B304; ttcsid_CE8LLDBC77U8PGLVCNBG=1758531618065::On3jbjD39st_yQJ1BIjz.4.1758531618273.0; QuantumMetricSessionID=1c4a31049331db6c14c7e77a0c00daf0; _dd_s=rum=0&expire=1758532744480',
    }


    cookies_user = {
    '_99vid': '0191e08c-aa05-a7d3-e823-8d36d3f2f358',
    '_99sid': '0191e08c-aa05-a7d3-e823-8d36df73d7f0',
    'ajs_anonymous_id': '0191e08c-aa05-a7d3-e823-8d36df73d7f0',
    '_tt_enable_cookie': '1',
    '__ssid': '0566933993846243a707d16d8fd37fb',
    'QuantumMetricUserID': 'b550e6947216aecefa22871cb8f8f0c7',
    '__zlcmid': '1Ntn2a9zF2h7mpc',
    '99previouslyauthenticated': 'true',
    'ajs_user_id': '9411255',
    'ki_r': '',
    'IR_PI': '8cc97b74-7025-11ef-b5f5-17bb1b54210d%7C1728123903608',
    '_ttp': '66R5dF03YRZBQIjWoMJ-3_vEcRN.tt.1',
    '_fbp': 'fb.1.1756457548149.17103444377146716',
    '_gcl_au': '1.1.292253385.1756457548',
    '_99csrf': 'e985e77b3e66c16b5552c1d84730384e78e8c73f7cd13eab9603d635ac6f1e6f',
    '99segment_sessionidx': '65',
    '99segment_session': '*',
    '_gid': 'GA1.2.1022332047.1758531617',
    'IR_gbd': '99designs.hk',
    'QuantumMetricSessionID': '1c4a31049331db6c14c7e77a0c00daf0',
    '_dd_s': 'rum=0&expire=1758532825766',
    'contests_session_id': 'b8310e63a4e8d9f77772cdfc22e9dd46',
    'contests_session_data': 'WObL4K52ke%2BMSGlSiyUX%2BbxI%2BaEUo3TQ63U0WxgCU%2Bb8xddyAUKviKZeXYGihvHd--8a36177a9d8d427689bdc6fb01b8e20f264a1cdd',
    '_dc_gtm_UA-3528448-37': '1',
    '_dc_gtm_UA-3528448-33': '1',
    'IR_3172': '1758531931030%7C127707%7C1758531931030%7C%7C',
    '_ga': 'GA1.1.173363251.1726049203',
    '_uetsid': '8f2972e0979211f0b14ea908de77c601',
    '_uetvid': '8c2c1350702511ef90486520c41e52ce',
    '_ga_1P5BPD7T0D': 'GS2.2.s1758531617$o45$g1$t1758531931$j60$l0$h0',
    'ttcsid': '1758531618065::UsnZgyTGZi9yh9nA3UJO.4.1758531932014.0',
    'ki_t': '1726049203587%3B1758531618226%3B1758531932165%3B20%3B306',
    'ttcsid_CE8LLDBC77U8PGLVCNBG': '1758531618065::On3jbjD39st_yQJ1BIjz.4.1758531936481.0',
    '_ga_Z6XV646S1S': 'GS2.1.s1758531617$o50$g1$t1758531937$j52$l0$h0',
    'aws-waf-token': 'a809c0d0-7433-4e7e-91b3-bf122e6e0894:EQoApWQ+1FhZAAAA:MMiIVZM0YAHcbi6+egoVCyeW+ZI4uvvHlIiJfoHiJHf0aWCk251Perg1IHiXxALFp66OzJRrQMOO4i0ZeyYlyvFKs1EB/4kSY7HjaQAnXRey0jrS0P7obQXniM442ZrQrf+O87db3mgGh+YwEptPH2SlKysEFjnZtzwHwH3UbH8wReugYH2DqbElugO8K18=',
    }

    headers_user = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
    'priority': 'u=0, i',
    'referer': 'https://99designs.hk/profiles/flaviogc',
    'sec-ch-ua': '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"macOS"',
    'sec-fetch-dest': 'document',
    'sec-fetch-mode': 'navigate',
    'sec-fetch-site': 'same-origin',
    'upgrade-insecure-requests': '1',
    'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
    # 'cookie': '_99vid=0191e08c-aa05-a7d3-e823-8d36d3f2f358; _99sid=0191e08c-aa05-a7d3-e823-8d36df73d7f0; ajs_anonymous_id=0191e08c-aa05-a7d3-e823-8d36df73d7f0; _tt_enable_cookie=1; __ssid=0566933993846243a707d16d8fd37fb; QuantumMetricUserID=b550e6947216aecefa22871cb8f8f0c7; __zlcmid=1Ntn2a9zF2h7mpc; 99previouslyauthenticated=true; ajs_user_id=9411255; ki_r=; IR_PI=8cc97b74-7025-11ef-b5f5-17bb1b54210d%7C1728123903608; _ttp=66R5dF03YRZBQIjWoMJ-3_vEcRN.tt.1; _fbp=fb.1.1756457548149.17103444377146716; _gcl_au=1.1.292253385.1756457548; _99csrf=e985e77b3e66c16b5552c1d84730384e78e8c73f7cd13eab9603d635ac6f1e6f; 99segment_sessionidx=65; 99segment_session=*; _gid=GA1.2.1022332047.1758531617; IR_gbd=99designs.hk; QuantumMetricSessionID=1c4a31049331db6c14c7e77a0c00daf0; _dd_s=rum=0&expire=1758532825766; contests_session_id=b8310e63a4e8d9f77772cdfc22e9dd46; contests_session_data=WObL4K52ke%2BMSGlSiyUX%2BbxI%2BaEUo3TQ63U0WxgCU%2Bb8xddyAUKviKZeXYGihvHd--8a36177a9d8d427689bdc6fb01b8e20f264a1cdd; _dc_gtm_UA-3528448-37=1; _dc_gtm_UA-3528448-33=1; IR_3172=1758531931030%7C127707%7C1758531931030%7C%7C; _ga=GA1.1.173363251.1726049203; _uetsid=8f2972e0979211f0b14ea908de77c601; _uetvid=8c2c1350702511ef90486520c41e52ce; _ga_1P5BPD7T0D=GS2.2.s1758531617$o45$g1$t1758531931$j60$l0$h0; ttcsid=1758531618065::UsnZgyTGZi9yh9nA3UJO.4.1758531932014.0; ki_t=1726049203587%3B1758531618226%3B1758531932165%3B20%3B306; ttcsid_CE8LLDBC77U8PGLVCNBG=1758531618065::On3jbjD39st_yQJ1BIjz.4.1758531936481.0; _ga_Z6XV646S1S=GS2.1.s1758531617$o50$g1$t1758531937$j52$l0$h0; aws-waf-token=a809c0d0-7433-4e7e-91b3-bf122e6e0894:EQoApWQ+1FhZAAAA:MMiIVZM0YAHcbi6+egoVCyeW+ZI4uvvHlIiJfoHiJHf0aWCk251Perg1IHiXxALFp66OzJRrQMOO4i0ZeyYlyvFKs1EB/4kSY7HjaQAnXRey0jrS0P7obQXniM442ZrQrf+O87db3mgGh+YwEptPH2SlKysEFjnZtzwHwH3UbH8wReugYH2DqbElugO8K18=',
    }

    # 创建具有重试机制的session
    session = create_robust_session()
    
    contest_id = get_contest_id(url)
    brief_url = url.replace('/entries', '/brief')
    
    logger.info(f"开始处理比赛 {contest_id}...")
    
    try:
        # 获取比赛基本信息
        price_usd, package_level, guarantee, fasttrack, industry, blind, other_note, inspiration_count, reference_count, style_values = get_brief_info(
            brief_url, cookies, headers, contest_id, output_dir, session)
        
        # 获取总页数
        total_pages = get_total_pages(url, cookies, headers, session)
        logger.info(f"总页数: {total_pages}")

    except Exception as e:
        logger.error(f"获取比赛基本信息失败: {contest_id}, 错误: {e}")
        raise

    csv_data = []
    successful_entries = set()
    nonactive = nonactive
    
    def fetch_and_download(page):
        if nonactive:
            page_url = f"{url}&page={page}"
        else:
            page_url = f"{url}?page={page}"
        
        logger.info(f"正在处理第 {page}/{total_pages} 页: {page_url}")

        # 使用safe_request获取页面
        try:
            response = safe_request(session, page_url, cookies, headers, max_retries=8)
        except Exception as e:
            logger.error(f"获取页面失败: {page_url}, 跳过此页. 错误: {e}")
            return []

        soup = BeautifulSoup(response.text, 'lxml')
        owner_tags = soup.find_all(class_=lambda c: c in ['entry-owner__id', 'entry-owner__id-link'])
        entry_divs = set()
        for tag in owner_tags:
            entry_div = tag.find_parent('div', class_=re.compile(r'^entry\b'))
            if entry_div:
                entry_divs.add(entry_div)
        entries = list(entry_divs)
        
        if not entries:
            logger.warning(f"第 {page} 页没有找到图片条目，跳过...")
            return []

        page_data = []
        visited_authors = {}  # 页面级别的用户缓存
        
        for entry in entries:
            # 添加随机延迟
            time.sleep(random.uniform(0.8, 2.0))
            
            entry_number = entry.get('id').split('-')[1]
            if entry_number in successful_entries:
                logger.info(f"条目 {entry_number} 已处理，跳过...")
                continue

            try:
                # 提取状态信息
                status = None
                status_overlays = entry.find('div', class_='entry__image__status-overlay')
                if status_overlays:
                    status_divs = status_overlays.find_all('div', class_='entry-status-overlay')
                    for div in status_divs:
                        if not div.has_attr('data-hidden'):
                            title_span = div.find('span', class_='entry-status-overlay__title')
                            if title_span:
                                status = title_span.get_text(strip=True)
                            break

                user_id = entry.get('data-user-id')
                design_id = entry.get('data-design-id')
                designer_tag = entry.find('a', class_='entry-owner__designer-name-link')
                if designer_tag:
                    base_href = designer_tag.get('href')
                    user_url = f"https://99designs.hk{base_href.rstrip('/')}/about"
                else:
                    user_url = 'N/A'
                user_name = designer_tag.text.strip() if designer_tag else 'N/A'

                # 获取用户信息（使用缓存）
                if user_url in visited_authors:
                    profile_data = visited_authors[user_url]
                else:
                    try:
                        profile_data = get_user_profile_info(user_url, cookies_user, headers_user, session)
                        visited_authors[user_url] = profile_data
                    except Exception as e:
                        logger.warning(f"获取用户信息失败: {user_url}, 错误: {e}")
                        profile_data = ['N/A'] * 10
                        visited_authors[user_url] = profile_data

                rating_value, review_count, start_date, contests_won, runner_up, oneto_one, repeat_clients, user_tag, certifications, languages = profile_data

                # 获取图片信息
                real_image_url = 'N/A'
                create_time = 'N/A'
                entry_link_tag = entry.find('a', class_='entry__image__inner')
                
                if entry_link_tag:
                    full_entry_url = f"https://99designs.hk{entry_link_tag.get('href')}"
                    
                    try:
                        real_image_url, create_time = get_real_image_url(full_entry_url, cookies, headers, session)
                    except Exception as e:
                        logger.warning(f"获取图片URL失败: {full_entry_url}, 错误: {e}")

                    # 下载图片
                    if real_image_url and real_image_url != 'N/A':
                        img_base_name = f'{entry_number}_{user_id}_entry'
                        if any(f.startswith(img_base_name) for f in os.listdir(output_dir)):
                            logger.info(f"图片 {img_base_name} 已存在，跳过下载")
                        else:
                            img_name = f'{img_base_name}.png'
                            img_path = os.path.join(output_dir, img_name)
                            os.makedirs(output_dir, exist_ok=True)

                            # 下载图片，使用重试机制
                            download_success = False
                            for attempt in range(6):
                                try:
                                    img_response = safe_request(session, real_image_url, cookies, headers, max_retries=3)
                                    with open(img_path, 'wb') as img_file:
                                        img_file.write(img_response.content)
                                    logger.info(f"图片 {img_name} 下载成功")
                                    download_success = True
                                    break
                                except Exception as e:
                                    logger.warning(f"图片下载失败 (尝试 {attempt + 1}/6): {real_image_url}, 错误: {e}")
                                    if attempt < 5:
                                        time.sleep(random.uniform(2, 5))
                            
                            if not download_success:
                                logger.error(f"图片下载最终失败: {img_name}")

                successful_entries.add(entry_number)
                rating = extract_rating(entry)
                winner_status = extract_winner(entry)
                
                page_data.append([
                    contest_id, price_usd, package_level, guarantee, blind, fasttrack, industry, other_note,
                    inspiration_count, reference_count, create_time, design_id, entry_number, rating,
                    winner_status, real_image_url, user_id, user_name, user_url, rating_value, review_count,
                    start_date, contests_won, runner_up, oneto_one, repeat_clients, user_tag, certifications,
                    languages, style_values['classicModern'], style_values['matureYouthful'],
                    style_values['feminineMasculine'], style_values['playfulSophisticated'],
                    style_values['economicalLuxurious'], style_values['geometricOrganic'],
                    style_values['abstractLiteral'],
                    status
                ])
                
            except Exception as e:
                logger.error(f"处理条目 {entry_number} 时出错: {e}")
                continue
        
        logger.info(f"第 {page} 页处理完成，获得 {len(page_data)} 条记录")
        return page_data

    # 使用线程池处理，但减少并发数量以避免被限制
    with ThreadPoolExecutor(max_workers=3) as executor:  # 减少并发数
        futures = [executor.submit(fetch_and_download, page) for page in range(1, total_pages + 1)]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    csv_data.extend(result)
            except Exception as e:
                logger.error(f"线程执行出错: {e}")

    if not csv_data:
        logger.warning(f"比赛 {contest_id} 没有找到任何图片数据")
        return

    # 转换为DataFrame并保存
    try:
        df = pd.DataFrame(csv_data, columns=[
            'ContestID', 'PriceUSD', 'PackageLevel', 'Guarantee', 'Blind', 'Fasttrack', 'Industry', 'OtherNotes',
            'Inspiration', 'Reference', 'CreateTime', 'DesignID', 'Entry', 'Rating', 'Winner', 'Image URL', 'UserID',
            'UserName', 'UserURL', 'AggregateRating', 'AggregateReviews', 'StartDate', 'ContestsWon', 'RunnerUp',
            'OnetoOne', 'RepeatClients', 'UserTag', 'Certifications', 'Languages',
            'ClassicModern', 'MatureYouthful', 'FeminineMasculine', 'PlayfulSophisticated',
            'EconomicalLuxurious', 'GeometricOrganic', 'AbstractLiteral','Status'
        ])

        csv_file_path = os.path.join(output_dir, f'Submission_Contestant_{csv_filename}_Deleted.csv')
        df.drop_duplicates(subset=['Entry'], inplace=True)  # 去重
        df.to_csv(csv_file_path, index=False)
        logger.info(f"CSV文件已保存: {csv_file_path}, 包含 {len(df)} 条记录")
        
    except Exception as e:
        logger.error(f"保存CSV文件时出错: {e}")
        raise
    
    finally:
        # 关闭session
        session.close()