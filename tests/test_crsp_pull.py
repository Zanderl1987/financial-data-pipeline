"""Tests for strategies/crsp_pull.py, on made-up CRSP rows only. Nothing here connects to WRDS."""

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies import crsp_pull as cp            # noqa: E402
from strategies import survivorship as sv         # noqa: E402
from strategies.stage3 import DEV_END, _is_holdout_symbol   # noqa: E402

FAR = pd.Timestamp("2024-12-31")


def _permno(holdout: bool, nth: int = 0) -> int:
    hits = (p for p in range(10_000, 12_000) if _is_holdout_symbol(sv.crsp_symbol(p)) == holdout)
    return [next(hits) for _ in range(nth + 1)][-1]


def _name(permno, start, end, shrcd=11, exchcd=1, ticker="AA", shrcls=None):
    return {"permno": permno, "namedt": pd.Timestamp(start), "nameendt": pd.Timestamp(end),
            "shrcd": shrcd, "exchcd": exchcd, "ticker": ticker, "shrcls": shrcls}


def _names(rows):
    return pd.DataFrame(rows, columns=["permno", "namedt", "nameendt", "shrcd", "exchcd", "ticker", "shrcls"])


def _delist(rows):
    df = pd.DataFrame(rows, columns=["permno", "dlstdt", "dlstcd", "dlret"])
    df["dlstdt"] = pd.to_datetime(df["dlstdt"])
    return df


def _prices(permno, start, end, level=20.0, seed=1, open_from=None):
    dates = pd.bdate_range(start, end)
    rng = np.random.default_rng(seed)
    p = level * np.exp(np.cumsum(rng.normal(0.0, 0.01, len(dates))))
    opens = pd.Series(p, index=dates)
    if open_from is not None:
        opens[opens.index < pd.Timestamp(open_from)] = np.nan
    return pd.DataFrame({"permno": permno, "date": dates, "prc": p, "openprc": opens.to_numpy(),
                         "askhi": p * 1.01, "bidlo": p * 0.99, "vol": 400_000.0, "cfacpr": 1.0,
                         "cfacshr": 1.0})


class FakeSource:
    """Serves made-up tables. `honour_end=False` plays a source that ignores the date cap."""

    def __init__(self, names, delist, prices, honour_end=True):
        self._names, self._delist, self._prices = names, delist, prices
        self.honour_end = honour_end
        self.price_requests = []

    def names(self):
        return self._names.copy()

    def delist(self):
        return self._delist.copy()

    def prices(self, permnos, start, end):
        self.price_requests.append((tuple(permnos), start, end))
        p = self._prices[self._prices["permno"].isin(permnos) & (self._prices["date"] >= pd.Timestamp(start))]
        if end is not None and self.honour_end:
            p = p[p["date"] <= pd.Timestamp(end)]
        return p.copy()


# ------------------------------------------------------------------ who is in the population

def test_only_common_stock_on_the_three_exchanges_is_kept():
    a, adr, arca = _permno(False, 0), _permno(False, 1), _permno(False, 2)
    names = _names([_name(a, "2000-01-03", FAR), _name(adr, "2000-01-03", FAR, shrcd=31),
                    _name(arca, "2000-01-03", FAR, exchcd=4)])
    c = cp.build_companies(names, _delist([]))
    assert list(c["permno"]) == [a]


def test_ticker_and_exchange_come_from_the_last_eligible_name_row():
    a = _permno(False)
    names = _names([_name(a, "2000-01-03", "2009-12-31", exchcd=3, ticker="OLD"),
                    _name(a, "2010-01-04", "2020-06-01", exchcd=1, ticker="NEW"),
                    _name(a, "2020-06-02", "2020-06-30", exchcd=-2, ticker="NEW")])
    c = cp.build_companies(names, _delist([(a, "2020-06-30", 574, None)])).set_index("permno")
    assert c.loc[a, "ticker"] == "NEW"
    assert c.loc[a, "exchcd"] == 1                 # a halt is not an exchange
    assert c.loc[a, "begdt"] == pd.Timestamp("2000-01-03")
    assert c.loc[a, "dlstcd"] == 574 and not c.loc[a, "left_population"]


