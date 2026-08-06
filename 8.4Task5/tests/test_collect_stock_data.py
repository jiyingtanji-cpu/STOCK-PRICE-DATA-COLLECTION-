"""Unit tests for collection normalization and validation."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import collect_stock_data as collector  # noqa: E402
from collect_stock_data import (  # noqa: E402
    ALLOW_PARTIAL,
    FAIL_CLOSED,
    CollectionError,
    OUTPUT_COLUMNS,
    REFERENCE_INPUT_COLUMNS,
    build_parser,
    collect,
    compare_reference_closes,
    download_ticker,
    expected_sessions,
    load_company_config,
    load_reference_close_sample,
    normalize_tickers,
    sanitize_error_message,
    validate_frame,
)


def sample_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
            "Open": [100.0, 102.0],
            "High": [103.0, 104.0],
            "Low": [99.0, 101.0],
            "Close": [102.0, 103.0],
            "Adj Close": [101.5, 102.5],
            "Volume": [1_000_000, 1_200_000],
        }
    )


def sample_sessions() -> pd.DatetimeIndex:
    return pd.DatetimeIndex(["2025-01-02", "2025-01-03"])


def test_valid_frame_passes() -> None:
    result = validate_frame(
        "TEST",
        sample_frame(),
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert result.passed
    assert result.observed_rows == 2
    assert result.missing_sessions == 0


def test_missing_session_fails() -> None:
    result = validate_frame(
        "TEST",
        sample_frame().iloc[:1].copy(),
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert not result.passed
    assert result.missing_sessions == 1


def test_duplicate_date_fails() -> None:
    frame = pd.concat([sample_frame(), sample_frame().iloc[[1]]], ignore_index=True)
    result = validate_frame(
        "TEST",
        frame,
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert not result.passed
    assert result.duplicate_dates == 1


def test_non_monotonic_dates_fail() -> None:
    frame = sample_frame().iloc[::-1].reset_index(drop=True)
    result = validate_frame(
        "TEST",
        frame,
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert not result.passed
    assert result.non_monotonic_dates == 1


def test_ohlc_violation_fails() -> None:
    frame = sample_frame()
    frame.loc[0, "High"] = 98.0
    result = validate_frame(
        "TEST",
        frame,
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert not result.passed
    assert result.ohlc_range_violations == 1


def test_missing_and_fractional_values_fail() -> None:
    frame = sample_frame()
    frame.loc[0, "Open"] = float("nan")
    frame["Volume"] = frame["Volume"].astype("float64")
    frame.loc[1, "Volume"] = 1.5
    result = validate_frame(
        "TEST",
        frame,
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert not result.passed
    assert result.missing_values == 1
    assert result.fractional_volume == 1


def test_ticker_normalization_preserves_order_and_deduplicates() -> None:
    assert normalize_tickers(["aapl, msft", "AAPL", "brk-b"]) == (
        "AAPL",
        "MSFT",
        "BRK-B",
    )


def test_company_config_drives_default_universe_and_names(tmp_path) -> None:
    config = tmp_path / "companies.csv"
    config.write_text(
        "ticker,company,exchange\nmsft,Microsoft Corporation,NASDAQ\n"
        "aapl,Apple Inc.,NASDAQ\n",
        encoding="utf-8",
    )
    assert load_company_config(config) == {
        "MSFT": "Microsoft Corporation",
        "AAPL": "Apple Inc.",
    }


def test_company_config_rejects_duplicate_ticker(tmp_path) -> None:
    config = tmp_path / "companies.csv"
    config.write_text(
        "ticker,company\nAAPL,Apple Inc.\naapl,Duplicate Apple\n",
        encoding="utf-8",
    )
    with pytest.raises(CollectionError, match="duplicate ticker"):
        load_company_config(config)


def test_study_period_has_1255_expected_sessions() -> None:
    sessions = expected_sessions(date(2021, 1, 1), date(2025, 12, 31), "NASDAQ")
    assert len(sessions) == 1255
    assert sessions[0].date() == date(2021, 1, 4)
    assert sessions[-1].date() == date(2025, 12, 31)


def test_schema_constant_matches_required_order() -> None:
    assert OUTPUT_COLUMNS == (
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Adj Close",
        "Volume",
    )


def test_missing_date_column_returns_failed_validation() -> None:
    frame = sample_frame().drop(columns=["Date"])
    result = validate_frame(
        "TEST",
        frame,
        sample_sessions(),
        date(2025, 1, 1),
        date(2025, 1, 3),
    )
    assert not result.passed
    assert "Date column" in result.error


def reference_sample(reference_close: float = 102.00) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "TEST",
                "date": "2025-01-02",
                "reference_close": reference_close,
                "reference_source": "Independent test source",
                "reference_url": "https://example.test/reference",
                "reference_retrieved_at_utc": "2026-08-04T00:00:00Z",
            }
        ],
        columns=REFERENCE_INPUT_COLUMNS,
    )


def test_reference_close_comparison_accepts_source_rounding() -> None:
    frame = sample_frame()
    frame.loc[0, "Close"] = 102.004
    comparison = compare_reference_closes(
        {"TEST": frame}, ("TEST",), reference_sample(102.00), tolerance=0.01
    )
    assert comparison.loc[0, "status"] == "PASS"
    assert comparison.loc[0, "difference"] == pytest.approx(0.004)


def test_reference_close_comparison_flags_material_difference() -> None:
    comparison = compare_reference_closes(
        {"TEST": sample_frame()},
        ("TEST",),
        reference_sample(100.00),
        tolerance=0.01,
    )
    assert comparison.loc[0, "status"] == "FAIL"
    assert comparison.loc[0, "absolute_difference"] == pytest.approx(2.0)


def test_reference_sample_requires_https_url_and_zoned_retrieval_time(tmp_path) -> None:
    sample = reference_sample()
    sample.loc[0, "reference_url"] = "http://example.test/reference"
    path = tmp_path / "reference.csv"
    sample.to_csv(path, index=False)
    with pytest.raises(CollectionError, match="absolute HTTPS"):
        load_reference_close_sample(path)

    sample.loc[0, "reference_url"] = "https://example.test/reference"
    sample.loc[0, "reference_retrieved_at_utc"] = "2026-08-04T00:00:00"
    sample.to_csv(path, index=False)
    with pytest.raises(CollectionError, match="UTC offset"):
        load_reference_close_sample(path)


def test_retry_failures_are_preserved_as_structured_diagnostics(monkeypatch) -> None:
    responses = [
        pd.DataFrame(),
        pd.DataFrame({"Open": [1.0]}, index=pd.DatetimeIndex(["2025-01-02"])),
        sample_frame().set_index("Date"),
    ]
    monkeypatch.setattr(collector.yf, "download", lambda **kwargs: responses.pop(0))
    diagnostics = []
    frame = download_ticker(
        "TEST",
        date(2025, 1, 1),
        date(2025, 1, 3),
        max_retries=3,
        retry_delay_seconds=0,
        diagnostics=diagnostics,
    )
    assert len(frame) == 2
    assert [record.attempt for record in diagnostics] == [1, 2]
    assert all(record.stage == "download_or_normalize" for record in diagnostics)
    assert "no rows" in diagnostics[0].error_message
    assert "Required columns absent" in diagnostics[1].error_message


def test_proxy_credentials_are_redacted_from_diagnostics(monkeypatch) -> None:
    secret_proxy = "http://student:secret@proxy.example:8080"
    monkeypatch.setenv("HTTPS_PROXY", secret_proxy)
    sanitized = sanitize_error_message(
        f"Connection through {secret_proxy} and http://other:password@proxy.test failed"
    )
    assert "student" not in sanitized
    assert "secret" not in sanitized
    assert "password" not in sanitized
    assert "[REDACTED_PROXY]" in sanitized


def test_partial_mode_publishes_passes_and_failure_report(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        collector,
        "expected_sessions",
        lambda start, end, calendar_name: sample_sessions(),
    )

    def fake_download(*, ticker, **kwargs):
        if ticker == "BAD":
            raise CollectionError("simulated empty response")
        return sample_frame()

    monkeypatch.setattr(collector, "download_ticker", fake_download)
    manifest, validation = collect(
        tickers=("TEST", "BAD"),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        calendar_name="NASDAQ",
        output_dir=tmp_path,
        max_retries=1,
        retry_delay_seconds=0,
        failure_policy=ALLOW_PARTIAL,
        reference_close_file=None,
    )
    assert manifest["ticker"].tolist() == ["TEST"]
    assert validation.set_index("ticker").loc["BAD", "status"] == "FAIL"
    assert list((tmp_path / "raw").glob("TEST_*.csv"))
    assert not list((tmp_path / "raw").glob("BAD_*.csv"))
    failure_report = pd.read_csv(tmp_path / "metadata" / "failure_report.csv")
    assert failure_report.loc[0, "ticker"] == "BAD"
    metadata = json.loads((tmp_path / "metadata" / "run_metadata.json").read_text())
    assert metadata["run_status"] == "PARTIAL"
    assert metadata["failure_policy"] == ALLOW_PARTIAL
    assert metadata["calendar"] == "NASDAQ"
    assert metadata["calendar_implementation"] == "NYSE"
    assert metadata["publication_completed_at_utc"]
    assert metadata["run_id"] == manifest.loc[0, "run_id"]


def test_partial_mode_replaces_snapshot_and_removes_stale_failed_ticker(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "raw").mkdir()
    stale_file = tmp_path / "raw" / "BAD_daily_old.csv"
    stale_file.write_text("stale\n", encoding="utf-8")
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "manifest.csv").write_text(
        "ticker,file\nBAD,raw/BAD_daily_old.csv\n", encoding="utf-8"
    )
    previous_failure = tmp_path / "failed_runs" / "previous" / "failure_report.csv"
    previous_failure.parent.mkdir(parents=True)
    previous_failure.write_text("previous failure evidence\n", encoding="utf-8")
    monkeypatch.setattr(
        collector,
        "expected_sessions",
        lambda start, end, calendar_name: sample_sessions(),
    )

    def fake_download(*, ticker, **kwargs):
        if ticker == "BAD":
            raise CollectionError("simulated empty response")
        return sample_frame()

    monkeypatch.setattr(collector, "download_ticker", fake_download)
    manifest, _ = collect(
        tickers=("TEST", "BAD"),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        calendar_name="NASDAQ",
        output_dir=tmp_path,
        max_retries=1,
        retry_delay_seconds=0,
        failure_policy=ALLOW_PARTIAL,
        reference_close_file=None,
    )
    assert manifest["ticker"].tolist() == ["TEST"]
    assert not stale_file.exists()
    assert {path.name for path in (tmp_path / "raw").glob("*.csv")} == {
        "TEST_daily_2025-01-01_2025-01-03.csv"
    }
    assert previous_failure.read_text(encoding="utf-8") == "previous failure evidence\n"


def test_fail_closed_remains_default_and_publishes_no_raw_files(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        collector,
        "expected_sessions",
        lambda start, end, calendar_name: sample_sessions(),
    )

    def fake_download(*, ticker, **kwargs):
        if ticker == "BAD":
            raise CollectionError("simulated failure")
        return sample_frame()

    monkeypatch.setattr(collector, "download_ticker", fake_download)
    with pytest.raises(CollectionError, match="Validation failed"):
        collect(
            tickers=("TEST", "BAD"),
            start=date(2025, 1, 1),
            end=date(2025, 1, 3),
            calendar_name="NASDAQ",
            output_dir=tmp_path,
            max_retries=1,
            retry_delay_seconds=0,
            failure_policy=FAIL_CLOSED,
            reference_close_file=None,
        )
    assert not (tmp_path / "raw").exists()
    failed_runs = list((tmp_path / "failed_runs").iterdir())
    assert len(failed_runs) == 1
    assert (failed_runs[0] / "failure_report.csv").exists()
    failed_metadata = json.loads((failed_runs[0] / "run_metadata.json").read_text())
    assert failed_metadata["run_status"] == "FAILED"
    assert failed_metadata["published_tickers"] == []


def test_fail_closed_preserves_previous_published_snapshot(tmp_path, monkeypatch) -> None:
    (tmp_path / "raw").mkdir()
    old_raw = tmp_path / "raw" / "OLD.csv"
    old_raw.write_text("old raw\n", encoding="utf-8")
    (tmp_path / "metadata").mkdir()
    old_manifest = tmp_path / "metadata" / "manifest.csv"
    old_manifest.write_text("old manifest\n", encoding="utf-8")
    monkeypatch.setattr(
        collector,
        "expected_sessions",
        lambda start, end, calendar_name: sample_sessions(),
    )
    monkeypatch.setattr(
        collector,
        "download_ticker",
        lambda **kwargs: (_ for _ in ()).throw(CollectionError("simulated failure")),
    )
    with pytest.raises(CollectionError, match="left unchanged"):
        collect(
            tickers=("BAD",),
            start=date(2025, 1, 1),
            end=date(2025, 1, 3),
            calendar_name="NASDAQ",
            output_dir=tmp_path,
            max_retries=1,
            retry_delay_seconds=0,
            failure_policy=FAIL_CLOSED,
            reference_close_file=None,
        )
    assert old_raw.read_text(encoding="utf-8") == "old raw\n"
    assert old_manifest.read_text(encoding="utf-8") == "old manifest\n"


def test_staging_write_failure_never_publishes_success_and_preserves_snapshot(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "raw").mkdir()
    old_raw = tmp_path / "raw" / "OLD.csv"
    old_raw.write_text("old raw\n", encoding="utf-8")
    (tmp_path / "metadata").mkdir()
    old_metadata = tmp_path / "metadata" / "run_metadata.json"
    old_metadata.write_text('{"run_status":"SUCCESS","run_id":"old"}\n', encoding="utf-8")
    monkeypatch.setattr(
        collector,
        "expected_sessions",
        lambda start, end, calendar_name: sample_sessions(),
    )
    monkeypatch.setattr(collector, "download_ticker", lambda **kwargs: sample_frame())
    original_atomic_csv_write = collector.atomic_csv_write

    def fail_staged_raw(frame, destination, **kwargs):
        if destination.parent.name == "raw" and ".stock_run_" in str(destination):
            raise OSError("simulated staging disk failure")
        return original_atomic_csv_write(frame, destination, **kwargs)

    monkeypatch.setattr(collector, "atomic_csv_write", fail_staged_raw)
    with pytest.raises(CollectionError, match="Publication failed"):
        collect(
            tickers=("TEST",),
            start=date(2025, 1, 1),
            end=date(2025, 1, 3),
            calendar_name="NASDAQ",
            output_dir=tmp_path,
            max_retries=1,
            retry_delay_seconds=0,
            reference_close_file=None,
        )
    assert old_raw.read_text(encoding="utf-8") == "old raw\n"
    assert json.loads(old_metadata.read_text(encoding="utf-8"))["run_id"] == "old"
    failed_runs = list((tmp_path / "failed_runs").iterdir())
    failed_metadata = json.loads((failed_runs[0] / "run_metadata.json").read_text())
    assert failed_metadata["run_status"] == "FAILED"
    failure_report = pd.read_csv(failed_runs[0] / "failure_report.csv")
    publication = failure_report.loc[failure_report["stage"] == "publication"]
    assert len(publication) == 1
    assert "simulated staging disk failure" in publication.iloc[0]["error_message"]


def test_final_status_write_failure_rolls_back_both_snapshot_directories(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "raw").mkdir()
    old_raw = tmp_path / "raw" / "OLD.csv"
    old_raw.write_text("old raw\n", encoding="utf-8")
    (tmp_path / "metadata").mkdir()
    old_manifest = tmp_path / "metadata" / "manifest.csv"
    old_manifest.write_text("old manifest\n", encoding="utf-8")
    monkeypatch.setattr(
        collector,
        "expected_sessions",
        lambda start, end, calendar_name: sample_sessions(),
    )
    monkeypatch.setattr(collector, "download_ticker", lambda **kwargs: sample_frame())
    original_write_json = collector.write_json

    def fail_final_status(payload, destination):
        if (
            destination == tmp_path / "metadata" / "run_metadata.json"
            and payload.get("run_status") == "SUCCESS"
        ):
            raise OSError("simulated final metadata failure")
        return original_write_json(payload, destination)

    monkeypatch.setattr(collector, "write_json", fail_final_status)
    with pytest.raises(CollectionError, match="Publication failed"):
        collect(
            tickers=("TEST",),
            start=date(2025, 1, 1),
            end=date(2025, 1, 3),
            calendar_name="NASDAQ",
            output_dir=tmp_path,
            max_retries=1,
            retry_delay_seconds=0,
            reference_close_file=None,
        )
    assert old_raw.read_text(encoding="utf-8") == "old raw\n"
    assert old_manifest.read_text(encoding="utf-8") == "old manifest\n"
    assert not list(tmp_path.parent.glob(f".{tmp_path.name}_publish_backup_*"))


def test_cli_exposes_explicit_failure_modes() -> None:
    parser = build_parser()
    assert parser.parse_args([]).failure_policy == FAIL_CLOSED
    assert parser.parse_args([]).tickers is None
    assert (
        parser.parse_args(["--allow-partial-with-failure-report"]).failure_policy
        == ALLOW_PARTIAL
    )
