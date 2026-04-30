import re
import time
import pandas as pd
from bs4 import BeautifulSoup
import undetected_chromedriver as uc

BASE_URL = "https://carsandbids.com"
CHROME_VERSION = 147
REQUEST_DELAY = 3


def _make_driver():
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    return uc.Chrome(options=options, headless=False, version_main=CHROME_VERSION)


def _scroll_to_load_all(driver, pause=1.5):
    """Scroll to bottom repeatedly until page height stops growing (triggers lazy-loaded cards)."""
    prev_height = 0
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height
    driver.execute_script("window.scrollTo(0, 0);")


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
    mileage_tmu = True  → mileage is uncertain (TMU / Not Actual / Miles Shown)
    mileage_tmu = False → mileage is accurate
    mileage_tmu = None  → exempt from odometer disclosure
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


def _extract_specs(soup):
    specs = {}
    dl = soup.select_one('div.cnb-details-quick-facts dl')
    if not dl:
        return specs
    dts = dl.find_all('dt')
    dds = dl.find_all('dd')
    for dt, dd in zip(dts, dds):
        key = dt.get_text(strip=True)
        value = dd.get_text(strip=True)
        specs[key] = value
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
    time.sleep(4)
    soup = BeautifulSoup(driver.page_source, 'lxml')

    specs = _extract_specs(soup)
    stats = _extract_stats(soup)
    status = _extract_status(soup)
    title = _extract_title(soup)

    location_raw = specs.get('Location', '')
    city, state, postal_code = _parse_location(location_raw)

    mileage, mileage_tmu = _parse_mileage(specs.get('Mileage', ''))

    return {
        'url':            url,
        'title':          title,
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
        'status':         status,
        'bid_amount':     _clean_number(stats.get('bid_amount')),
        'ended_at':       stats.get('Ended'),
        'bids':           _clean_number(stats.get('Bids')),
        'views':          _clean_number(stats.get('Views')),
        'watching':       _clean_number(stats.get('Watching')),
    }


def _get_listing_urls(driver, page):
    driver.get(f"{BASE_URL}/past-auctions/?page={page}")
    time.sleep(5)
    _scroll_to_load_all(driver)
    soup = BeautifulSoup(driver.page_source, 'lxml')
    cards = soup.select('ul.auctions-list li.auction-item')
    print(f"  Cards found after scrolling: {len(cards)}")
    urls = []
    for card in cards:
        a = card.select_one('a.hero')
        if a and a.get('href'):
            urls.append(BASE_URL + a['href'])
    return urls


def scrape_page(page=1, output_path="cars_and_bids.xlsx"):
    driver = _make_driver()
    rows = []

    try:
        print(f"Loading past auctions page {page}...")
        urls = _get_listing_urls(driver, page)
        print(f"Found {len(urls)} listings\n")

        for i, url in enumerate(urls, 1):
            try:
                print(f"[{i}/{len(urls)}] {url}")
                row = _extract_listing(driver, url)
                rows.append(row)
                print(f"  -> {row.get('title')} | {row.get('status')} | ${row.get('bid_amount')}")
            except Exception as e:
                print(f"  ERROR: {e}")
                rows.append({'url': url, 'error': str(e)})

            time.sleep(REQUEST_DELAY)

    finally:
        driver.quit()

    df = pd.DataFrame(rows)
    df.to_excel(output_path, index=False)
    print(f"\nDone. Saved {len(rows)} rows to: {output_path}")
    return df