def test_share_class_suffix_is_used_only_when_stage3_has_that_symbol():
    a, b = _permno(False, 0), _permno(False, 1)
    names = _names([_name(a, "2000-01-03", FAR, ticker="BF", shrcls="B"),
                    _name(b, "2000-01-03", FAR, ticker="ZZ", shrcls="A")])
    c = cp.build_companies(names, _delist([]), stage3_symbols={"BF-B", "ZZ"}).set_index("permno")
    assert c.loc[a, "ticker"] == "BF-B"
    assert c.loc[b, "ticker"] == "ZZ"


def test_a_company_that_left_the_population_is_not_given_its_later_delisting():
    """Became a non-common share class, then delisted with a big loss: the loss happened
    outside the population, so the position closes at the last eligible close instead."""
    a = _permno(False)
    names = _names([_name(a, "2000-01-03", "2012-03-30"), _name(a, "2012-04-02", "2014-01-31", shrcd=73)])
    c = cp.build_companies(names, _delist([(a, "2014-01-31", 580, -0.8)])).set_index("permno")
    assert c.loc[a, "left_population"]
    assert c.loc[a, "dlstcd"] == sv.ACTIVE_CODE and pd.isna(c.loc[a, "dlret"])
    assert c.loc[a, "crsp_dlstcd"] == 580


def test_a_company_with_no_delisting_row_counts_as_alive():
    a = _permno(False)
    c = cp.build_companies(_names([_name(a, "2000-01-03", FAR)]), _delist([])).set_index("permno")
    assert c.loc[a, "dlstcd"] == sv.ACTIVE_CODE and pd.isna(c.loc[a, "dlstdt"])


def test_rows_outside_eligible_spells_are_dropped():
    a = _permno(False)
    names = _names([_name(a, "2010-01-04", "2010-06-30"), _name(a, "2010-07-01", "2010-12-31", shrcd=73),
                    _name(a, "2011-01-03", "2011-06-30")])
    spells = cp.eligible_spells(names)
    kept = cp.in_spells(_prices(a, "2010-01-04", "2011-06-30"), spells)
    assert kept["date"].between("2010-07-01", "2010-12-31").sum() == 0
    assert kept["date"].min() == pd.Timestamp("2010-01-04")
    assert kept["date"].max() == pd.Timestamp("2011-06-30")


# ------------------------------------------------------------------ the pull

OPENS_FROM = "1993-06-01"     # about 150 days after the pull starts, room for trailing volume


def _setup(tmp_path, honour_end=True):
    """A development company on NYSE, one on AMEX, and a holdout company on Nasdaq."""
    dev, amex, hold = _permno(False, 0), _permno(False, 1), _permno(True)
    names = _names([_name(dev, "1993-01-04", FAR, ticker="DEVX", exchcd=1),
                    _name(amex, "1993-01-04", FAR, ticker="AMXX", exchcd=2),
                    _name(hold, "1993-01-04", FAR, ticker="HOLDX", exchcd=3)])
    prices = pd.concat([_prices(p, "1993-01-04", "2020-12-31", seed=i, open_from=OPENS_FROM)
                        for i, p in enumerate((dev, amex, hold))])
    src = FakeSource(names, _delist([]), prices, honour_end=honour_end)
    return src, dev, hold, tmp_path / "licensed" / "crsp"


def _requests(src, permno):
    """(start, end) of every price request that included `permno`."""
    return [(s, e) for perms, s, e in src.price_requests if permno in perms]


def _asked_after_dev_end(src, permno) -> bool:
    return any(e is None or pd.Timestamp(e) > pd.Timestamp(DEV_END) for _s, e in _requests(src, permno))


def test_holdout_prices_after_dev_end_are_never_requested(tmp_path):
    src, dev, hold, root = _setup(tmp_path)
    cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None)
    assert _requests(src, hold) == [("1993-01-04", DEV_END)]
    assert _requests(src, dev) == [("1993-01-04", DEV_END), (cp.AFTER_DEV_END, None)]
    p = pd.read_parquet(root / "prices.parquet")
    assert p.loc[p["permno"] == hold, "date"].max() <= pd.Timestamp(DEV_END)
    assert p.loc[p["permno"] == dev, "date"].max() == pd.Timestamp("2020-12-31")


