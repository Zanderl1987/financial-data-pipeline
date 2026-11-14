"""openfigi must save as it goes. Without an API key it resolves 10 tickers per
request; the 2026-11-14 run had ~4,170 new tickers, hit run_all's 600s timeout
and saved nothing, so every later run would start over and time out again."""

import pandas as pd
import pytest

import openfigi_pipeline as ofp


def test_each_chunk_is_written_before_the_next_is_resolved(monkeypatch):
    tickers = [f"T{i}" for i in range(7)]
    written = []

    def fake_resolve(batch):
        if "T6" in batch:
            raise TimeoutError("killed by run_all")
        return pd.DataFrame({"ticker": batch})

    monkeypatch.setattr(ofp, "WRITE_CHUNK", 3)
    monkeypatch.setattr(ofp, "collect_tickers_from_iceberg", lambda: set(tickers))
    monkeypatch.setattr(ofp, "get_existing_mapped_tickers", lambda: set())
    monkeypatch.setattr(ofp, "resolve_tickers", fake_resolve)
    monkeypatch.setattr(ofp, "write_to_iceberg", lambda df: written.append(list(df.ticker)))

    with pytest.raises(TimeoutError):
        ofp.main()

    assert written == [["T0", "T1", "T2"], ["T3", "T4", "T5"]]
