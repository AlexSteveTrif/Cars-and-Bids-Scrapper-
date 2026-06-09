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
- Tracks last completed page in scraper_progress.json
- All timestamps reported in Mountain Time (America/Edmonton)
- Optional Excel snapshot export

Public API
----------
scrape_pages(start_page, end_page, master_path, excel_path, skip_existing)
get_resume_page(lookback, progress_path)
"""

import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

BASE_URL          = "https://carsandbids.com"
CHROME_VERSION    = 147
REQUEST_DELAY     = 4
LIST_PAGE_TIMEOUT = 300
DETAIL_PAGE_TIMEOUT = 300
LIST_PAGE_RETRIES = 30
LOOKBACK_PAGES    = 5
PROGRESS_FILE     = "scraper_progress.json"

MOUNTAIN_TZ = ZoneInfo("America/Edmonton")

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
# Time helpers (Mountain Time throughout)
# ---------------------------------------------------------------------------

def _now_mst():
    return datetime.now(tz=MOUNTAIN_TZ)


def _fmt_time(dt=None):
    if dt is None:
        dt = _now_mst()
    return dt.strftime("%b %d, %Y %I:%M:%S %p %Z")


def _elapsed(start):
    secs = int((_now_mst() - start).total_seconds())
    m, s = divmod(secs, 60)
    return f"{m}m {s}s" if m else f"{s}s"


# ---------------------------------------------------------------------------
# Progress checkpoint
# ---------------------------------------------------------------------------

def _load_progress(progress_path=PROGRESS_FILE):
    if not os.path.exists(progress_path):
        return None
    with open(progress_path) as f:
        return json.load(f)


def _save_progress(page, progress_path=PROGRESS_FILE):
    data = {
        "last_completed_page": page,
        "last_run_mst": _fmt_time(),
    }
    with open(progress_path, 'w') as f:
        json.dump(data, f, indent=2)


def get_resume_page(lookback=LOOKBACK_PAGES, progress_path=PROGRESS_FILE):
    """
    Return the recommended start page based on the last checkpoint.

    Goes back `lookback` pages from the last completed page as a safety
    buffer. Because skip_existing=True deduplicates by auction_id, those
    overlap pages won't produce duplicate rows.
    """
    progress = _load_progress(progress_path)
    if not progress:
        print("No checkpoint found — starting from page 1.")
        return 1
    last = progress["last_completed_page"]
    resume = max(1, last - lookback)
    print(f"Checkpoint: last completed page = {last}")
    print(f"Resuming from page {resume} ({lookback}-page lookback)")
    print(f"Last run: {progress.get('last_run_mst', 'unknown')}\n")
    return resume


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
        for el in dd.select('button, .sr-only'):
            el.decompose()
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
            dealer_tag  = td.find(class_=re.compile(r'dealer', re.I))
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


def _wait_for(driver, css_selector, timeout):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, css_selector))
        )
        return True
    except TimeoutException:
        return False


def _extract_listing(driver, url):
    driver.get(url)
    _wait_for(driver, 'div.cnb-details-quick-facts', DETAIL_PAGE_TIMEOUT)
    time.sleep(1)
    soup = BeautifulSoup(driver.page_source, 'lxml')

    specs = _extract_specs(soup)
    stats = _extract_stats(soup)

    city, state, postal_code = _parse_location(specs.get('Location', ''))
    mileage, mileage_tmu     = _parse_mileage(specs.get('Mileage', ''))

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
        'scraped_at':     _now_mst().isoformat(timespec='seconds'),
    }


def _get_listing_urls(driver, page):
    """
    Load a past-auctions page and return all listing URLs.
    Retries up to LIST_PAGE_RETRIES times — deeper pages render more slowly.
    Logs the time of each retry in Mountain Time.
    """
    for attempt in range(1, LIST_PAGE_RETRIES + 1):
        driver.get(f"{BASE_URL}/past-auctions/?page={page}")
        appeared = _wait_for(driver, 'ul.auctions-list li.auction-item', LIST_PAGE_TIMEOUT)

        if appeared:
            _scroll_to_load_all(driver)
            soup  = BeautifulSoup(driver.page_source, 'lxml')
            cards = soup.select('ul.auctions-list li.auction-item')
            urls  = [
                BASE_URL + c.select_one('a.hero')['href']
                for c in cards
                if c.select_one('a.hero') and c.select_one('a.hero').get('href')
            ]
            if urls:
                return urls

        wait_secs = 5 * attempt
        print(f"  [{_fmt_time()}] Attempt {attempt}/{LIST_PAGE_RETRIES}: "
              f"no cards rendered — retrying in {wait_secs}s...")
        time.sleep(wait_secs)

    return []


# ---------------------------------------------------------------------------
# Master file I/O
# ---------------------------------------------------------------------------

def _load_master(master_path):
    if not os.path.exists(master_path):
        return pd.DataFrame(columns=COLUMN_ORDER), set()
    df   = pd.read_csv(master_path)
    seen = set(df['auction_id'].dropna().astype(str)) if 'auction_id' in df.columns else set()
    return df, seen


def _save_master(df, master_path):
    """Atomic write: write to .tmp then rename — crash-safe."""
    df  = df.reindex(columns=COLUMN_ORDER)
    tmp = master_path + '.tmp'
    df.to_csv(tmp, index=False)
    os.replace(tmp, master_path)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_pages(
    start_page    = None,
    end_page      = None,
    master_path   = "master_data.csv",
    excel_path    = None,
    skip_existing = True,
    lookback      = LOOKBACK_PAGES,
    progress_path = PROGRESS_FILE,
):
    """
    Scrape past-auction pages and append new rows to master_data.csv.

    Parameters
    ----------
    start_page    : int | None  First page (inclusive). None = auto from checkpoint.
    end_page      : int | None  Last page (inclusive).  None = run until empty page.
    master_path   : str         Persistent CSV — created if missing.
    excel_path    : str | None  Also export master as .xlsx after the run.
    skip_existing : bool        Skip auction_ids already in master.
    lookback      : int         Pages to rewind from checkpoint when auto-resuming.
    progress_path : str         Path to the JSON checkpoint file.
    """
    if start_page is None:
        start_page = get_resume_page(lookback, progress_path)

    master_df, seen_ids = _load_master(master_path)
    run_start = _now_mst()

    print(f"Run started:  {_fmt_time(run_start)}")
    print(f"Master:       {len(master_df)} existing rows ({len(seen_ids)} unique IDs)")
    end_label = str(end_page) if end_page is not None else "until empty page"
    print(f"Pages:        {start_page} -> {end_label}\n")

    driver   = _make_driver()
    total_added = 0
    page_range = (
        range(start_page, end_page + 1)
        if end_page is not None
        else _infinite_range(start_page)
    )

    try:
        for page in page_range:
            page_start = _now_mst()
            print(f"=== PAGE {page} | {_fmt_time(page_start)} ===")

            urls = _get_listing_urls(driver, page)
            print(f"  Cards found: {len(urls)}")

            if not urls:
                print("  Page is empty — stopping.")
                break

            if skip_existing:
                fresh = [u for u in urls if _auction_id_from_url(u) not in seen_ids]
                print(f"  New (not in master): {len(fresh)} / {len(urls)}")
                urls = fresh

            # Per-page buffer — must be reset each page. Rows already merged
            # into master_df below must NOT be carried over and re-appended on
            # the next page (that was the duplication bug).
            page_rows = []

            for i, url in enumerate(urls, 1):
                try:
                    print(f"  [{i}/{len(urls)}] {url}")
                    row = _extract_listing(driver, url)
                    page_rows.append(row)
                    seen_ids.add(row['auction_id'])
                    print(f"    -> {row.get('title')} | {row.get('status')} | ${row.get('bid_amount')}")
                except Exception as e:
                    print(f"    ERROR at {_fmt_time()}: {e}")
                    page_rows.append({
                        'auction_id': _auction_id_from_url(url),
                        'url':        url,
                        'scraped_at': _now_mst().isoformat(timespec='seconds'),
                    })

                time.sleep(REQUEST_DELAY)

            # Append only THIS page's rows to master, then save + checkpoint.
            if page_rows:
                master_df = pd.concat([master_df, pd.DataFrame(page_rows)],
                                      ignore_index=True)
                _save_master(master_df, master_path)
                total_added += len(page_rows)

            _save_progress(page, progress_path)

            print(f"  Page {page} done | {_fmt_time()} | elapsed: {_elapsed(page_start)} "
                  f"| master total: {len(master_df)} rows\n")

    finally:
        driver.quit()

    if excel_path:
        master_df.to_excel(excel_path, index=False)
        print(f"Excel snapshot -> {excel_path}")

    print(f"\nRun finished: {_fmt_time()}")
    print(f"Total elapsed: {_elapsed(run_start)}")
    print(f"Added this run: {total_added} rows | Master total: {len(master_df)} rows")
    return master_df


def _infinite_range(start):
    """Generator that counts up from start indefinitely."""
    n = start
    while True:
        yield n
        n += 1