def test_a_source_that_ignores_the_cap_is_refused_and_nothing_is_kept(tmp_path):
    src, _dev, _hold, root = _setup(tmp_path, honour_end=False)
    with pytest.raises(RuntimeError, match="holdout prices"):
        cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None)
    assert not (root / "prices.parquet").exists()
    assert not (root / "_parts").exists()               # rows already pulled are deleted too


def test_output_loads_through_the_scoring_code(tmp_path):
    src, dev, hold, root = _setup(tmp_path)
    report = cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None)
    panel = sv.load_dev_panel({}, root=str(root))
    assert {dev, hold} <= set(panel.companies["permno"]) and len(panel.companies) == 3
    assert report["start"] == panel.report["start"] == OPENS_FROM
    frames = panel.load_batch([dev, hold])
    assert frames[hold].index.max() <= pd.Timestamp(DEV_END)
    saved = json.loads((root / "pull_report.json").read_text(encoding="utf-8"))
    assert saved["start"] == report["start"]


def test_a_pull_that_starts_too_late_for_the_first_date_is_refused(tmp_path):
    """Trailing volume needs sessions before the first scored date."""
    src, *_rest, root = _setup(tmp_path)
    with pytest.raises(RuntimeError, match="earlier --start"):
        cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None,
                min_lead_days=10_000)


def test_stage3_assignment_is_followed_and_unmatched_symbols_are_reported(tmp_path):
    src, dev, hold, root = _setup(tmp_path)
    hsym = next(f"T{i}" for i in range(1000) if _is_holdout_symbol(f"T{i}"))
    names = src._names.copy()
    names.loc[names["permno"] == dev, "ticker"] = hsym     # a dev-hash company matches a holdout symbol
    src._names = names
    live = (pd.Timestamp("1993-01-04"), pd.Timestamp("2026-10-30"))
    report = cp.pull(src, {hsym: live, "GONE": live}, str(root), start="1993-01-04", batch=1,
                     log=lambda *_: None, load_yahoo=lambda s, end: pd.Series(dtype=float))
    assert not _asked_after_dev_end(src, dev)               # treated as holdout
    assert report["stage3_unmatched"] == {"holdout": [s for s in ["GONE"] if _is_holdout_symbol(s)],
                                          "development": [s for s in ["GONE"] if not _is_holdout_symbol(s)]}


# ------------------------------------------------------------------ stricter holdout protection

def _holdout_symbol(skip=()):
    return next(f"T{i}" for i in range(1000) if _is_holdout_symbol(f"T{i}") and f"T{i}" not in skip)


def _crsp_closes(src, permno):
    p = src._prices[src._prices["permno"] == permno].set_index("date")
    return p["prc"] / p["cfacpr"]


def _strict_setup(tmp_path):
    """A development-hash company, alive, trading as NEWNAME; Stage 3 holds it as an
    unmatched holdout symbol (renamed after CRSP's last date)."""
    src, dev, _hold, root = _setup(tmp_path)
    names = src._names.copy()
    names.loc[names["permno"] == dev, "ticker"] = "NEWNAME"
    src._names = names
    return src, dev, root, _holdout_symbol()


def test_old_ticker_of_an_unmatched_holdout_symbol_moves_the_company_to_holdout(tmp_path):
    src, dev, root, hsym = _strict_setup(tmp_path)
    old = _name(dev, "1990-01-02", "1992-12-31", ticker=hsym, exchcd=1)
    src._names = pd.concat([_names([old]), src._names], ignore_index=True)
    report = cp.pull(src, {hsym: (pd.Timestamp("2019-01-02"), pd.Timestamp("2026-10-30"))}, str(root),
                     start="1993-01-04", batch=1, log=lambda *_: None,
                     load_yahoo=lambda s, end: pd.Series(dtype=float))
    assert not _asked_after_dev_end(src, dev)
    assert report["holdout_rules"]["ticker_history"] == 1


