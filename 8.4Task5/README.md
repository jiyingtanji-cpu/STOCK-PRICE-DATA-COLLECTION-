# Task 5 - Stock Price Data Collection

**Author:** Yingtan Ji  
**Primary source:** Yahoo Finance through `yfinance`  
**Default study period:** 2021-01-01 to 2025-12-31, inclusive  
**Default company universe:** loaded from `config/selected_companies.csv`

## Purpose

This submission provides a reproducible Python workflow that downloads,
organizes, validates, and publishes daily historical equity prices. It needs no
API key. Each accepted security is saved as a flat CSV, while provenance,
validation, external comparison, hashes, and run-level status are saved as
separate metadata.

The submitted universe contains AAPL, MSFT, GOOGL, AMZN, and NVDA because those
rows are defined in `config/selected_companies.csv`; the script does not embed
that list or the company names. A different ordered universe can be supplied by
editing that CSV or passing `--companies-file`. `--tickers` can override the
default ticker selection while retaining matching company names from the
configuration file.

No missing row or price is imputed. The request explicitly uses
`repair=False`, `auto_adjust=False`, and `rounding=False`. Output changes are
limited to schema normalization, ISO date formatting, and integer
serialization for reported share volume.

## Reproduce the submission

Create an isolated environment and install the version-pinned direct
dependencies. The requirements file also pins `exchange-calendars`, which is
used for an independent calendar check. It is not a fully generated transitive
lock file.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\collect_stock_data.py
.\.venv\Scripts\python.exe -m pytest -q
```

The command-line `--end` date is inclusive; the implementation adds one
calendar day only when forming yfinance's exclusive `end` parameter. A custom
sample can be collected without editing source code:

```powershell
.\.venv\Scripts\python.exe scripts\collect_stock_data.py `
  --tickers AAPL MSFT `
  --start 2022-01-01 `
  --end 2024-12-31
```

To define both tickers and manifest company names in another file:

```powershell
.\.venv\Scripts\python.exe scripts\collect_stock_data.py `
  --companies-file config\selected_companies.csv
```

Use `python scripts\collect_stock_data.py --help` for all options.

## Failure policies and transaction-safe publication

The default mode is fail-closed:

```powershell
.\.venv\Scripts\python.exe scripts\collect_stock_data.py --fail-closed
```

For a larger production-style universe, partial publication is optional:

```powershell
.\.venv\Scripts\python.exe scripts\collect_stock_data.py `
  --allow-partial-with-failure-report
```

Every run receives a unique `run_id`. All raw files and current metadata are
first written to a sibling staging directory. Existing auxiliary artifacts,
such as earlier `failed_runs/`, are preserved in that staged tree. The script
backs up the preceding `data/` directory and installs the complete staged
directory as one snapshot rename. It restores the prior directory if snapshot
installation or the final-status write fails. The final `SUCCESS` or `PARTIAL`
status is written only after the full new snapshot is live. This prevents a
success record from referring to missing files, prevents mixed raw/metadata
versions, and ensures a partial run cannot leave a stale CSV for a failed
ticker.

In fail-closed mode, any final ticker failure leaves the last published
`data/raw/` and `data/metadata/` snapshot unchanged. The new failure evidence
is written to `data/failed_runs/<RUN_ID>/`. In partial mode, the new snapshot
contains only securities that passed every applicable check. If catastrophic
rollback itself fails, the recovery backup is intentionally retained instead
of being deleted.

Failure reports contain only `ticker`, `attempt`, `stage`, `error_type`,
`error_message`, and `timestamp_utc`. Configured proxy values and URL
credentials are redacted; metadata records only whether a proxy was configured.

## Directory structure

```text
task5_stock_data_collection/
|-- .github/workflows/tests.yml
|-- config/
|   |-- reference_close_sample.csv
|   `-- selected_companies.csv
|-- data/
|   |-- failed_runs/
|   |   `-- <RUN_ID>/
|   |       |-- failure_report.csv
|   |       |-- run_metadata.json
|   |       |-- validation_summary.csv
|   |       `-- reference_close_comparison.csv  # when configured
|   |-- metadata/
|   |   |-- failure_report.csv
|   |   |-- manifest.csv
|   |   |-- reference_close_comparison.csv
|   |   |-- run_metadata.json
|   |   `-- validation_summary.csv
|   `-- raw/
|       `-- <TICKER>_daily_<START>_<END>.csv
|-- docs/
|   `-- Task5_Stock_Price_Data_Collection_Report_Yingtan_Ji.docx
|-- logs/collection.log
|-- scripts/collect_stock_data.py
|-- tests/test_collect_stock_data.py
|-- pytest.ini
|-- README.md
`-- requirements.txt
```

## Output schema

