"""
Cleanup utilities for master_data.csv.

Use these to retroactively fix data issues caused by older versions of the
scraper. Each function is idempotent — running it twice is harmless.

Usage from R:
    source_python("clean_master.py")
    clean_model_column("master_data.csv")
"""

import shutil

import pandas as pd


# Columns that are blank for the vast majority of auctions (they are only
# populated while a listing is live), so a missing value in these is expected
# and is NOT a reason to discard the row.
OPTIONAL_COLUMNS = ['watching', 'views']


def clean_model_column(csv_path="master_data.csv", inplace=True):
    """
    Strip trailing 'Save' from the `model` column.

    Earlier scrapes accidentally included the screen-reader text from the
    "Save listing" subscribe button next to the model name.
    """
    df = pd.read_csv(csv_path)

    if 'model' not in df.columns:
        print(f"No 'model' column in {csv_path}")
        return df

    mask = df['model'].notna() & df['model'].str.endswith('Save', na=False)
    affected = mask.sum()

    if affected == 0:
        print(f"No rows needed cleaning in {csv_path}")
        return df

    df.loc[mask, 'model'] = df.loc[mask, 'model'].str.replace(r'Save$', '', regex=True).str.strip()

    if inplace:
        df.to_csv(csv_path, index=False)
        print(f"Cleaned 'Save' suffix from {affected} rows in {csv_path}")

    return df


def dedupe_and_drop_incomplete(
    csv_path="master_data.csv",
    out_path="master_data_clean.csv",
    inplace=False,
):
    """
    De-duplicate auctions and drop incomplete rows from the master data.

    Older scrape runs appended the same auctions to master_data.csv many times
    over (some up to 48x, all byte-for-byte identical), which inflated the file
    by ~36%. This fixes that in two steps:

      1. Drop duplicate ``auction_id`` rows, keeping the most recently scraped
         copy of each auction (by ``scraped_at``).
      2. Drop rows missing a real field. The columns in ``OPTIONAL_COLUMNS``
         (``watching``/``views``) are exempt, because they are blank for nearly
         all non-live auctions and their absence does not make a record
         unusable.

    By default the result is written to ``out_path`` and the original file is
    left untouched. Pass ``inplace=True`` to overwrite ``csv_path`` instead; a
    ``<csv_path>.bak`` copy of the original is made first.
    """
    df = pd.read_csv(csv_path)
    start_rows = len(df)

    # 1. De-duplicate on auction_id, keeping the newest scrape. Sort so that
    #    valid timestamps come last (unparseable/blank sort first), then keep
    #    the last row of each auction_id group.
    if 'scraped_at' in df.columns:
        order = pd.to_datetime(df['scraped_at'], errors='coerce', utc=True)
        df = df.assign(_order=order).sort_values(
            '_order', kind='stable', na_position='first'
        )
    df = df.drop_duplicates(subset='auction_id', keep='last')
    after_dedupe = len(df)
    removed_dupes = start_rows - after_dedupe

    # 2. Drop rows missing any required (non-optional) field.
    required = [c for c in df.columns
                if c not in OPTIONAL_COLUMNS and not c.startswith('_')]
    df = df.dropna(subset=required)
    removed_incomplete = after_dedupe - len(df)

    # Tidy up: drop the helper column and reset the index.
    df = df.drop(columns=[c for c in df.columns if c.startswith('_')])
    df = df.reset_index(drop=True)

    # Write the result.
    target = csv_path if inplace else out_path
    if inplace:
        backup = csv_path + '.bak'
        shutil.copy2(csv_path, backup)
        print(f"Backed up original  -> {backup}")
    df.to_csv(target, index=False)

    print(f"Read           {start_rows:>6,} rows from {csv_path}")
    print(f"Removed dupes  {removed_dupes:>6,} duplicate auction_id rows")
    print(f"Removed nulls  {removed_incomplete:>6,} incomplete rows "
          f"(missing a field other than {OPTIONAL_COLUMNS})")
    print(f"Wrote          {len(df):>6,} clean rows to {target}")
    return df


if __name__ == '__main__':
    dedupe_and_drop_incomplete()