def test_price_match_before_2018_moves_a_renamed_company_to_holdout(tmp_path):
    src, dev, root, hsym = _strict_setup(tmp_path)
    yahoo = _crsp_closes(src, dev) / 3.0                    # a 3-for-1 split after CRSP's last date
    seen = []

    def load_yahoo(sym, end):
        seen.append((sym, end))
        return yahoo
    report = cp.pull(src, {hsym: (pd.Timestamp("1993-01-04"), pd.Timestamp("2026-10-30"))}, str(root),
                     start="1993-01-04", batch=1, log=lambda *_: None, load_yahoo=load_yahoo)
    assert seen == [(hsym, DEV_END)]                        # only pre-holdout Yahoo prices are read
    assert not _asked_after_dev_end(src, dev)
    assert report["holdout_rules"]["price_match"] == 1
    assert report["stage3_unresolved_holdout"] == []
    panel = sv.load_dev_panel({hsym: (pd.Timestamp("1993-01-04"), pd.Timestamp("2026-10-30"))}, root=str(root))
    row = panel.companies.set_index("permno").loc[dev]
    assert row["holdout"] and row["basis"] == "holdout_price_match"


def test_yahoo_rows_after_dev_end_are_ignored_even_if_returned(tmp_path):
    """Matching must not use reserved prices, whatever the loader hands back."""
    src, dev, root, hsym = _strict_setup(tmp_path)
    crsp = _crsp_closes(src, dev)
    # The same company, but listed on Yahoo from mid-November 2017: under 60 days before the
    # cut, so it can only "match" if prices after DEV_END are used.
    yahoo = crsp[crsp.index >= "2017-11-15"] / 3.0
    assert (yahoo.index <= DEV_END).sum() < cp.MATCH_MIN_DAYS
    report = cp.pull(src, {hsym: (pd.Timestamp("2017-11-15"), pd.Timestamp("2026-10-30"))}, str(root),
                     start="1993-01-04", batch=1, log=lambda *_: None, load_yahoo=lambda s, end: yahoo)
    assert report["holdout_rules"]["price_match"] == 0
    assert report["stage3_unresolved_holdout"] == [hsym]


def test_unrelated_prices_do_not_match():
    idx = pd.bdate_range("2016-01-04", "2017-12-29")
    rng = np.random.default_rng(5)
    a = pd.Series(20 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx)))), index=idx)
    b = pd.Series(20 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx)))), index=idx)
    closes = pd.DataFrame({1: b, 2: a * 4.0})
    assert cp.price_matches(a, closes) == [2]
    assert cp.price_matches(a.iloc[:cp.MATCH_MIN_DAYS - 1], closes) == []     # too few shared days


def test_matching_ignores_days_after_dev_end_on_both_sides():
    idx = pd.bdate_range("2017-11-15", "2019-12-31")
    rng = np.random.default_rng(6)
    a = pd.Series(20 * np.exp(np.cumsum(rng.normal(0, 0.02, len(idx)))), index=idx)
    assert (idx <= DEV_END).sum() < cp.MATCH_MIN_DAYS
    assert cp.price_matches(a, pd.DataFrame({7: a * 2.0})) == []


def test_listing_date_moves_a_post_2017_listing_to_holdout(tmp_path):
    src, dev, root, hsym = _strict_setup(tmp_path)
    near, far = _permno(False, 3), _permno(False, 4)
    src._names = pd.concat([src._names, _names([_name(near, "2019-03-05", FAR, ticker="NEAR"),
                                                _name(far, "2019-06-03", FAR, ticker="FARX")])],
                           ignore_index=True)
    src._prices = pd.concat([src._prices, _prices(near, "2019-03-05", "2020-12-31", seed=8),
                             _prices(far, "2019-06-03", "2020-12-31", seed=9)])
    report = cp.pull(src, {hsym: (pd.Timestamp("2019-03-04"), pd.Timestamp("2026-10-30"))}, str(root),
                     start="1993-01-04", batch=1, log=lambda *_: None,
                     load_yahoo=lambda s, end: pd.Series(dtype=float))
    assert not _asked_after_dev_end(src, near)
    assert _asked_after_dev_end(src, far)
    assert report["holdout_rules"]["listing_date"] == 1