| Field | Type | Definition |
|---|---:|---|
| `Date` | ISO date | Trading-session date |
| `Open` | decimal | Yahoo Finance opening price |
| `High` | decimal | Yahoo Finance session high |
| `Low` | decimal | Yahoo Finance session low |
| `Close` | decimal | Yahoo Finance unadjusted close |
| `Adj Close` | decimal | Yahoo Finance adjusted close |
| `Volume` | integer | Reported shares traded |

## Acceptance checks

Each security must satisfy all applicable conditions before publication:

1. The required ordered schema is present.
2. Dates are parseable, unique, ascending, and within the requested interval.
3. Every scheduled NASDAQ session is present, with no unexpected date.
4. Price and volume fields contain no missing or non-finite value.
5. Prices are positive; volume is a non-negative integer.
6. `High` is no lower than `Open`, `Low`, or `Close`; `Low` is no higher than
   `Open`, `High`, or `Close`.
7. For each configured external sample row, the absolute difference between
   Yahoo and the reference close is at most USD 0.01.

The submitted five CSVs contain 1,255 sessions each (6,275 rows total), with
no missing session, duplicate date, null, non-finite value, invalid price or
volume, or OHLC range violation. SHA-256 values in `manifest.csv` match the
delivered raw files.

In the pinned `pandas-market-calendars` version, the requested `NASDAQ` name is
an alias implemented by its `NYSE` U.S.-equity calendar. The metadata records
both names. For the submitted 2021-01-01 through 2025-12-31 interval, an
independent `exchange-calendars` XNAS/XNYS calculation produced the identical
1,255-date set, so this alias does not change the completeness result.

### Independent close-price sample

The external sample compares each submitted Yahoo `Close` on 2025-12-31 with
Nasdaq.com's chart API value. `Difference` is defined as `Yahoo Close - Nasdaq
Close`. Nasdaq reports these values to cents, so the check uses an absolute
tolerance of USD 0.01 while retaining Yahoo's returned precision.

| Ticker | Date | Yahoo Close | Nasdaq Close | Difference | Result |
|---|---|---:|---:|---:|---|
| AAPL | 2025-12-31 | 271.85998535 | 271.86 | -0.00001465 | PASS |
| MSFT | 2025-12-31 | 483.61999512 | 483.62 | -0.00000488 | PASS |
| GOOGL | 2025-12-31 | 313.00000000 | 313.00 | 0.00000000 | PASS |
| AMZN | 2025-12-31 | 230.82000732 | 230.82 | 0.00000732 | PASS |
| NVDA | 2025-12-31 | 186.50000000 | 186.50 | 0.00000000 | PASS |

The cited source values, retrieval times, and per-ticker URLs are in
`config/reference_close_sample.csv`; calculated evidence is in
`data/metadata/reference_close_comparison.csv`. This is a sample-based
cross-check, not a claim that all 6,275 observations were reconciled with a
licensed professional feed.

## Tests and continuous integration

The deterministic suite contains 24 tests covering normalization, schema and
value checks, market-session completeness, external close comparison,
retries, proxy redaction, both failure policies, configuration validation,
reference-source provenance, stale-file removal, staging failure, final-status
failure, and snapshot rollback. All 24 tests passed locally on 2026-08-04.

`.github/workflows/tests.yml` is configured to run on pushes, pull requests,
or manual dispatch using Python 3.12. It installs the pinned requirements,
runs `pip check`, compiles the collection script, and executes pytest. The
workflow deliberately avoids live market downloads. This folder is not itself
a Git repository, so the workflow is configured and ready to use but was not
claimed to have executed on GitHub in this local submission.

## Provenance timing

The submitted Yahoo files were collected on 2026-07-30. The feedback-driven
Nasdaq sample comparison and supporting metadata/report updates were completed
on 2026-08-04. These are distinct evidence events; the external source's
retrieval timestamp is preserved per row and is not presented as the original
Yahoo collection time.

## Limitations and responsible use

`yfinance` is an independent open-source client and is not affiliated with or
endorsed by Yahoo. Live provider availability can be intermittent, and later
downloads may reflect provider revisions. The included hashes identify the
exact files submitted. The data are suitable for coursework and reproducible
research, not execution-grade trading or regulated reporting. Users remain
responsible for complying with provider terms.

## References

Aroussi, R. (2026). *yfinance: Download market data from Yahoo! Finance's API*
[Computer software]. PyPI. https://pypi.org/project/yfinance/

Aroussi, R. (n.d.). *yfinance.download*. yfinance documentation. Retrieved July
30, 2026, from
https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html

Pandas Market Calendars. (n.d.). *pandas_market_calendars documentation*.
Retrieved July 30, 2026, from
https://pandas-market-calendars.readthedocs.io/en/stable/

Nasdaq. (n.d.). *Historical stock chart data*. Retrieved August 4, 2026, from
https://www.nasdaq.com/market-activity/quotes/historical

Yahoo Finance. (n.d.). *Historical data*. Retrieved July 30, 2026, from
https://finance.yahoo.com/
