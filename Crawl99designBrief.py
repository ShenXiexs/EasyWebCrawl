import pandas as pd
from retrying import retry
import json
import requests
from requests.exceptions import SSLError
from bs4 import BeautifulSoup
import re
import os

# Retry decorator to handle retries on SSLError
# Retry decorator to handle retries on SSLError, with a maximum retry limit of 15
@retry(wait_random_min=200, wait_random_max=500, stop_max_attempt_number=15, retry_on_exception=lambda e: isinstance(e, SSLError))
def get_contest_id(url):
    """Extract Contest ID from the URL."""
    match = re.search(r'contests/[^/]+-(\d+)/entries', url)
    if match:
        return match.group(1)
    else:
        return 'N/A'


@retry(wait_random_min=200, wait_random_max=500, stop_max_attempt_number=15, retry_on_exception=lambda e: isinstance(e, SSLError))
def extract_winner_entry(url):
    """Fetch HTML from the URL and extract winning entry IDs."""
    # Send a GET request to retrieve the HTML content
    response = requests.get(url)
    response.raise_for_status()  # Raise an error if the request fails
    # Parse HTML content with BeautifulSoup
    soup = BeautifulSoup(response.text, 'html.parser')
    # Find all entry elements
    entries = soup.find_all("div", class_="entry")
    winner_ids = set()  # Use a set to remove duplicates

    for entry in entries:
        entry_id = entry.get('id', '').replace("entry-", "")
        # Check if the entry has winner status
        winner_status = entry.find("div", {"data-entry-status": "winner"})
        if winner_status and entry_id:  # Ensure entry_id exists
            winner_ids.add(entry_id)

    # Return a comma-separated string of IDs
    return ",".join(winner_ids)


@retry(wait_random_min=200, wait_random_max=500, stop_max_attempt_number=15, retry_on_exception=lambda e: isinstance(e, SSLError))
def get_brief_info(brief_url, headers, contest_id, output_dir):
    """Fetch the brief page, extract PriceUSD, PackageLevel, Guarantee, FastTrack, Industry, Blind, OtherNote, Inspiration count, Reference count, and design style attributes."""
    response = requests.get(brief_url, headers=headers)
    response.raise_for_status()  # Raise an exception for 4xx/5xx errors
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
            print("Error decoding JSON from data-initial-props")
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

    # Extract the publicId for images (for counting purposes)
    public_id_matches = re.findall(r'&quot;publicId&quot;:&quot;([a-zA-Z0-9]+)&quot;', response.text)
    if not public_id_matches:
        print(f"No images found for contest {contest_id}.")
        return price_usd, package_level, guarantee, fasttrack, industry, blind, other_note, 0, 0, style_values

    # Check for reference public IDs
    references_section = re.search(
        r'References&quot;,&quot;elements&quot;:\{&quot;attachments&quot;:\{&quot;value&quot;:\[\{&quot;publicId&quot;:&quot;([a-zA-Z0-9]+)&quot;',
        response.text)
    references_public_ids = references_section.group(1) if references_section else None

    # Count inspiration and reference images without downloading
    inspiration_count, reference_count = 0, 0
    for public_id in public_id_matches:
        if references_public_ids and public_id == references_public_ids:
            reference_count += 1
        else:
            inspiration_count += 1

    return price_usd, package_level, guarantee, fasttrack, industry, blind, other_note, inspiration_count, reference_count, style_values

@retry(wait_random_min=200, wait_random_max=500, stop_max_attempt_number=15, retry_on_exception=lambda e: isinstance(e, SSLError))
def download_brief(url, output_dir, csv_filename):
    """Download brief information and save it to a CSV file."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.25 Safari/537.36 Core/1.70.3861.400'
    }

    # Data to be saved in CSV
    csv_data = []

    # Extract ContestID from the URL
    contest_id = get_contest_id(url)

    # Extract the brief information (including design style attributes)
    brief_url = url.replace('/entries', '/brief')

    retries = 15  # Retry up to 15 times
    while retries > 0:
        try:
            price_usd, package_level, guarantee, fasttrack, industry, blind, other_note, inspiration_count, reference_count, style_values = get_brief_info(
                brief_url, headers, contest_id, output_dir)
            # Exit the retry loop if successful
            break
        except SSLError as e:
            retries -= 1
            print(f"Failed to fetch brief info (retries left: {retries}): {e}")
            if retries == 0:
                print("Aborting brief download after multiple failures.")
                return  # Exit function if all retries fail

    # Extract the winner entry data
    winner_entry = extract_winner_entry(url)

    # Append data to csv_data list, including design style attributes
    csv_data.append([
        contest_id, price_usd, package_level, guarantee, blind, fasttrack, industry, other_note,
        inspiration_count, reference_count,
        style_values['classicModern'], style_values['matureYouthful'],
        style_values['feminineMasculine'], style_values['playfulSophisticated'],
        style_values['economicalLuxurious'], style_values['geometricOrganic'],
        style_values['abstractLiteral'], winner_entry
    ])

    # Save the data to a CSV file
    csv_file_path = os.path.join(output_dir, f'Submission_Contestant_{csv_filename}.csv')
    df = pd.DataFrame(csv_data, columns=[
        'ContestID', 'PriceUSD', 'PackageLevel', 'Guarantee', 'Blind', 'Fasttrack', 'Industry', 'OtherNotes',
        'Inspiration', 'Reference',
        'ClassicModern', 'MatureYouthful', 'FeminineMasculine', 'PlayfulSophisticated',
        'EconomicalLuxurious', 'GeometricOrganic', 'AbstractLiteral', 'WinnerEntry'
    ])
    df.to_csv(csv_file_path, index=False)
    print(f"CSV file saved as {csv_file_path}.")