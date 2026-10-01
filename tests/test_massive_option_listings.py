"""Tests for massive_option_listings_pipeline (no network)."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import massive_option_listings_pipeline as mol


def _c(ticker, strike, exp, typ="call", shares=100, extra=None):
    d = {"ticker": ticker, "strike_price": strike, "expiration_date": exp,
         "contract_type": typ, "shares_per_contract": shares}
    if extra:
        d["additional_underlyings"] = extra
    return d


def test_standard_frame_drops_adjusted_and_odd_size_contracts():
    rows = [
        _c("O:FDX261016C00195000", 195.0, "2026-10-16"),
        _c("O:FDX1261016C00195000", 195.0, "2026-10-16",
           extra=[{"type": "equity", "underlying": "FDXF", "amount": 50}]),
        _c("O:FDX261016P00195000", 195.0, "2026-10-16", typ="put", shares=150),
    ]
    df = mol.standard_frame(rows, "FDX")
    assert df["contract_ticker"].tolist() == ["O:FDX261016C00195000"]
    assert df["put_call"].tolist() == ["CALL"]


def test_live_on_excludes_expired():
    df = mol.standard_frame([_c("O:A260918C00100000", 100.0, "2026-09-18"),
                             _c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    assert mol.live_on(df, "2026-10-01")["expiration_date"].tolist() == ["2026-10-16"]


def test_first_day_is_initial_then_added_and_removed():
    d1 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.standard_frame([_c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    first = mol.diff_contracts(None, d1, "A", "2026-10-01")
    assert first["change"].tolist() == ["initial"]
    nxt = mol.diff_contracts(d1, d2, "A", "2026-10-02").sort_values("change")
    assert nxt[["contract_ticker", "change"]].values.tolist() == [
        ["O:A261016C00110000", "added"], ["O:A261016C00100000", "removed"]]
    assert list(nxt.columns) == mol.CHANGE_COLS


def test_expiry_shows_as_removed_not_as_a_listing():
    d1 = mol.standard_frame([_c("O:A261002C00100000", 100.0, "2026-10-02"),
                             _c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.live_on(d1, "2026-10-05")  # next trading day after the 10-02 expiry
    out = mol.diff_contracts(d1, d2, "A", "2026-10-05")
    assert out[["contract_ticker", "change"]].values.tolist() == [
        ["O:A261002C00100000", "removed"]]


def test_summary_and_missing_status():
    d = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16"),
                            _c("O:A261120P00080000", 80.0, "2026-11-20", typ="put")], "A")
    s = mol.summarize(d, "A", "2026-10-01", "ok")
    assert (s["n_contracts"], s["n_expirations"], s["min_strike"], s["max_strike"]) == (2, 2, 80.0, 100.0)
    empty = mol.summarize(d.iloc[0:0], "A", "2026-10-02", "missing")
    assert empty["status"] == "missing" and empty["n_contracts"] == 0
    assert empty["min_strike"] is None


def test_replay_reconstructs_live_set():
    d1 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16")], "A")
    d2 = mol.standard_frame([_c("O:A261016C00100000", 100.0, "2026-10-16"),
                             _c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    d3 = mol.standard_frame([_c("O:A261016C00110000", 110.0, "2026-10-16")], "A")
    ch = pd.concat([mol.diff_contracts(None, d1, "A", "2026-10-01"),
                    mol.diff_contracts(d1, d2, "A", "2026-10-02"),
                    mol.diff_contracts(d2, d3, "A", "2026-10-05")])
    assert mol.replay_live_set(ch, "2026-10-02") == {"O:A261016C00100000", "O:A261016C00110000"}
    assert mol.replay_live_set(ch, "2026-10-05") == {"O:A261016C00110000"}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, str(body)[:100]

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.responses.pop(0)


def _client(responses):
    s = FakeSession(responses)
    return mol.MassiveClient("KEY", session=s, interval=0, sleep=lambda x: None), s


def test_every_page_is_authenticated_and_all_pages_are_read(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    p1 = {"results": [_c("O:A261016C00100000", 100.0, "2026-10-16")],
          "next_url": "https://api.massive.com/v3/reference/options/contracts?cursor=abc"}
    p2 = {"results": [_c("O:A261016C00110000", 110.0, "2026-10-16")]}
    client, s = _client([_Resp(200, p1), _Resp(200, p2)])
    df = mol.fetch_contracts(client, "A", "2026-10-01")
    assert len(df) == 2
    assert all(params.get("apiKey") == "KEY" for _, params in s.calls)
    assert s.calls[1][0].endswith("cursor=abc")


def test_429_backs_off_then_succeeds(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    client, s = _client([_Resp(429, {}), _Resp(200, {"results": []})])
    assert mol.fetch_contracts(client, "A", "2026-10-01").empty
    assert len(s.calls) == 2


def test_persistent_failure_raises(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", (None,))
    client, _ = _client([_Resp(500, {})] * mol.MAX_RETRIES)
    import pytest
    with pytest.raises(RuntimeError):
        mol.fetch_contracts(client, "A", "2026-10-01")


def test_both_expired_queries_are_unioned_and_filtered_to_live(monkeypatch):
    monkeypatch.setattr(mol, "EXPIRED_QUERIES", ("false", "true"))
    live = {"results": [_c("O:A261016C00100000", 100.0, "2026-10-16")]}
    gone = {"results": [_c("O:A261002C00100000", 100.0, "2026-10-02"),
                        _c("O:A260918C00100000", 100.0, "2026-09-18")]}
    client, s = _client([_Resp(200, live), _Resp(200, gone)])
    df = mol.fetch_contracts(client, "A", "2026-10-01")
    assert sorted(df["expiration_date"]) == ["2026-10-02", "2026-10-16"]
    assert [p.get("expired") for _, p in s.calls] == ["false", "true"]


def _fake_fetch(lists):
    """lists: {date: [contract tickers]} -> fetch(client, symbol, as_of)."""
    def fetch(client, symbol, as_of):
        rows = [_c(t, float(t[-8:]) / 1000, "2027-01-15") for t in lists[as_of]]
        return mol.standard_frame(rows, symbol)
    return fetch


DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08"]
LISTS = {
    "2026-09-01": ["O:A270115C00100000"],
    "2026-09-02": ["O:A270115C00100000", "O:A270115C00110000"],
    "2026-09-03": [],                                   # API hiccup
    "2026-09-04": ["O:A270115C00110000"],
    "2026-09-08": ["O:A270115C00110000", "O:A270115C00120000"],
}


def _read(out_root, kind):
    import glob
    files = glob.glob(f"{out_root}/raw/massive/{kind}/**/*.parquet", recursive=True)
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def test_missing_day_is_skipped_not_diffed(tmp_path):
    mol.advance_symbol(None, "A", DAYS, str(tmp_path), flush_every=2, fetch=_fake_fetch(LISTS))
    ch = _read(tmp_path, "option_listing_changes")
    assert "2026-09-03" not in set(ch["date"])
    summ = _read(tmp_path, "option_chain_summary").set_index("date")
    assert summ.loc["2026-09-03", "status"] == "missing"
    removed_0904 = ch[(ch["date"] == "2026-09-04") & (ch["change"] == "removed")]
    assert removed_0904["contract_ticker"].tolist() == ["O:A270115C00100000"]


def test_resume_after_crash_matches_uninterrupted_run(tmp_path):
    full, crash = tmp_path / "full", tmp_path / "crash"
    mol.advance_symbol(None, "A", DAYS, str(full), flush_every=2, fetch=_fake_fetch(LISTS))

    calls = {"n": 0}
    def dying(client, symbol, as_of):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("boom")
        return _fake_fetch(LISTS)(client, symbol, as_of)
    import pytest
    with pytest.raises(RuntimeError):
        mol.advance_symbol(None, "A", DAYS, str(crash), flush_every=2, fetch=dying)
    mol.advance_symbol(None, "A", DAYS, str(crash), flush_every=2, fetch=_fake_fetch(LISTS))

    key = ["date", "contract_ticker", "change"]
    a = _read(full, "option_listing_changes").drop_duplicates(key).sort_values(key)[key]
    b = _read(crash, "option_listing_changes").drop_duplicates(key).sort_values(key)[key]
    assert a.reset_index(drop=True).equals(b.reset_index(drop=True))
    assert mol.advance_symbol(None, "A", DAYS, str(crash), flush_every=2,
                              fetch=_fake_fetch(LISTS)) == 0


# ---- Final-review fixes (2026-10-01) -------------------------------------

def test_empty_days_before_any_data_are_missing_and_first_data_is_initial(tmp_path):
    # C1: dates beyond the provider's history window may come back empty. They
    # must not become an empty baseline that turns the whole chain into "added".
    lists = {"2026-09-01": [], "2026-09-02": [],
             "2026-09-03": ["O:A270115C00100000", "O:A270115C00110000"]}
    mol.advance_symbol(None, "A", list(lists), str(tmp_path), fetch=_fake_fetch(lists))
    ch = _read(tmp_path, "option_listing_changes")
    assert set(ch["change"]) == {"initial"}
    summ = _read(tmp_path, "option_chain_summary").set_index("date")
    assert summ.loc["2026-09-01", "status"] == "missing"
    assert summ.loc["2026-09-03", "status"] == "ok"


def test_one_failing_symbol_does_not_stop_the_rest(tmp_path):
    # C1/I4: a symbol that errors is logged and skipped; the run reports it.
    def advance(client, sym, days, out_root):
        if sym == "BAD":
            raise RuntimeError("Massive request failed 5x")
        return len(days)
    failed = mol.run_symbols(None, ["A", "BAD", "C"], ["2026-09-01"], str(tmp_path),
                             advance=advance)
    assert failed == ["BAD"]


def test_checkpoint_is_consistent_if_killed_between_writes(tmp_path, monkeypatch):
    # I1: the date and the contract list must commit together. Simulate a kill
    # right after the new contract list is written but before the pointer moves.
    state = str(tmp_path / "state")
    d1 = mol.standard_frame([_c("O:A270115C00100000", 100.0, "2027-01-15")], "A")
    d2 = mol.standard_frame([_c("O:A270115C00110000", 110.0, "2027-01-15")], "A")
    mol._save_state(state, "A", "2026-09-01", d1)
    real_replace = os.replace
    def kill_on_pointer(src, dst):
        if dst.endswith("A.json"):
            raise KeyboardInterrupt("killed")
        return real_replace(src, dst)
    monkeypatch.setattr(mol.os, "replace", kill_on_pointer)
    import pytest
    with pytest.raises(KeyboardInterrupt):
        mol._save_state(state, "A", "2026-09-02", d2)
    monkeypatch.setattr(mol.os, "replace", real_replace)
    last, live = mol._load_state(state, "A")
    assert last == "2026-09-01"
    assert live["contract_ticker"].tolist() == ["O:A270115C00100000"]


def test_sudden_collapse_is_suspect_not_diffed(tmp_path):
    # I2: a truncated response (well under half of yesterday) must not be read
    # as mass delisting followed by mass relisting.
    many = [f"O:A270115C00{100 + i:03d}000" for i in range(10)]
    lists = {"2026-09-01": many, "2026-09-02": many[:3], "2026-09-03": many}
    mol.advance_symbol(None, "A", list(lists), str(tmp_path), fetch=_fake_fetch(lists))
    ch = _read(tmp_path, "option_listing_changes")
    assert set(ch["change"]) == {"initial"}
    summ = _read(tmp_path, "option_chain_summary").set_index("date")
    assert summ.loc["2026-09-02", "status"] == "suspect"


def test_summary_records_churn_for_split_days(tmp_path):
    # I3: a stock split re-tickers the whole chain; the summary must expose it.
    before = ["O:A270115C00100000", "O:A270115C00110000"]
    after = ["O:A270115C00010000", "O:A270115C00011000"]
    lists = {"2026-09-01": before, "2026-09-02": after}
    mol.advance_symbol(None, "A", list(lists), str(tmp_path), fetch=_fake_fetch(lists))
    summ = _read(tmp_path, "option_chain_summary").set_index("date")
    assert (summ.loc["2026-09-02", "n_added"], summ.loc["2026-09-02", "n_removed"]) == (2, 2)
    assert summ.loc["2026-09-02", "replaced_frac"] == 1.0


def test_second_concurrent_run_is_refused(tmp_path):
    # I5: two processes on the same checkpoints double the request rate and
    # overwrite each other's state.
    state = str(tmp_path)
    assert mol.acquire_lock(state) is True
    assert mol.acquire_lock(state) is False          # same live pid holds it
    mol.release_lock(state)
    assert mol.acquire_lock(state) is True


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    with open(os.path.join(tmp_path, ".lock"), "w", encoding="utf-8") as f:
        f.write("999999999")
    assert mol.acquire_lock(str(tmp_path)) is True


def test_run_window_is_fixed_by_manifest_across_restarts(tmp_path):
    # M1: a restart days later must keep the original window, not slide it.
    state = str(tmp_path)
    m1 = mol.load_or_create_manifest(state, "2024-10-06", "2026-10-01", ["A", "B"])
    m2 = mol.load_or_create_manifest(state, "2024-10-20", "2026-10-15", ["A", "B"])
    assert (m2["start"], m2["end"]) == ("2024-10-06", "2026-10-01")
    assert m2["launched_at"] == m1["launched_at"]