def test_a_company_matched_to_a_development_symbol_is_never_moved(tmp_path):
    src, dev, root, hsym = _strict_setup(tmp_path)
    dsym = next(f"T{i}" for i in range(1000) if not _is_holdout_symbol(f"T{i}"))
    names = src._names.copy()
    names.loc[names["permno"] == dev, "ticker"] = dsym
    src._names = names
    yahoo = _crsp_closes(src, dev)                          # would price-match the holdout symbol too
    live = (pd.Timestamp("1993-01-04"), pd.Timestamp("2026-10-30"))
    report = cp.pull(src, {hsym: live, dsym: live}, str(root), start="1993-01-04", batch=1,
                     log=lambda *_: None, load_yahoo=lambda s, end: yahoo)
    assert _asked_after_dev_end(src, dev)
    assert report["holdout_rules"]["price_match"] == 0


def test_unmatched_holdout_symbols_need_a_yahoo_loader(tmp_path):
    src, _dev, root, hsym = _strict_setup(tmp_path)
    with pytest.raises(ValueError, match="load_yahoo"):
        cp.pull(src, {hsym: (pd.Timestamp("1993-01-04"), pd.Timestamp("2026-10-30"))}, str(root),
                start="1993-01-04", batch=1, log=lambda *_: None)


def test_refuses_to_write_under_curated(tmp_path):
    src, *_rest = _setup(tmp_path)
    with pytest.raises(ValueError, match="curated"):
        cp.pull(src, {}, str(tmp_path / "storage" / "curated" / "crsp"), start="1993-01-04", log=lambda *_: None)


def test_existing_output_is_not_overwritten_without_asking(tmp_path):
    src, _dev, _hold, root = _setup(tmp_path)
    cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None)
    with pytest.raises(FileExistsError):
        cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None)
    cp.pull(src, {}, str(root), start="1993-01-04", batch=1, log=lambda *_: None, overwrite=True)


def test_price_query_caps_holdout_dates_in_the_sql_itself():
    sql, params = cp.prices_sql([10001, 10002], "1990-01-01", DEV_END)
    assert "crsp.dsf" in sql and "date <= %(end)s" in sql and params["end"] == DEV_END
    sql, params = cp.prices_sql([10001], "1990-01-01", None)
    assert "%(end)s" not in sql and "end" not in params
    with pytest.raises(TypeError):
        cp.prices_sql(["10001; drop table x"], "1990-01-01", None)


# ------------------------------------------------------------------ --tables-only

class FakeDb:
    """Plays a wrds.Connection for the table check. `tables` maps name -> column names."""

    def __init__(self, tables, last_date="2024-12-31"):
        self.tables, self.last_date, self.sql = tables, last_date, []

    def list_tables(self, library):
        assert library == "crsp"
        return list(self.tables)

    def describe_table(self, library, table):
        return pd.DataFrame({"name": self.tables[table], "type": ["x"] * len(self.tables[table])})

    def raw_sql(self, sql, **_):
        self.sql.append(sql)
        return pd.DataFrame({"last": [pd.Timestamp(self.last_date)]})


def _legacy_tables(drop=()):
    t = {"dsenames": list(cp.NAMES_COLUMNS), "dsedelist": list(cp.DELIST_COLUMNS), "dsf": list(cp.PRICE_COLUMNS)}
    for table, col in drop:
        t[table] = [c for c in t[table] if c != col]
    return t


def test_table_check_reports_a_complete_legacy_layout_and_its_last_date():
    db = FakeDb({**_legacy_tables(), "dsf_v2": ["permno", "dlycaldt"]})
    out = cp.describe_tables(db)
    assert out["legacy_complete"] is True
    assert out["present"] == {"dsenames": True, "dsedelist": True, "dsf": True,
                              "dsf_v2": True, "stksecurityinfohist": False, "stkdelists": False}
    assert out["missing_columns"] == {}
    assert out["legacy_dsf_last_date"] == "2024-12-31"
    assert set(out["columns"]) == {"dsenames", "dsedelist", "dsf", "dsf_v2"}
    assert all("dsf" in s and "max(date)" in s for s in db.sql)


def test_table_check_flags_a_missing_legacy_table_or_column_without_querying_prices():
    db = FakeDb({k: v for k, v in _legacy_tables([("dsedelist", "dlret")]).items() if k != "dsf"})
    out = cp.describe_tables(db)
    assert out["legacy_complete"] is False
    assert out["present"]["dsf"] is False
    assert out["missing_columns"] == {"dsedelist": ["dlret"], "dsf": list(cp.PRICE_COLUMNS)}
    assert out["legacy_dsf_last_date"] is None and db.sql == []
