# Cars & Bids Scraper

A scraper for past auction listings on [carsandbids.com](https://carsandbids.com). It pulls the vehicle specs, seller info, and final auction stats for every completed listing, then writes them to a deduplicated CSV master file. Built to run inside RStudio via reticulate, since the rest of my analysis lives in R.

## What it collects

For each listing, one row in `master_data.csv` with these fields:

**Vehicle**

`make`, `model`, `engine`, `drivetrain`, `transmission`, `body_style`, `vin`, `title_status`, `exterior_color`, `interior_color`

`mileage` is split into two columns: a numeric value, and `mileage_tmu` which flags whether the reading is uncertain. `True` for "TMU" or "Not Actual" or "Miles Shown" listings, `False` for accurate odometers, and `None` for vehicles old enough to be exempt from disclosure.

`location` gets parsed into `city`, `state`, and `postal_code`. The state column also handles Canadian provinces, and the postal code column handles both US ZIPs (`30519`) and Canadian formats (`M5V 2T6`).

**Seller**

`seller_name` and `seller_type`. Seller type is a Dealer/Private label.

**Auction**

`status` (Sold, Reserve Not Met, etc.), `reserve` (Reserve / No Reserve), `bid_amount` as a number, `bids`, `views`, `watching`, and `ended_at` as a full timestamp like `Apr 30, 2026 1:20 PM MDT`.

**Bookkeeping**

`auction_id` is extracted from the URL path and used as the dedup key. Plus `url`, `title`, and `scraped_at`.

## Setup

I run this inside RStudio with reticulate pointing at a project-local `.venv` managed by [`uv`](https://github.com/astral-sh/uv). The project root has an `.Rprofile` that locks the Python path so it survives session restarts.

If you're starting fresh, from the project root in a terminal:

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

Then add this to `.Rprofile` (use your own absolute path):

```r
Sys.setenv(RETICULATE_PYTHON = "<absolute path>/.venv/Scripts/python.exe")
```

Restart R, confirm with `reticulate::py_config()`, and you're good.

## Usage

```r
library(reticulate)
source_python("scraper.py")

df <- scrape_pages(
  start_page    = 1L,
  end_page      = 5L,
  master_path   = "master_data.csv",
  excel_path    = "cars_and_bids.xlsx",
  skip_existing = TRUE
)
```

`skip_existing = TRUE` means previously-scraped listings won't be hit again. Useful for incremental updates: run the same command tomorrow and it'll only fetch what's actually new.

The scraper writes `master_data.csv` after every page, so if Chrome dies on page 4 of a 10-page run you keep pages 1–3.

## Things that surprised me

A few things took longer than they should have. Writing them down here in case anyone runs into the same problems.

**Cloudflare blocks vanilla Selenium immediately.** First page load returned a "Just a moment..." challenge page no matter what stealth flags I added. `undetected-chromedriver` patches the WebDriver fingerprint at the binary level and gets through cleanly. It does need a visible Chrome window though, headless mode trips the check.

**Python 3.12 removed `distutils`** from the standard library, but undetected-chromedriver still imports it. The fix is `pip install setuptools` even though you're not using setuptools directly. The error message doesn't make this obvious.

**ChromeDriver version pinning.** undetected-chromedriver auto-downloads a ChromeDriver, but it doesn't always match your installed Chrome. I had to pin `version_main=147` in the `_make_driver()` call to match my local Chrome. If your Chrome is on a different version, edit the `CHROME_VERSION` constant in `scraper.py`.

**The past-auctions list is lazy-loaded.** First few runs returned wildly different counts (85, 30, 15) because not all the cards had rendered before BeautifulSoup parsed the page. The fix is to scroll to the bottom in a loop until the page height stops increasing, then capture the HTML.

**Mileage isn't always a number.** Listings on older cars show things like "209,100 Miles Shown - TMU" or "Exempt". Splitting mileage into a numeric column plus a `mileage_tmu` flag means I can still filter and aggregate cleanly downstream.

## Limitations

Things I'd want to fix if I keep building on this:

- Seller type detection is a heuristic that looks for "dealer" text in the seller block. Most listings don't expose an explicit type label, so private sellers default to Private. If anyone has a more reliable approach, I'm open to suggestions.
- `CHROME_VERSION` is hardcoded. Should auto-detect from the registry on Windows.
- No retry on individual listing failures. If a page times out, the row gets written with just the URL and ID and you have to re-run that listing manually.
- Only handles the past-auctions section. Doesn't touch active auctions, which would need a different parser since the bid/status fields are live.

## Files

```
scraper.py        # main scraper, all the parsing lives here
scraper.R         # R wrapper that calls scrape_pages via reticulate
requirements.txt  # Python dependencies
master_data.csv   # the output (committed so you can see the schema)
.Rprofile         # locks RETICULATE_PYTHON to the project venv (gitignored)
```

## Disclaimer

Personal project, not affiliated with Cars & Bids. Scrape at a reasonable rate (the default `REQUEST_DELAY = 3` seconds is intentional) and don't be a jerk about it.
