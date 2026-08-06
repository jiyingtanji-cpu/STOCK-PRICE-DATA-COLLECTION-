"""Collect and validate daily historical equity prices from Yahoo Finance.

The command-line end date is inclusive. The yfinance end date is exclusive, so
one calendar day is added internally. No price repair, interpolation, forward
fill, or synthetic-row construction is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf


AUTHOR = "Yingtan Ji"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPANIES_FILE = PROJECT_ROOT / "config" / "selected_companies.csv"
DEFAULT_START = date(2021, 1, 1)
DEFAULT_END = date(2025, 12, 31)
DEFAULT_CALENDAR = "NASDAQ"
FAIL_CLOSED = "fail-closed"
ALLOW_PARTIAL = "allow-partial-with-failure-report"
DEFAULT_REFERENCE_TOLERANCE = 0.01
OUTPUT_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Adj Close", "Volume")
PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close")
FAILURE_REPORT_COLUMNS = (
    "ticker",
    "attempt",
    "stage",
    "error_type",
    "error_message",
    "timestamp_utc",
)
REFERENCE_INPUT_COLUMNS = (
    "ticker",
    "date",
    "reference_close",
    "reference_source",
    "reference_url",
    "reference_retrieved_at_utc",
)
REFERENCE_OUTPUT_COLUMNS = (
    "ticker",
    "date",
    "yahoo_close",
    "reference_close",
    "difference",
    "absolute_difference",
    "tolerance",
    "status",
    "reference_source",
    "reference_url",
    "reference_retrieved_at_utc",
)
LOGGER = logging.getLogger("stock_data_collection")


class CollectionError(RuntimeError):
    """Raised when a download cannot be accepted as a valid dataset."""


@dataclass(frozen=True)
class ValidationResult:
    """Machine-readable outcome of the validation rules for one security."""

    ticker: str
    status: str
    expected_sessions: int
    observed_rows: int
    missing_sessions: int
    unexpected_dates: int
    duplicate_dates: int
    non_monotonic_dates: int
    missing_values: int
    non_finite_values: int
    non_positive_prices: int
    ohlc_range_violations: int
    negative_volume: int
    fractional_volume: int
    first_date: str
    last_date: str
    error: str

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class DiagnosticRecord:
    """Sanitized, machine-readable record of one failed processing attempt."""

    ticker: str
    attempt: int
    stage: str
    error_type: str
    error_message: str
    timestamp_utc: str


def utc_timestamp() -> str:
    """Return a compact UTC timestamp without sub-second noise."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_error_message(message: object) -> str:
    """Remove proxy credentials and configured proxy values from diagnostics."""

    sanitized = str(message)
    for variable in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        proxy_value = os.environ.get(variable)
        if proxy_value:
            sanitized = sanitized.replace(proxy_value, "[REDACTED_PROXY]")
    sanitized = re.sub(
        r"(?i)\b(https?|socks5h?|socks4a?)://[^\s/@]+@",
        r"\1://[REDACTED]@",
        sanitized,
    )
    return sanitized[:1000]


def add_diagnostic(
    records: list[DiagnosticRecord],
    *,
    ticker: str,
    attempt: int,
    stage: str,
    error_type: str,
    error_message: object,
) -> None:
    """Append one sanitized diagnostic record."""

    records.append(
        DiagnosticRecord(
            ticker=ticker,
            attempt=attempt,
            stage=stage,
            error_type=error_type,
            error_message=sanitize_error_message(error_message),
            timestamp_utc=utc_timestamp(),
        )
    )


def parse_iso_date(value: str) -> date:
    """Parse an ISO 8601 calendar date for argparse."""

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Expected YYYY-MM-DD."
        ) from exc


