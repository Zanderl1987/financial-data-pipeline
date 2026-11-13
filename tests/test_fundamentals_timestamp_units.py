"""Fundamentals raw files must share one timestamp unit (found 2026-10-26).

Files before 2026-10-04 were timestamp[us], later ones timestamp[ns]; a
start_date of 0202-02-01 fits in us but not ns, so DuckDB could not read the
mixed glob and curated.py stopped rebuilding fundamentals_quarterly.
"""

import datetime
import os
import sys

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fundamentals_pipeline as fp
import repair_fundamentals_timestamp_units as repair


def _write(path, unit, dates):
    table = pa.table({
        "metric": ["revenue"] * len(dates),
        "start_date": pa.array(dates, type=pa.timestamp(unit)),
        "period_end": pa.array([datetime.datetime(2022, 4, 30)] * len(dates), type=pa.timestamp(unit)),
    })
    pq.write_table(table, path)


def test_to_dates_pins_microseconds():
    out = fp._to_dates(pd.Series(["2022-04-30", None]))
    assert str(out.dtype) == "datetime64[us]"
    assert out.iloc[0] == pd.Timestamp("2022-04-30") and pd.isna(out.iloc[1])


def test_flush_batch_writes_microsecond_timestamps(tmp_path):
    rows = [{"metric": "revenue", "value": "1", "period_end": "2022-04-30",
             "start_date": "2022-02-01"}]
    fp._flush_batch(rows, str(tmp_path), "part.parquet")
    schema = pq.read_schema(tmp_path / "part.parquet")
    assert schema.field("period_end").type == pa.timestamp("us")
    assert schema.field("start_date").type == pa.timestamp("us")


def test_repair_makes_the_mixed_glob_readable(tmp_path):
    old = datetime.datetime(202, 2, 1)  # the MIND Technology typo, only fits in us
    # the ns file sorts first, so DuckDB takes ns as the column type -- as in the store
    _write(tmp_path / "a_new.parquet", "ns", [datetime.datetime(2023, 2, 1)])
    _write(tmp_path / "b_old.parquet", "us", [old, datetime.datetime(2022, 2, 1)])
    glob_path = str(tmp_path / "*.parquet").replace("\\", "/")
    read = (f"SELECT count(*), min(start_date) "
            f"FROM read_parquet('{glob_path}', union_by_name=True)")
    with pytest.raises(duckdb.Error):
        duckdb.sql(read).fetchall()

    assert repair.main(["--root", str(tmp_path), "--apply"]) == 0
    assert duckdb.sql(read).fetchone() == (3, old)
    assert pq.read_schema(tmp_path / "a_new.parquet").field("start_date").type == pa.timestamp("us")
    assert pq.read_table(tmp_path / "a_new.parquet").column("start_date")[0].as_py() == \
        datetime.datetime(2023, 2, 1)


def test_hive_partition_directories_do_not_leak_into_the_schema(tmp_path):
    folder = tmp_path / "year=2026" / "month=10"
    folder.mkdir(parents=True)
    _write(folder / "f.parquet", "ns", [datetime.datetime(2023, 2, 1)])
    assert repair.repair_file(str(folder / "f.parquet"), apply=True)[0]
    names = pq.ParquetFile(folder / "f.parquet").schema_arrow.names
    assert names == ["metric", "start_date", "period_end"]


def test_dry_run_changes_nothing(tmp_path):
    _write(tmp_path / "f.parquet", "ns", [datetime.datetime(2023, 2, 1)])
    before = (tmp_path / "f.parquet").read_bytes()
    needs, _ = repair.repair_file(str(tmp_path / "f.parquet"), apply=False)
    assert needs and (tmp_path / "f.parquet").read_bytes() == before


def test_us_files_are_left_alone(tmp_path):
    _write(tmp_path / "f.parquet", "us", [datetime.datetime(2023, 2, 1)])
    before = (tmp_path / "f.parquet").read_bytes()
    assert repair.repair_file(str(tmp_path / "f.parquet"), apply=True) == (False, "already us")
    assert (tmp_path / "f.parquet").read_bytes() == before


def test_lossy_cast_refuses(tmp_path):
    table = pa.table({"t": pa.array([1_000_000_000_000_000_001], type=pa.timestamp("ns"))})
    pq.write_table(table, tmp_path / "f.parquet")
    with pytest.raises(pa.ArrowInvalid):
        repair.repair_file(str(tmp_path / "f.parquet"), apply=True)
    assert pq.read_schema(tmp_path / "f.parquet").field("t").type == pa.timestamp("ns")
