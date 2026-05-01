library(reticulate)

source_python("scraper.py")

# Scrape pages 1 through 5, skip listings already in master
df <- scrape_pages(
  start_page    = 80L,
  end_page      = 500L,
  master_path   = "master_data.csv",
  excel_path    = "cars_and_bids.xlsx",   # optional snapshot
  skip_existing = TRUE
)