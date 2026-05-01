"""
Cleanup utilities for master_data.csv.

Use these to retroactively fix data issues caused by older versions of the
scraper. Each function is idempotent — running it twice is harmless.

Usage from R:
    source_python("clean_master.py")
    clean_model_column("master_data.csv")
"""

import pandas as pd


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


if __name__ == '__main__':
    clean_model_column()
