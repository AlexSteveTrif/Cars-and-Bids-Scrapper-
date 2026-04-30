"""
Cars & Bids Past Auctions Scraper
==================================

Scrapes detailed auction data from carsandbids.com past auctions and maintains
a deduplicated master CSV. Each auction is keyed by its unique URL slug ID.

Key features
------------
- Bypasses Cloudflare via undetected-chromedriver
- Handles lazy-loaded listing cards (scrolls to fetch all)
- Splits Mileage into numeric value + TMU flag
- Splits Location into city / state / postal_code
- Deduplicates against an existing master CSV
- Saves after every page (atomic write — safe against crashes)
- Optional Excel snapshot export

Public API
----------
scrape_pages(start_page, end_page, master_path, excel_path, skip_existing)
"""

import os
import re
import time
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup
import undetected_chromedriver as uc

BASE_URL = "https://carsandbids.com"
CHROME_VERSION = 147
REQUEST_DELAY = 3
PAGE_LOAD_WAIT = 5
DETAIL_LOAD_WAIT = 4

COLUMN_ORDER = [
    'auction_id', 'url', 'title',
    'make', 'model', 'engine', 'drivetrain',
    'mileage', 'mileage_tmu',
    'transmission', 'vin', 'body_style', 'title_status',
    'exterior_color', 'interior_color',
    'city', 'state', 'postal_code',
    'seller_name', 'seller_type',
    'reserve', 'status', 'bid_amount',
    'ended_at', 'bids', 'views', 'watching',
    'scraped_at',
]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _make_driver():
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    return uc.Chrome(options=options, headless=False, version_main=CHROME_VERSION)


def _scroll_to_load_all(driver, pause=1.5):
    """Scroll to bottom repeatedly until page height stops growing."""
    prev_height = 0
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height
    driver.execute_script("window.scrollTo(0, 0);")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _auction_id_from_url(url):
    """Extract the unique auction ID slug — the primary key."""
    m = re.search(r'/auctions/([^/]+)/', url)
    return m.group(1) if m else None


def _parse_location(text):
    if not text:
        return None, None, None
    try:
        city_part, state_postal = text.split(", ", 1)
        state, postal = state_postal.split(" ", 1)
        return city_part.strip(), state.strip(), postal.strip()
    except Exception:
        return text.strip(), None, None


def _parse_mileage(text):
    """
    Returns (numeric_mileage, mileage_tmu).
      mileage_tmu = False → accurate
      mileage_tmu = True  → uncertain (TMU / Not Actual / Miles Shown)
      mileage_tmu = None  → exempt from disclosure
    """
    if not text:
        return None, None
    text_lower = text.lower()

    num_match = re.search(r'[\d,]+', text)
    numeric = int(num_match.group().replace(',', '')) if num_match else None

    if 'exempt' in text_lower:
        tmu = None
    elif any(kw in text_lower for kw in ('tmu', 'not actual', 'miles shown')):
        tmu = True
    else:
        tmu = False

    return numeric, tmu


def _clean_number(text):
    if not text:
        return None
    cleaned = re.sub(r'[$,]', '', text.strip())
    try:
        return int(cleaned)
    except ValueError:
        try:
            return float(cleaned)
        except ValueError:
            return None


# ---------------------------------------------------------------------------
# Page extractors
# ---------------------------------------------------------------------------

def _extract_specs(soup):
    specs = {}
    dl = soup.select_one('div.cnb-details-quick-facts dl')
    if not dl:
        return specs
    for dt, dd in zip(dl.find_all('dt'), dl.find_all('dd')):
        specs[dt.get_text(strip=True)] = dd.get_text(strip=True)
    return specs


def _extract_stats(soup):
    result = {}
    stats_div = soup.select_one('div.cnb-details-stats')
    if not stats_div:
        return result

    nr_tag = stats_div.select_one('span.cnb-nr-tag')
    result['reserve'] = nr_tag.get_text(strip=True) if nr_tag else 'Reserve'

    bid_val = stats_div.select_one('div.current-bid span.bid-value')
    result['bid_amount'] = bid_val.get_text(strip=True) if bid_val else None

    for li in stats_div.select('ul.stats li'):
        th = li.select_one('div.th')
        td = li.select_one('div.td')
        if not th or not td:
            continue
        key = th.get_text(strip=True)
        if key == 'Seller':
            seller_link = td.select_one('a.user')
            result['seller_name'] = seller_link.get_text(strip=True) if seller_link else td.get_text(strip=True)
            dealer_tag = td.find(class_=re.compile(r'dealer', re.I))
            dealer_text = re.search(r'\bdealer\b', td.get_text(), re.I)
            result['seller_type'] = 'Dealer' if (dealer_tag or dealer_text) else 'Private'
        else:
            result[key] = td.get_text(strip=True)

    return result


def _extract_status(soup):
    bid_bar = soup.select_one('div.bid-bar')
    if not bid_bar:
        return None
    if 'sold' in bid_bar.get('class', []):
        return 'Sold'
    status_span = bid_bar.select_one('li.ended span.value')
    return status_span.get_text(strip=True) if status_span else 'Ended'


def _extract_title(soup):
    h4 = soup.select_one('h4.details-subheading')
    if h4:
        nr_tag = h4.find('span', class_='cnb-nr-tag')
        if nr_tag:
            nr_tag.extract()
        return h4.get_text(strip=True)
    page_title = soup.find('title')
    if page_title:
        return page_title.get_text().split(' | ')[0].strip()
    return None


