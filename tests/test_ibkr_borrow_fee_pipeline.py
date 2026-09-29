"""
Tests for ibkr_borrow_fee_pipeline and the backtest borrow-fee loaders.

The fixture mirrors the live usa.txt layout verified 2026-09-29: a #BOF line,
the #SYM header (with FIGI), rows with a trailing '|', and a #EOF row count.
FEERATE is annual percent, so the loaders must return 100x the stored value.
"""

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import event_backtest as eb
import ibkr_borrow_fee_pipeline as ibkr

FETCHED_AT = "2026-09-29T15:46:04+00:00"

USA_TXT = (
    "#BOF|2026.09.29|11:46:04\n"
    "#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|\n"
    "AAPL|USD|APPLE INC|265598|US0378331005|3.6300|0.2500|>10000000|BBG000B9XRY4|\n"
    "BYND|USD|BEYOND MEAT INC|367326567|US08862E1091|-25.4961|29.3761|150000|BBG003CVMLQ7|\n"
    "SPY|USD|SS SPDR S&amp;P 500 ETF TRUST-US|756733|US78462F1030|3.6300|0.2500|5100000|BBG000BDTBL9|\n"
    "#EOF|3\n"
)


class TestParse:
    def test_parses_live_layout(self):
        df = ibkr._parse_usa_txt(USA_TXT, FETCHED_AT)
        assert df["symbol"].tolist() == ["AAPL", "BYND", "SPY"]  # no BOF/EOF rows
        row = df.set_index("symbol").loc["BYND"]
        assert row["fee_rate"] == 29.3761
        assert row["rebate_rate"] == -25.4961
        assert row["conid"] == "367326567"
        assert row["figi"] == "BBG003CVMLQ7"
        assert (df["date"] == "2026-09-29").all()

    def test_capped_availability_kept_as_number(self):
        df = ibkr._parse_usa_txt(USA_TXT, FETCHED_AT).set_index("symbol")
        assert df.loc["AAPL", "available"] == 10_000_000
        assert bool(df.loc["AAPL", "available_capped"]) is True
        assert df.loc["BYND", "available"] == 150_000
        assert bool(df.loc["BYND", "available_capped"]) is False

    def test_html_entities_unescaped(self):
        df = ibkr._parse_usa_txt(USA_TXT, FETCHED_AT).set_index("symbol")
        assert df.loc["SPY", "name"] == "SS SPDR S&P 500 ETF TRUST-US"

    def test_columns_mapped_by_name_not_position(self):
        reordered = USA_TXT.replace(
            "#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|",
            "#SYM|CUR|NAME|CON|ISIN|FEERATE|REBATERATE|AVAILABLE|FIGI|",
        )
        df = ibkr._parse_usa_txt(reordered, FETCHED_AT).set_index("symbol")
        assert df.loc["BYND", "fee_rate"] == -25.4961  # follows the header

    def test_truncated_file_raises(self):
        with pytest.raises(ValueError, match="EOF"):
            ibkr._parse_usa_txt(USA_TXT.replace("#EOF|3", "#EOF|4"), FETCHED_AT)

    def test_missing_header_raises(self):
        body = "\n".join(l for l in USA_TXT.splitlines() if not l.startswith("#SYM"))
        with pytest.raises(ValueError, match="header"):
            ibkr._parse_usa_txt(body, FETCHED_AT)


class _FakeFTP:
    """Stands in for ftplib.FTP; hosts in `dead` time out on connect."""
    dead: set = set()
    connected: list = []

    def __init__(self, host, timeout=None):
        _FakeFTP.connected.append(host)
        if host in _FakeFTP.dead:
            raise TimeoutError("timed out")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        return "230 Login successful."

    def retrbinary(self, cmd, callback):
        callback(USA_TXT.encode())


class TestFetch:
    @pytest.fixture(autouse=True)
    def _fake_ftp(self, monkeypatch):
        _FakeFTP.dead, _FakeFTP.connected = set(), []
        monkeypatch.setattr(ibkr.ftplib, "FTP", _FakeFTP)
        monkeypatch.setattr(ibkr.time, "sleep", lambda s: None)

    def test_falls_back_to_second_host(self):
        _FakeFTP.dead = {ibkr.FTP_HOSTS[0]}
        assert ibkr._fetch_usa_txt().startswith("#BOF")
        assert _FakeFTP.connected == list(ibkr.FTP_HOSTS)

    def test_all_hosts_down_raises_naming_them(self):
        _FakeFTP.dead = set(ibkr.FTP_HOSTS)
        with pytest.raises(RuntimeError) as exc:
            ibkr._fetch_usa_txt()
        for host in ibkr.FTP_HOSTS:
            assert host in str(exc.value)

    def test_main_writes_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert ibkr.main() == 0
        files = list((tmp_path / ibkr.OUTPUT_DIR).rglob("*.parquet"))
        assert len(files) == 1
        assert len(pd.read_parquet(files[0])) == 3


class TestLoadersReturnBps:
    """fee_rate is stored as IBKR's annual percent; backtests consume bps."""

    @pytest.fixture(autouse=True)
    def _fake_table(self, monkeypatch):
        table = pd.DataFrame({
            "symbol": ["AAPL", "BYND"], "date": ["2026-09-29", "2026-09-29"],
            "fee_rate": [0.25, 29.3761], "fetched_at": [FETCHED_AT] * 2,
        })

        def fake_load(name, symbol=None, start=None, end=None, columns=None, limit=None):
            df = table if symbol is None else table[table["symbol"].isin(
                [symbol] if isinstance(symbol, str) else symbol)]
            df = df.head(limit) if limit else df
            return df[columns].copy() if columns else df.copy()

        monkeypatch.setattr(eb.q, "load", fake_load)

    def test_single_symbol(self):
        assert eb.load_borrow_fee("AAPL").iloc[0] == pytest.approx(25.0)

    def test_matrix(self):
        m = eb.load_borrow_fee_matrix(["AAPL", "BYND"])
        assert m["AAPL"].iloc[0] == pytest.approx(25.0)
        assert m["BYND"].iloc[0] == pytest.approx(2937.61)