def normalize_tickers(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize, validate, and de-duplicate ticker symbols in input order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        for part in raw_value.split(","):
            ticker = part.strip().upper()
            if not ticker:
                continue
            if not all(character.isalnum() or character in ".-" for character in ticker):
                raise argparse.ArgumentTypeError(
                    f"Unsupported ticker format: '{ticker}'."
                )
            if ticker not in seen:
                normalized.append(ticker)
                seen.add(ticker)
    if not normalized:
        raise argparse.ArgumentTypeError("At least one ticker is required.")
    return tuple(normalized)


def load_company_config(path: Path) -> dict[str, str]:
    """Load the ordered ticker-to-company mapping used by the default run."""

    try:
        companies = pd.read_csv(
            path,
            dtype={"ticker": "string", "company": "string"},
            keep_default_na=False,
        )
    except (OSError, pd.errors.ParserError) as exc:
        raise CollectionError(f"Could not read company config '{path}': {exc}") from exc

    required = ("ticker", "company")
    missing_columns = [column for column in required if column not in companies]
    if missing_columns:
        raise CollectionError(
            "Company config is missing required columns: "
            + ", ".join(missing_columns)
            + "."
        )
    if companies.empty:
        raise CollectionError("Company config must contain at least one security.")

    mapping: dict[str, str] = {}
    for row_number, record in enumerate(
        companies.loc[:, list(required)].to_dict(orient="records"), start=2
    ):
        raw_ticker = str(record["ticker"]).strip()
        company = str(record["company"]).strip()
        try:
            normalized = normalize_tickers([raw_ticker])
        except argparse.ArgumentTypeError as exc:
            raise CollectionError(
                f"Invalid ticker in company config row {row_number}: {exc}"
            ) from exc
        if len(normalized) != 1:
            raise CollectionError(
                f"Company config row {row_number} must contain exactly one ticker."
            )
        ticker = normalized[0]
        if not company:
            raise CollectionError(
                f"Company config row {row_number} has an empty company name."
            )
        if ticker in mapping:
            raise CollectionError(f"Company config contains duplicate ticker '{ticker}'.")
        mapping[ticker] = company
    return mapping


def expected_sessions(start: date, end: date, calendar_name: str) -> pd.DatetimeIndex:
    """Return expected exchange sessions within an inclusive date interval."""

    try:
        calendar = mcal.get_calendar(calendar_name)
    except RuntimeError as exc:
        raise CollectionError(f"Unknown market calendar '{calendar_name}'.") from exc
    schedule = calendar.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    sessions = pd.DatetimeIndex(schedule.index)
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    return sessions.normalize()


def download_ticker(
    ticker: str,
    start: date,
    end: date,
    max_retries: int,
    retry_delay_seconds: float,
    diagnostics: list[DiagnosticRecord] | None = None,
) -> pd.DataFrame:
    """Download one ticker with bounded retries and explicit price semantics."""

    exclusive_end = end + timedelta(days=1)
    latest_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            LOGGER.info(
                "Downloading %s (%s to %s inclusive), attempt %d/%d.",
                ticker,
                start,
                end,
                attempt,
                max_retries,
            )
            frame = yf.download(
                tickers=ticker,
                start=start.isoformat(),
                end=exclusive_end.isoformat(),
                interval="1d",
                auto_adjust=False,
                repair=False,
                actions=False,
                progress=False,
                threads=False,
                ignore_tz=True,
                keepna=False,
                rounding=False,
                timeout=30,
                multi_level_index=False,
            )
            if frame is None or frame.empty:
                raise CollectionError("Yahoo Finance returned no rows.")
            return normalize_download(frame)
        except Exception as exc:  # Network and provider exceptions are heterogeneous.
            error_type = type(exc).__name__
            error_message = sanitize_error_message(exc)
            latest_error = f"{error_type}: {error_message}"
            if diagnostics is not None:
                add_diagnostic(
                    diagnostics,
                    ticker=ticker,
                    attempt=attempt,
                    stage="download_or_normalize",
                    error_type=error_type,
                    error_message=error_message,
                )
            LOGGER.warning("%s attempt %d failed: %s", ticker, attempt, latest_error)
            if attempt < max_retries:
                time.sleep(retry_delay_seconds * (2 ** (attempt - 1)))
    raise CollectionError(
        f"Download failed after {max_retries} attempts for {ticker}: {latest_error}"
    )


def normalize_download(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert a yfinance daily response to the required flat CSV schema."""

    normalized = frame.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        if normalized.columns.nlevels != 2:
            raise CollectionError("Unexpected yfinance column index depth.")
        unique_last_level = normalized.columns.get_level_values(-1).unique()
        if len(unique_last_level) != 1:
            raise CollectionError("Response contains more than one ticker.")
        normalized.columns = normalized.columns.get_level_values(0)

    normalized.index = pd.to_datetime(normalized.index, errors="coerce")
    if normalized.index.isna().any():
        raise CollectionError("One or more response dates could not be parsed.")
    if normalized.index.tz is not None:
        normalized.index = normalized.index.tz_localize(None)
    normalized.index = normalized.index.normalize()
    normalized.index.name = "Date"

    missing_columns = [column for column in OUTPUT_COLUMNS[1:] if column not in normalized]
    if missing_columns:
        raise CollectionError(f"Required columns absent: {', '.join(missing_columns)}.")

    normalized = normalized.loc[:, list(OUTPUT_COLUMNS[1:])].reset_index()
    for column in OUTPUT_COLUMNS[1:]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized


def validate_frame(
    ticker: str,
    frame: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    start: date,
    end: date,
    calendar_name: str = DEFAULT_CALENDAR,
) -> ValidationResult:
    """Validate schema, completeness, uniqueness, values, and OHLC coherence."""

    errors: list[str] = []
    if tuple(frame.columns) != OUTPUT_COLUMNS:
        errors.append(
            "Schema mismatch: expected " + ", ".join(OUTPUT_COLUMNS) + "."
        )

    if "Date" in frame:
        parsed_dates = pd.to_datetime(frame["Date"], errors="coerce")
    else:
        parsed_dates = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
        errors.append("Required Date column is absent.")
    invalid_dates = int(parsed_dates.isna().sum())
    if invalid_dates:
        errors.append(f"{invalid_dates} unparseable date value(s).")
    observed_dates = pd.DatetimeIndex(parsed_dates.dropna()).normalize()
    duplicate_dates = int(observed_dates.duplicated().sum())
    non_monotonic_dates = int(not observed_dates.is_monotonic_increasing)
    missing_session_dates = sessions.difference(observed_dates)
    unexpected_session_dates = observed_dates.difference(sessions)

    out_of_bounds = int(
        ((observed_dates.date < start) | (observed_dates.date > end)).sum()
    )
    if out_of_bounds:
        errors.append(f"{out_of_bounds} row(s) outside the requested interval.")
    if duplicate_dates:
        errors.append(f"{duplicate_dates} duplicate trading date(s).")
    if non_monotonic_dates:
        errors.append("Trading dates are not in ascending order.")
    if len(missing_session_dates):
        preview = ", ".join(timestamp.date().isoformat() for timestamp in missing_session_dates[:5])
        errors.append(
            f"{len(missing_session_dates)} expected {calendar_name} session(s) missing"
            + (f" (first: {preview})." if preview else ".")
        )
    if len(unexpected_session_dates):
        preview = ", ".join(
            timestamp.date().isoformat() for timestamp in unexpected_session_dates[:5]
        )
        errors.append(
            f"{len(unexpected_session_dates)} unexpected date(s)"
            + (f" (first: {preview})." if preview else ".")
        )

    numeric_columns = [column for column in OUTPUT_COLUMNS[1:] if column in frame]
    missing_values = int(frame[numeric_columns].isna().sum().sum()) if numeric_columns else 0
    if missing_values:
        errors.append(f"{missing_values} missing numeric value(s).")

    non_finite_values = 0
    if numeric_columns:
        non_finite_values = int(
            (~frame[numeric_columns].map(lambda value: math.isfinite(value) if pd.notna(value) else True))
            .sum()
            .sum()
        )
    if non_finite_values:
        errors.append(f"{non_finite_values} non-finite numeric value(s).")

    non_positive_prices = 0
    if all(column in frame for column in PRICE_COLUMNS):
        non_positive_prices = int((frame[list(PRICE_COLUMNS)] <= 0).sum().sum())
    if non_positive_prices:
        errors.append(f"{non_positive_prices} non-positive price value(s).")

    ohlc_range_violations = 0
    if all(column in frame for column in ("Open", "High", "Low", "Close")):
        upper_bound = frame[["Open", "Low", "Close"]].max(axis=1)
        lower_bound = frame[["Open", "High", "Close"]].min(axis=1)
        ohlc_range_violations = int(
            ((frame["High"] < upper_bound) | (frame["Low"] > lower_bound)).sum()
        )
    if ohlc_range_violations:
        errors.append(f"{ohlc_range_violations} OHLC range violation(s).")

    negative_volume = 0
    fractional_volume = 0
    if "Volume" in frame:
        negative_volume = int((frame["Volume"] < 0).sum())
        finite_volume = frame.loc[frame["Volume"].notna(), "Volume"]
        fractional_volume = int((finite_volume % 1 != 0).sum())
    if negative_volume:
        errors.append(f"{negative_volume} negative volume value(s).")
    if fractional_volume:
        errors.append(f"{fractional_volume} fractional volume value(s).")

    if invalid_dates:
        first_date = ""
        last_date = ""
    elif observed_dates.empty:
        first_date = ""
        last_date = ""
    else:
        first_date = observed_dates.min().date().isoformat()
        last_date = observed_dates.max().date().isoformat()

    return ValidationResult(
        ticker=ticker,
        status="FAIL" if errors else "PASS",
        expected_sessions=len(sessions),
        observed_rows=len(frame),
        missing_sessions=len(missing_session_dates),
        unexpected_dates=len(unexpected_session_dates),
        duplicate_dates=duplicate_dates,
        non_monotonic_dates=non_monotonic_dates,
        missing_values=missing_values,
        non_finite_values=non_finite_values,
        non_positive_prices=non_positive_prices,
        ohlc_range_violations=ohlc_range_violations,
        negative_volume=negative_volume,
        fractional_volume=fractional_volume,
        first_date=first_date,
        last_date=last_date,
        error=" | ".join(errors),
    )


def load_reference_close_sample(path: Path) -> pd.DataFrame:
    """Load and validate a small independently sourced closing-price sample."""

    try:
        sample = pd.read_csv(path, dtype={"ticker": "string", "date": "string"})
    except (OSError, pd.errors.ParserError) as exc:
        raise CollectionError(f"Could not read reference close file '{path}': {exc}") from exc

    missing_columns = [column for column in REFERENCE_INPUT_COLUMNS if column not in sample]
    if missing_columns:
        raise CollectionError(
            "Reference close file is missing required columns: "
            + ", ".join(missing_columns)
            + "."
        )
    sample = sample.loc[:, list(REFERENCE_INPUT_COLUMNS)].copy()
    sample["ticker"] = sample["ticker"].str.strip().str.upper()
    parsed_dates = pd.to_datetime(sample["date"], format="%Y-%m-%d", errors="coerce")
    if parsed_dates.isna().any():
        raise CollectionError("Reference close file contains an invalid ISO date.")
    sample["date"] = parsed_dates.dt.strftime("%Y-%m-%d")
    sample["reference_close"] = pd.to_numeric(sample["reference_close"], errors="coerce")
    if sample["reference_close"].isna().any() or (
        ~sample["reference_close"].map(math.isfinite)
    ).any():
        raise CollectionError("Reference close file contains a non-numeric close value.")
    if (sample["reference_close"] <= 0).any():
        raise CollectionError("Reference close values must be positive.")
    if sample[["ticker", "date"]].duplicated().any():
        raise CollectionError("Reference close file contains a duplicate ticker/date pair.")
    for column in ("reference_source", "reference_url", "reference_retrieved_at_utc"):
        sample[column] = sample[column].astype("string").str.strip()
        if sample[column].eq("").any():
            raise CollectionError(f"Reference close file contains an empty {column}.")
    for reference_url in sample["reference_url"]:
        parsed_url = urlparse(str(reference_url))
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise CollectionError("Reference URLs must be absolute HTTPS URLs.")
    for timestamp in sample["reference_retrieved_at_utc"]:
        try:
            parsed_timestamp = datetime.fromisoformat(
                str(timestamp).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise CollectionError(
                "Reference retrieval timestamps must be valid ISO 8601 values."
            ) from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise CollectionError(
                "Reference retrieval timestamps must include a UTC offset."
            )
    return sample


def compare_reference_closes(
    frames: dict[str, pd.DataFrame],
    requested_tickers: Sequence[str],
    reference_sample: pd.DataFrame,
    tolerance: float = DEFAULT_REFERENCE_TOLERANCE,
) -> pd.DataFrame:
    """Compare provider closes with an independent, explicitly cited sample."""

    if tolerance < 0:
        raise CollectionError("reference_tolerance cannot be negative.")

    requested = set(requested_tickers)
    rows: list[dict[str, object]] = []
    for record in reference_sample.to_dict(orient="records"):
        ticker = str(record["ticker"])
        if ticker not in requested:
            continue
        comparison: dict[str, object] = {
            "ticker": ticker,
            "date": str(record["date"]),
            "yahoo_close": None,
            "reference_close": float(record["reference_close"]),
            "difference": None,
            "absolute_difference": None,
            "tolerance": tolerance,
            "status": "NOT_CHECKED",
            "reference_source": record["reference_source"],
            "reference_url": record["reference_url"],
            "reference_retrieved_at_utc": record["reference_retrieved_at_utc"],
        }
        frame = frames.get(ticker)
        if frame is not None:
            dates = pd.to_datetime(frame["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
            matches = frame.loc[dates == record["date"], "Close"]
            if matches.empty:
                comparison["status"] = "NOT_FOUND"
            else:
                yahoo_close = float(matches.iloc[0])
                difference = yahoo_close - float(record["reference_close"])
                comparison.update(
                    {
                        "yahoo_close": yahoo_close,
                        "difference": difference,
                        "absolute_difference": abs(difference),
                        "status": "PASS"
                        if abs(difference) <= tolerance + 1e-12
                        else "FAIL",
                    }
                )
        rows.append(comparison)
    return pd.DataFrame(rows, columns=REFERENCE_OUTPUT_COLUMNS)


def atomic_csv_write(frame: pd.DataFrame, destination: Path, **kwargs: object) -> None:
    """Write a CSV atomically so an interrupted run cannot leave a partial file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        frame.to_csv(temporary_path, index=False, encoding="utf-8", lineterminator="\n", **kwargs)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(payload: dict[str, object], destination: Path) -> None:
    """Write deterministic, human-readable UTF-8 JSON."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary.write(serialized)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def new_run_id() -> str:
    """Return a sortable, collision-resistant identifier for one collection run."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def write_failed_run_artifacts(
    *,
    output_dir: Path,
    run_id: str,
    validation_frame: pd.DataFrame,
    diagnostic_frame: pd.DataFrame,
    comparison_frame: pd.DataFrame,
    include_reference_comparison: bool,
    run_metadata: dict[str, object],
) -> Path:
    """Persist diagnostics without modifying the last published snapshot."""

    failed_dir = output_dir / "failed_runs" / run_id
    atomic_csv_write(validation_frame, failed_dir / "validation_summary.csv")
    atomic_csv_write(diagnostic_frame, failed_dir / "failure_report.csv")
    if include_reference_comparison:
        atomic_csv_write(
            comparison_frame,
            failed_dir / "reference_close_comparison.csv",
            float_format="%.8f",
        )
    write_json(run_metadata, failed_dir / "run_metadata.json")
    return failed_dir


def publish_snapshot(
    *,
    staging_root: Path,
    output_dir: Path,
    run_id: str,
    final_run_metadata: dict[str, object],
) -> None:
    """Publish one complete data-directory snapshot, rolling back on failure.

    The staged metadata initially has a conservative ``PENDING_PUBLICATION``
    status. Existing auxiliary artifacts (for example ``failed_runs/``) are
    copied into the staged snapshot. The final success/partial status is written
    only after the complete staged directory is live. Any error restores the
    preceding snapshot.
    """

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    names = ("raw", "metadata")
    for name in names:
        if not (staging_root / name).is_dir():
            raise CollectionError(f"Staged snapshot is missing its {name}/ directory.")

    if output_dir.exists() and not output_dir.is_dir():
        raise CollectionError(f"Output path is not a directory: {output_dir}")
    if output_dir.exists():
        for existing in output_dir.iterdir():
            if existing.name in names:
                continue
            staged_destination = staging_root / existing.name
            if staged_destination.exists():
                raise CollectionError(
                    f"Staged snapshot conflicts with existing artifact '{existing.name}'."
                )
            if existing.is_dir():
                shutil.copytree(existing, staged_destination)
            else:
                shutil.copy2(existing, staged_destination)

    backup_root = output_dir.parent / f".{output_dir.name}_publish_backup_{run_id}"
    if backup_root.exists():
        raise CollectionError(f"Publication backup already exists: {backup_root}")
    backed_up = False
    published = False
    preserve_backup = False

    try:
        if output_dir.exists():
            os.replace(output_dir, backup_root)
            backed_up = True
        os.replace(staging_root, output_dir)
        published = True

        committed_metadata = dict(final_run_metadata)
        committed_metadata["publication_completed_at_utc"] = utc_timestamp()
        write_json(committed_metadata, output_dir / "metadata" / "run_metadata.json")
    except Exception as publish_error:
        rollback_errors: list[str] = []
        try:
            if published and output_dir.exists():
                shutil.rmtree(output_dir)
            if backed_up and backup_root.exists():
                os.replace(backup_root, output_dir)
        except Exception as rollback_error:  # pragma: no cover - catastrophic I/O
            rollback_errors.append(sanitize_error_message(rollback_error))
        message = "Snapshot publication failed and the prior snapshot was restored"
        if rollback_errors:
            preserve_backup = True
            message += "; rollback errors: " + " | ".join(rollback_errors)
            message += f"; recovery backup retained at {backup_root}"
        raise CollectionError(
            f"{message}. Cause: {sanitize_error_message(publish_error)}"
        ) from publish_error
    finally:
        if backup_root.exists() and not preserve_backup:
            shutil.rmtree(backup_root)


def collect(
    tickers: Sequence[str],
    start: date,
    end: date,
    calendar_name: str,
    output_dir: Path,
    max_retries: int,
    retry_delay_seconds: float,
    failure_policy: str = FAIL_CLOSED,
    reference_close_file: Path | None = None,
    reference_tolerance: float = DEFAULT_REFERENCE_TOLERANCE,
    company_names: dict[str, str] | None = None,
    companies_file: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collect, validate, and transactionally publish requested datasets."""

    if start > end:
        raise CollectionError("Start date must be on or before end date.")
    if max_retries < 1:
        raise CollectionError("max_retries must be at least 1.")
    if retry_delay_seconds < 0:
        raise CollectionError("retry_delay_seconds cannot be negative.")
    if failure_policy not in (FAIL_CLOSED, ALLOW_PARTIAL):
        raise CollectionError(f"Unsupported failure policy '{failure_policy}'.")
    if reference_tolerance < 0:
        raise CollectionError("reference_tolerance cannot be negative.")

    try:
        normalized_tickers = normalize_tickers(tickers)
    except argparse.ArgumentTypeError as exc:
        raise CollectionError(str(exc)) from exc
    effective_company_names = company_names or {}
    sessions = expected_sessions(start, end, calendar_name)
    if sessions.empty:
        raise CollectionError("The requested interval contains no scheduled market sessions.")
    calendar_implementation = mcal.get_calendar(calendar_name).name

    run_id = new_run_id()
    collection_started_at = utc_timestamp()
    frames: dict[str, pd.DataFrame] = {}
    results: list[ValidationResult] = []
    diagnostics: list[DiagnosticRecord] = []

    for ticker in normalized_tickers:
        diagnostic_count_before = len(diagnostics)
        try:
            frame = download_ticker(
                ticker=ticker,
                start=start,
                end=end,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
                diagnostics=diagnostics,
            )
            validation = validate_frame(
                ticker, frame, sessions, start, end, calendar_name=calendar_name
            )
            frames[ticker] = frame
            if not validation.passed:
                add_diagnostic(
                    diagnostics,
                    ticker=ticker,
                    attempt=0,
                    stage="validation",
                    error_type="ValidationError",
                    error_message=validation.error,
                )
        except Exception as exc:
            error_message = sanitize_error_message(exc)
            if len(diagnostics) == diagnostic_count_before:
                add_diagnostic(
                    diagnostics,
                    ticker=ticker,
                    attempt=0,
                    stage="collection",
                    error_type=type(exc).__name__,
                    error_message=error_message,
                )
            validation = ValidationResult(
                ticker=ticker,
                status="FAIL",
                expected_sessions=len(sessions),
                observed_rows=0,
                missing_sessions=len(sessions),
                unexpected_dates=0,
                duplicate_dates=0,
                non_monotonic_dates=0,
                missing_values=0,
                non_finite_values=0,
                non_positive_prices=0,
                ohlc_range_violations=0,
                negative_volume=0,
                fractional_volume=0,
                first_date="",
                last_date="",
                error=f"{type(exc).__name__}: {error_message}",
            )
        results.append(validation)

    comparison_frame = pd.DataFrame(columns=REFERENCE_OUTPUT_COLUMNS)
    if reference_close_file is not None:
        reference_sample = load_reference_close_sample(reference_close_file)
        internally_valid_frames = {
            result.ticker: frames[result.ticker]
            for result in results
            if result.passed and result.ticker in frames
        }
        comparison_frame = compare_reference_closes(
            internally_valid_frames,
            normalized_tickers,
            reference_sample,
            reference_tolerance,
        )
        comparison_errors: dict[str, list[str]] = {}
        for row in comparison_frame.to_dict(orient="records"):
            if row["status"] not in ("FAIL", "NOT_FOUND"):
                continue
            message = (
                f"Reference close comparison {row['status']} for {row['date']} "
                f"(tolerance {reference_tolerance:g})."
            )
            comparison_errors.setdefault(str(row["ticker"]), []).append(message)
            add_diagnostic(
                diagnostics,
                ticker=str(row["ticker"]),
                attempt=0,
                stage="reference_comparison",
                error_type="ReferenceComparisonError",
                error_message=message,
            )
        results = [
            replace(
                result,
                status="FAIL",
                error=" | ".join(
                    part
                    for part in (result.error, *comparison_errors[result.ticker])
                    if part
                ),
            )
            if result.ticker in comparison_errors
            else result
            for result in results
        ]

    validation_frame = pd.DataFrame([asdict(result) for result in results])
    diagnostic_frame = pd.DataFrame(
        [asdict(record) for record in diagnostics], columns=FAILURE_REPORT_COLUMNS
    )

    failures = [result for result in results if not result.passed]
    successful_results = [result for result in results if result.passed]
    if failures and failure_policy == FAIL_CLOSED:
        run_status = "FAILED"
        publish_results: list[ValidationResult] = []
    elif failures:
        run_status = "PARTIAL"
        publish_results = successful_results
    else:
        run_status = "SUCCESS"
        publish_results = successful_results

    base_run_metadata: dict[str, object] = {
        "author": AUTHOR,
        "calendar": calendar_name,
        "calendar_implementation": calendar_implementation,
        "collection_completed_at_utc": utc_timestamp(),
        "collection_started_at_utc": collection_started_at,
        "command_end_is_inclusive": True,
        "companies_file": str(companies_file) if companies_file else None,
        "failed_tickers": [result.ticker for result in failures],
        "failure_policy": failure_policy,
        "generated_at_utc": collection_started_at,
        "interval": "1d",
        "network_proxy_configured": bool(
            os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        ),
        "no_interpolation_or_price_repair": True,
        "output_columns": list(OUTPUT_COLUMNS),
        "platform": platform.platform(),
        "published_tickers": [result.ticker for result in publish_results],
        "python_version": platform.python_version(),
        "reference_close_file": str(reference_close_file) if reference_close_file else None,
        "reference_tolerance": reference_tolerance if reference_close_file else None,
        "requested_end": end.isoformat(),
        "requested_start": start.isoformat(),
        "requested_tickers": list(normalized_tickers),
        "run_id": run_id,
        "tickers": list(normalized_tickers),
        "versions": {
            "pandas": pd.__version__,
            "pandas_market_calendars": mcal.__version__,
            "yfinance": yf.__version__,
        },
    }

    if failures and failure_policy == FAIL_CLOSED:
        failed_metadata = {
            **base_run_metadata,
            "failure_report": f"failed_runs/{run_id}/failure_report.csv",
            "published_tickers": [],
            "run_status": "FAILED",
        }
        write_failed_run_artifacts(
            output_dir=output_dir,
            run_id=run_id,
            validation_frame=validation_frame,
            diagnostic_frame=diagnostic_frame,
            comparison_frame=comparison_frame,
            include_reference_comparison=reference_close_file is not None,
            run_metadata=failed_metadata,
        )
        failure_text = "; ".join(
            f"{result.ticker}: {result.error}" for result in failures
        )
        raise CollectionError(
            "Validation failed; the current published snapshot was left unchanged. "
            + failure_text
        )
    if not publish_results:
        failed_metadata = {
            **base_run_metadata,
            "failure_report": f"failed_runs/{run_id}/failure_report.csv",
            "published_tickers": [],
            "run_status": "FAILED",
        }
        write_failed_run_artifacts(
            output_dir=output_dir,
            run_id=run_id,
            validation_frame=validation_frame,
            diagnostic_frame=diagnostic_frame,
            comparison_frame=comparison_frame,
            include_reference_comparison=reference_close_file is not None,
            run_metadata=failed_metadata,
        )
        raise CollectionError(
            "No ticker passed validation; failed-run diagnostics were written and "
            "the current published snapshot was left unchanged."
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".stock_run_{run_id}_", dir=output_dir.parent)
    )
    try:
        staged_raw_dir = staging_root / "raw"
        staged_metadata_dir = staging_root / "metadata"
        staged_raw_dir.mkdir()
        staged_metadata_dir.mkdir()

        manifest_rows: list[dict[str, object]] = []
        for result in publish_results:
            ticker = result.ticker
            frame = frames[ticker].copy()
            frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
            frame["Volume"] = frame["Volume"].astype("int64")
            file_name = f"{ticker}_daily_{start.isoformat()}_{end.isoformat()}.csv"
            destination = staged_raw_dir / file_name
            atomic_csv_write(frame, destination)
            manifest_rows.append(
                {
                    "run_id": run_id,
                    "ticker": ticker,
                    "company": effective_company_names.get(ticker, ""),
                    "file": f"raw/{file_name}",
                    "source_url": f"https://finance.yahoo.com/quote/{ticker}/history/",
                    "retrieved_at_utc": collection_started_at,
                    "requested_start": start.isoformat(),
                    "requested_end_inclusive": end.isoformat(),
                    "first_observation": result.first_date,
                    "last_observation": result.last_date,
                    "row_count": result.observed_rows,
                    "sha256": sha256_file(destination),
                    "market_calendar": calendar_name,
                    "market_calendar_implementation": calendar_implementation,
                    "interval": "1d",
                    "auto_adjust": False,
                    "repair": False,
                    "price_columns": "Open; High; Low; Close; Adj Close",
                    "volume_unit": "shares",
                    "yfinance_version": yf.__version__,
                    "pandas_version": pd.__version__,
                    "network_proxy_configured": bool(
                        os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
                    ),
                    "failure_policy": failure_policy,
                    "run_status": run_status,
                }
            )

        manifest_frame = pd.DataFrame(manifest_rows)
        atomic_csv_write(manifest_frame, staged_metadata_dir / "manifest.csv")
        atomic_csv_write(
            validation_frame, staged_metadata_dir / "validation_summary.csv"
        )
        atomic_csv_write(diagnostic_frame, staged_metadata_dir / "failure_report.csv")
        if reference_close_file is not None:
            atomic_csv_write(
                comparison_frame,
                staged_metadata_dir / "reference_close_comparison.csv",
                float_format="%.8f",
            )

        pending_metadata = {
            **base_run_metadata,
            "failure_report": "metadata/failure_report.csv",
            "intended_run_status": run_status,
            "run_status": "PENDING_PUBLICATION",
        }
        write_json(pending_metadata, staged_metadata_dir / "run_metadata.json")
        final_metadata = {
            **base_run_metadata,
            "failure_report": "metadata/failure_report.csv",
            "run_status": run_status,
        }
        publish_snapshot(
            staging_root=staging_root,
            output_dir=output_dir,
            run_id=run_id,
            final_run_metadata=final_metadata,
        )
        return manifest_frame, validation_frame
    except Exception as exc:
        add_diagnostic(
            diagnostics,
            ticker="_RUN_",
            attempt=0,
            stage="publication",
            error_type=type(exc).__name__,
            error_message=exc,
        )
        diagnostic_frame = pd.DataFrame(
            [asdict(record) for record in diagnostics], columns=FAILURE_REPORT_COLUMNS
        )
        failed_metadata = {
            **base_run_metadata,
            "failure_report": f"failed_runs/{run_id}/failure_report.csv",
            "published_tickers": [],
            "publication_error": sanitize_error_message(exc),
            "run_status": "FAILED",
        }
        try:
            write_failed_run_artifacts(
                output_dir=output_dir,
                run_id=run_id,
                validation_frame=validation_frame,
                diagnostic_frame=diagnostic_frame,
                comparison_frame=comparison_frame,
                include_reference_comparison=reference_close_file is not None,
                run_metadata=failed_metadata,
            )
        except Exception as diagnostic_error:  # pragma: no cover - secondary I/O failure
            LOGGER.error(
                "Could not persist failed-run diagnostics: %s",
                sanitize_error_message(diagnostic_error),
            )
        raise CollectionError(
            "Publication failed; the current published snapshot was left unchanged. "
            + sanitize_error_message(exc)
        ) from exc
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


def build_parser() -> argparse.ArgumentParser:
    """Construct the command-line interface."""

    parser = argparse.ArgumentParser(
        description=(
            "Download daily historical stock prices from Yahoo Finance, validate "
            "them against an exchange calendar, and save auditable CSV files."
        )
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help=(
            "Ticker symbols separated by spaces or commas. If omitted, tickers "
            "are loaded from --companies-file."
        ),
    )
    parser.add_argument(
        "--companies-file",
        type=Path,
        default=DEFAULT_COMPANIES_FILE,
        help=(
            "CSV containing at least ticker and company columns; it defines the "
            "default ordered universe and manifest company names."
        ),
    )
    parser.add_argument(
        "--start",
        type=parse_iso_date,
        default=DEFAULT_START,
        help=f"Inclusive start date (default: {DEFAULT_START}).",
    )
    parser.add_argument(
        "--end",
        type=parse_iso_date,
        default=DEFAULT_END,
        help=f"Inclusive end date (default: {DEFAULT_END}).",
    )
    parser.add_argument(
        "--calendar",
        default=DEFAULT_CALENDAR,
        help=f"pandas-market-calendars name (default: {DEFAULT_CALENDAR}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data",
        help="Output directory containing raw/ and metadata/.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum provider requests per ticker (default: 3).",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=2.0,
        help="Initial exponential-backoff delay (default: 2.0).",
    )
    failure_group = parser.add_mutually_exclusive_group()
    failure_group.add_argument(
        "--fail-closed",
        dest="failure_policy",
        action="store_const",
        const=FAIL_CLOSED,
        default=FAIL_CLOSED,
        help=(
            "Reject the entire run if any ticker fails (default); diagnostics are "
            "still written."
        ),
    )
    failure_group.add_argument(
        "--allow-partial-with-failure-report",
        dest="failure_policy",
        action="store_const",
        const=ALLOW_PARTIAL,
        help=(
            "Publish passing tickers when at least one ticker fails and write a "
            "machine-readable failure report."
        ),
    )
    parser.add_argument(
        "--reference-close-file",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "config"
        / "reference_close_sample.csv",
        help=(
            "Independent close-price sample used for a small external accuracy "
            "cross-check."
        ),
    )
    parser.add_argument(
        "--reference-tolerance",
        type=float,
        default=DEFAULT_REFERENCE_TOLERANCE,
        help="Maximum absolute close difference accepted (default: 0.01).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "logs" / "collection.log",
        help="UTF-8 log-file path.",
    )
    return parser


def configure_logging(log_file: Path) -> None:
    """Configure concise console and persistent UTF-8 logging."""

    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)sZ | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime

    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the collection workflow and return a process exit code."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        companies_file = arguments.companies_file.resolve()
        company_names = load_company_config(companies_file)
        tickers = (
            normalize_tickers(arguments.tickers)
            if arguments.tickers is not None
            else tuple(company_names)
        )
    except (argparse.ArgumentTypeError, CollectionError) as exc:
        parser.error(str(exc))

    configure_logging(arguments.log_file.resolve())
    try:
        manifest, validation = collect(
            tickers=tickers,
            start=arguments.start,
            end=arguments.end,
            calendar_name=arguments.calendar,
            output_dir=arguments.output_dir.resolve(),
            max_retries=arguments.max_retries,
            retry_delay_seconds=arguments.retry_delay_seconds,
            failure_policy=arguments.failure_policy,
            reference_close_file=arguments.reference_close_file.resolve(),
            reference_tolerance=arguments.reference_tolerance,
            company_names=company_names,
            companies_file=companies_file,
        )
    except CollectionError as exc:
        LOGGER.error("%s", exc)
        return 1

    failed_count = int((validation["status"] == "FAIL").sum())
    if failed_count:
        LOGGER.warning(
            "Completed in partial mode: %d ticker(s) published, %d failed; see "
            "data/metadata/failure_report.csv.",
            len(manifest),
            failed_count,
        )
    else:
        LOGGER.info(
            "Completed: %d ticker(s), %d total observations, all validation checks passed.",
            len(manifest),
            int(validation["observed_rows"].sum()),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