def _extract_listing(driver, url):
    driver.get(url)
    time.sleep(DETAIL_LOAD_WAIT)
    soup = BeautifulSoup(driver.page_source, 'lxml')

    specs = _extract_specs(soup)
    stats = _extract_stats(soup)

    city, state, postal_code = _parse_location(specs.get('Location', ''))
    mileage, mileage_tmu = _parse_mileage(specs.get('Mileage', ''))

    return {
        'auction_id':     _auction_id_from_url(url),
        'url':            url,
        'title':          _extract_title(soup),
        'make':           specs.get('Make'),
        'model':          specs.get('Model'),
        'engine':         specs.get('Engine'),
        'drivetrain':     specs.get('Drivetrain'),
        'mileage':        mileage,
        'mileage_tmu':    mileage_tmu,
        'transmission':   specs.get('Transmission'),
        'vin':            specs.get('VIN'),
        'body_style':     specs.get('Body Style'),
        'title_status':   specs.get('Title Status'),
        'exterior_color': specs.get('Exterior Color'),
        'interior_color': specs.get('Interior Color'),
        'city':           city,
        'state':          state,
        'postal_code':    postal_code,
        'seller_name':    stats.get('seller_name'),
        'seller_type':    stats.get('seller_type'),
        'reserve':        stats.get('reserve'),
        'status':         _extract_status(soup),
        'bid_amount':     _clean_number(stats.get('bid_amount')),
        'ended_at':       stats.get('Ended'),
        'bids':           _clean_number(stats.get('Bids')),
        'views':          _clean_number(stats.get('Views')),
        'watching':       _clean_number(stats.get('Watching')),
        'scraped_at':     datetime.now().isoformat(timespec='seconds'),
    }


def _get_listing_urls(driver, page):
    driver.get(f"{BASE_URL}/past-auctions/?page={page}")
    time.sleep(PAGE_LOAD_WAIT)
    _scroll_to_load_all(driver)
    soup = BeautifulSoup(driver.page_source, 'lxml')
    cards = soup.select('ul.auctions-list li.auction-item')
    urls = []
    for card in cards:
        a = card.select_one('a.hero')
        if a and a.get('href'):
            urls.append(BASE_URL + a['href'])
    return urls


# ---------------------------------------------------------------------------
# Master file I/O
# ---------------------------------------------------------------------------

def _load_master(master_path):
    if not os.path.exists(master_path):
        return pd.DataFrame(columns=COLUMN_ORDER), set()
    df = pd.read_csv(master_path)
    seen = set(df['auction_id'].dropna().astype(str)) if 'auction_id' in df.columns else set()
    return df, seen


def _save_master(df, master_path):
    """Atomic write: temp file then rename."""
    df = df.reindex(columns=COLUMN_ORDER)
    tmp = master_path + '.tmp'
    df.to_csv(tmp, index=False)
    os.replace(tmp, master_path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_pages(
    start_page=1,
    end_page=1,
    master_path="master_data.csv",
    excel_path=None,
    skip_existing=True,
):
    """
    Scrape pages [start_page..end_page] of past auctions.

    Parameters
    ----------
    start_page    : int   First page to scrape (inclusive).
    end_page      : int   Last page to scrape (inclusive).
    master_path   : str   Persistent CSV. Created if missing.
    excel_path    : str   If provided, also export master as .xlsx after the run.
    skip_existing : bool  Skip URLs whose auction_id is already in master.
    """
    master_df, seen_ids = _load_master(master_path)
    print(f"Master: {len(master_df)} existing rows ({len(seen_ids)} unique IDs)")
    print(f"Pages:  {start_page} -> {end_page}\n")

    driver = _make_driver()
    new_rows = []

    try:
        for page in range(start_page, end_page + 1):
            print(f"=== PAGE {page} ===")
            urls = _get_listing_urls(driver, page)
            print(f"  Cards found: {len(urls)}")

            if not urls:
                print("  Page is empty — stopping.")
                break

            if skip_existing:
                fresh = [u for u in urls if _auction_id_from_url(u) not in seen_ids]
                print(f"  New (not in master): {len(fresh)} / {len(urls)}")
                urls = fresh

            for i, url in enumerate(urls, 1):
                try:
                    print(f"  [{i}/{len(urls)}] {url}")
                    row = _extract_listing(driver, url)
                    new_rows.append(row)
                    seen_ids.add(row['auction_id'])
                    print(f"    -> {row.get('title')} | {row.get('status')} | ${row.get('bid_amount')}")
                except Exception as e:
                    print(f"    ERROR: {e}")
                    new_rows.append({
                        'auction_id': _auction_id_from_url(url),
                        'url': url,
                        'scraped_at': datetime.now().isoformat(timespec='seconds'),
                    })

                time.sleep(REQUEST_DELAY)

            # Save after every page so a mid-run crash doesn't lose progress
            if new_rows:
                merged = pd.concat([master_df, pd.DataFrame(new_rows)], ignore_index=True)
                _save_master(merged, master_path)
                print(f"  Saved checkpoint -> {master_path} ({len(merged)} rows)\n")

    finally:
        driver.quit()

    # Final reload + return
    master_df, _ = _load_master(master_path)

    if excel_path:
        master_df.to_excel(excel_path, index=False)
        print(f"Excel snapshot -> {excel_path}")

    print(f"\nDone. Master file: {master_path} ({len(master_df)} total rows, {len(new_rows)} added this run)")
    return master_df
