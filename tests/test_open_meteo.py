"""
Tests for open_meteo_pipeline's backfill resume logic.

The backfill checkpoints one parquet file per date chunk. A run that loses
batches to Open-Meteo's rolling 429 limit still writes a *valid* file for that
chunk -- just with fewer locations in it. The original resume check only tested
that the file existed, so those short chunks were skipped forever (found
2026-08-09: three chunks were silently missing 5-10 of the 25 locations).
"""

import pandas as pd
import pytest

import open_meteo_pipeline as omp


def _chunk_df(locations):
    """Minimal checkpoint frame: one row per location."""
    return pd.DataFrame(
        [
            {"location": loc["name"], "cluster": loc["cluster"], "date": "2005-01-01"}
            for loc in locations
        ]
    )


def test_resume_state_complete_chunk_has_no_missing(tmp_path):
    path = tmp_path / "complete.parquet"
    _chunk_df(omp.LOCATIONS).to_parquet(path, index=False)

    existing, missing = omp._resume_state(str(path))

    assert missing == []
    assert len(existing) == len(omp.LOCATIONS)


def test_resume_state_partial_chunk_reports_exactly_the_gap(tmp_path):
    path = tmp_path / "partial.parquet"
    dropped = omp.LOCATIONS[:5]
    _chunk_df(omp.LOCATIONS[5:]).to_parquet(path, index=False)

    existing, missing = omp._resume_state(str(path))

    assert [loc["name"] for loc in missing] == [loc["name"] for loc in dropped]
    assert len(existing) == len(omp.LOCATIONS) - 5


def test_resume_state_unreadable_checkpoint_refetches_whole_chunk(tmp_path):
    path = tmp_path / "corrupt.parquet"
    path.write_bytes(b"not a parquet file")

    existing, missing = omp._resume_state(str(path))

    assert existing is None
    assert len(missing) == len(omp.LOCATIONS)


def test_date_chunks_cover_the_range_contiguously():
    chunks = omp._date_chunks("1990-01-01", "2026-08-09")

    assert chunks[0][0] == "1990-01-01"
    assert chunks[-1][1] == "2026-08-09"
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert pd.Timestamp(next_start) == pd.Timestamp(prev_end) + pd.Timedelta(days=1)


@pytest.mark.parametrize("n_missing", [1, 5, 6, 25])
def test_missing_locations_batch_into_full_size_groups(n_missing):
    missing = omp.LOCATIONS[:n_missing]
    batches = [missing[i:i + omp.BATCH_SIZE] for i in range(0, len(missing), omp.BATCH_SIZE)]

    assert sum(len(b) for b in batches) == n_missing
    assert all(len(b) <= omp.BATCH_SIZE for b in batches)
