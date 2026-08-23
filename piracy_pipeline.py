#!/usr/bin/env python3
"""
Piracy Incident Pipeline — maritime security events relevant to shipping costs.

Two keyless sources. Motivation: Somali piracy resurged from late 2023 (MV Ruen,
MV Abdullah hijackings) and incident risk feeds routing (Suez vs Cape of Good
Hope), insurance premia, and freight rates — an event-study input against the
shipping.* cost tables.

  1. ICC IMB live piracy map archive — the WP Google Maps REST endpoint that the
     public map at https://icc-ccs.org/map/ loads in-browser. One GET returns the
     full marker set: ~2,700 incidents, 2012-present (year embedded in the IMB
     incident-number title "NNN-YY"), global coverage with lat/lon. No exact
     dates at this endpoint — day-level dating for Somali attacks comes from
     source 2. robots.txt only disallows /wp-admin/; fetched once per run with a
     declared UA (public endpoint, no circumvention involved).
     https://icc-ccs.org/wp-json/wpgmza/v1/markers

  2. Wikipedia "List of ships attacked by Somali pirates" via MediaWiki API —
     structured {{Hijacked ship}} templates with capture date, vessel, flag,
     status, ransom. Somali-specific backfill 2005-2024; not kept current by
     editors, so source 1 covers the ongoing resurgence.
     https://en.wikipedia.org/wiki/List_of_ships_attacked_by_Somali_pirates

Outputs:
  storage/raw/piracy/imb/year=YYYY/month=MM/piracy_incidents_*.parquet
      -> CATALOG table piracy_incidents   (IMB markers, global)
  storage/raw/piracy/wiki/year=YYYY/month=MM/somali_hijackings_*.parquet
      -> CATALOG table somali_hijackings  (Wikipedia log, dated)

Usage:
  python piracy_pipeline.py             # incremental (skips a source if its last snapshot is <12h old)
  python piracy_pipeline.py --backfill  # force full refetch of both sources

CLI output is ASCII-only (cp1252 terminal). Columns deliberately avoid bare
"year"/"month" names (Hive partition shadowing — see CLAUDE.md).
"""

import argparse
import datetime
import os
import re
import time
from pathlib import Path

import pandas as pd
import requests

from storage_utils import write_partitioned, find_parquet_files

BASE_DIR = Path(__file__).parent
IMB_OUT_DIR = BASE_DIR / "storage" / "raw" / "piracy" / "imb"
WIKI_OUT_DIR = BASE_DIR / "storage" / "raw" / "piracy" / "wiki"

IMB_MARKERS_URL = "https://icc-ccs.org/wp-json/wpgmza/v1/markers"
WIKI_API_URL = (
    "https://en.wikipedia.org/w/api.php"
    "?action=parse&page=List_of_ships_attacked_by_Somali_pirates"
    "&prop=wikitext&format=json&formatversion=2&redirects=1"
)

USER_AGENT = "financial-data-pipeline/1.0 (research; contact: github.com/Zanderl1987)"
REQUEST_INTERVAL = 1.0
MAX_RETRIES = 3
BACKOFF_SECONDS = 30
STALE_HOURS = 12  # incremental mode skips a source whose last snapshot is younger

SOURCE_IMB = "ICC IMB live piracy map"
SOURCE_WIKI = "Wikipedia list of ships attacked by Somali pirates"

# Coarse boxes approximating IMB's regional reporting. First match wins.
# gulf_of_aden_somalia spans the Gulf of Aden, Somali Basin, southern Red Sea
# approaches and the western Indian Ocean hunting grounds used since 2008.
REGION_BOXES = [
    ("gulf_of_aden_somalia", -15.0, 25.0, 38.0, 80.0),
    ("southeast_asia", -12.0, 25.0, 95.0, 141.0),
    ("gulf_of_guinea", -10.0, 10.0, -25.0, 15.0),
    ("americas_caribbean", -55.0, 35.0, -135.0, -50.0),
]


def _get_with_retry(url: str, timeout: int = 60) -> requests.Response | None:
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  429 from server. Backing off {wait}s (attempt {attempt}/{MAX_RETRIES}).")
                time.sleep(wait)
            else:
                print(f"  HTTP {r.status_code} for {url.split('?')[0]}")
                return None
        except requests.RequestException as exc:
            print(f"  Request error (attempt {attempt}): {exc}")
            time.sleep(BACKOFF_SECONDS * attempt)
    return None


def classify_region(lat: float, lng: float) -> str:
    for name, lat_min, lat_max, lng_min, lng_max in REGION_BOXES:
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            return name
    return "other"


# ---------------------------------------------------------------------------
# Source 1: ICC IMB live piracy map markers
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"^(\d{1,3})-(\d{2})$")


def fetch_imb_markers(now: datetime.datetime) -> pd.DataFrame | None:
    print("[piracy_incidents] Fetching ICC IMB live-map markers...")
    r = _get_with_retry(IMB_MARKERS_URL)
    if r is None:
        return None
    try:
        rows = r.json()
    except ValueError:
        print("  Response was not JSON.")
        return None
    if not isinstance(rows, list) or not rows:
        print("  Unexpected payload shape (expected non-empty marker list).")
        return None

    recs = []
    for row in rows:
        try:
            lat = float(row.get("lat"))
            lng = float(row.get("lng"))
        except (TypeError, ValueError):
            continue
        title = str(row.get("title") or "").strip()
        m = _TITLE_RE.match(title)
        seq_no = f"{int(m.group(1)):03d}" if m else None
        year = 2000 + int(m.group(2)) if m else None
        recs.append(
            {
                "incident_id": f"{seq_no}-{m.group(2)}" if m else f"marker_{row.get('id')}",
                "incident_year": year,
                "incident_seq": seq_no,
                "lat": lat,
                "lng": lng,
                "region": classify_region(lat, lng),
                "imb_map_id": str(row.get("map_id") or ""),
                "source": SOURCE_IMB,
                "fetched_at": now.isoformat(),
            }
        )

    df = pd.DataFrame(recs)
    # Titles fail to parse for ~2% of pins (stray markers, office pins). Fill
    # their year from each map's modal parsed year where possible.
    modal = (
        df[df["incident_year"].notna()]
        .groupby("imb_map_id")["incident_year"]
        .agg(lambda s: s.mode().iat[0] if len(s.mode()) else None)
    )
    need_fill = df["incident_year"].isna() & df["imb_map_id"].isin(modal.index)
    df.loc[need_fill, "incident_year"] = df.loc[need_fill, "imb_map_id"].map(modal)

    before = len(df)
    df = df.drop_duplicates(subset=["incident_id"])
    dropped = before - len(df)
    print(f"  Parsed {before:,} markers ({dropped} cross-map duplicates dropped), "
          f"{df['incident_year'].min():.0f}-{df['incident_year'].max():.0f}, "
          f"{(df['region'] == 'gulf_of_aden_somalia').sum()} in Gulf of Aden/Somalia region.")
    return df


# ---------------------------------------------------------------------------
# Source 2: Wikipedia Somali hijacking log
# ---------------------------------------------------------------------------

_FIELD_LINE_RE = re.compile(r"^\s*\|\s*(\w+)\s*=(.*)$")
_SECTION_RE = re.compile(r"^===\s*(\d{4})\s*===$")
_RANSOM_RE = re.compile(r"(?:US)?\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|m|bn)?", re.IGNORECASE)


def _strip_wikitext(text: str) -> str:
    if not text or text.strip().lower() == "unknown":
        return "" if not text else text.strip()
    t = text
    t = re.sub(r"<!--.*?-->", "", t, flags=re.DOTALL)
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.DOTALL)
    t = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\{\{convert\|([\d.]+)\|([a-z]+)[^}]*\}\}", r"\1 \2", t, flags=re.IGNORECASE)
    # {{Ship|FV|Name}} / {{ship||OS 35}} -> "FV Name" / "OS 35" (join non-empty
    # args); must run before the generic template stripper eats these whole.
    t = re.sub(
        r"\{\{[Ss]hip\|([^{}]*)\}\}",
        lambda m: " ".join(a.strip() for a in m.group(1).split("|") if a.strip()),
        t,
    )
    t = re.sub(r"\{\{Sclass\|([^}|]+)\|([^}|]+)[^}]*\}\}", r"\1-class \2", t, flags=re.IGNORECASE)
    t = re.sub(r"\{\{(?:MV|USS|HMS|RFA|MT|MS)\|([^}|]+)(?:\|[^}]*)?\}\}", r"\1", t)
    for _ in range(4):
        prev = t
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
        if t == prev:
            break
    t = re.sub(r"\[\[([^\]|]*)\|([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    t = t.replace("'''", "").replace("''", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _clean_vessel_name(fields: dict) -> str:
    # Some templates carry two vessels (name/name2, e.g. the Mavuno pair).
    parts = []
    for key in ("name", "name2"):
        cleaned = _strip_wikitext(fields.get(key, ""))
        if cleaned and cleaned.lower() not in ("unknown", "n/a"):
            parts.append(cleaned)
    # Some templates genuinely have no name ("|name=Unknown") — keep the row,
    # mirroring the source's own placeholder, rather than emitting blanks.
    return " / ".join(parts) if parts else "Unknown"


def _parse_ransom_usd(text: str) -> float | None:
    if not text:
        return None
    m = _RANSOM_RE.search(text.replace("&nbsp;", " "))
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit.startswith("b") or unit == "bn":
        value *= 1e9
    elif unit.startswith("m"):
        value *= 1e6
    return value


def parse_wiki_wikitext(wikitext: str, now: datetime.datetime) -> pd.DataFrame:
    lines = wikitext.split("\n")
    records = []
    section_year = None
    in_block = False
    fields: dict[str, str] = {}
    current_field = None
    row_num = 0

    def close_block():
        nonlocal row_num
        if not fields:
            return
        name = _clean_vessel_name(fields)
        cdate_raw = _strip_wikitext(fields.get("cdate", ""))
        rdate_raw = _strip_wikitext(fields.get("rdate", ""))
        ransom_note = _strip_wikitext(fields.get("ransom", ""))
        crew_raw = _strip_wikitext(fields.get("crew", ""))
        crew_m = re.search(r"\d+", crew_raw or "")
        cdate = pd.to_datetime(cdate_raw, errors="coerce")
        rdate = pd.to_datetime(rdate_raw, errors="coerce")
        records.append(
            {
                "vessel_name": name,
                "vessel_type": _strip_wikitext(fields.get("class", "")),
                "flag_state": _strip_wikitext(fields.get("flag", "")),
                "owner_country": _strip_wikitext(fields.get("owner", "")),
                "crew_count": int(crew_m.group(0)) if crew_m else None,
                "cargo": _strip_wikitext(fields.get("cargo", "")),
                "hijack_status": _strip_wikitext(fields.get("status", "")),
                "ransom_note": ransom_note,
                "ransom_usd": _parse_ransom_usd(ransom_note),
                "incident_date": cdate.date() if pd.notna(cdate) else None,
                "capture_date_raw": cdate_raw,
                "release_date": rdate.date() if pd.notna(rdate) else None,
                "section_year": section_year,
                "row_num": row_num,
                "description": _strip_wikitext(fields.get("info", ""))[:2000],
                "source": SOURCE_WIKI,
                "fetched_at": now.isoformat(),
            }
        )
        row_num += 1

    for line in lines:
        sec = _SECTION_RE.match(line.strip())
        if sec:
            close_block()
            in_block = False
            fields, current_field = {}, None
            section_year = int(sec.group(1))
            continue
        if line.strip().startswith("{{Hijacked ship"):
            close_block()
            in_block = True
            fields, current_field = {}, None
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped == "}}":
            close_block()
            in_block = False
            fields, current_field = {}, None
            continue
        fm = _FIELD_LINE_RE.match(line)
        if fm and fm.group(1) != "image":
            current_field = fm.group(1).lower()
            fields[current_field] = fm.group(2).strip()
        elif current_field:
            fields[current_field] += " " + stripped

    close_block()

    return pd.DataFrame(records)


def fetch_wiki_hijackings(now: datetime.datetime) -> pd.DataFrame | None:
    print("[somali_hijackings] Fetching Wikipedia Somali-piracy attack list...")
    r = _get_with_retry(WIKI_API_URL, timeout=60)
    if r is None:
        return None
    try:
        wikitext = r.json()["parse"]["wikitext"]
    except (ValueError, KeyError):
        print("  Unexpected API response shape.")
        return None

    df = parse_wiki_wikitext(wikitext, now)
    if df.empty:
        print("  No hijacking templates parsed - page structure may have changed.")
        return None
    df["section_year"] = df["section_year"].astype("Int64")
    print(f"  Parsed {len(df)} incidents across sections "
          f"{df['section_year'].dropna().min()}-{df['section_year'].dropna().max()}, "
          f"{df['incident_date'].notna().sum()} with parseable dates.")
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _snapshot_is_fresh(out_dir: Path) -> bool:
    files = find_parquet_files(str(out_dir))
    if not files:
        return False
    age_hours = (time.time() - os.path.getmtime(files[-1])) / 3600
    return age_hours < STALE_HOURS


def main():
    parser = argparse.ArgumentParser(description="Piracy incident pipeline (ICC IMB + Wikipedia)")
    parser.add_argument("--backfill", action="store_true",
                        help="Force refetch of both sources even if recent snapshots exist.")
    args = parser.parse_args()
    mode = "backfill" if args.backfill else "incremental"
    now = datetime.datetime.utcnow()

    imb_df = wiki_df = None

    if not args.backfill and _snapshot_is_fresh(IMB_OUT_DIR):
        print("[piracy_incidents] Snapshot <12h old, skipping fetch.")
    else:
        imb_df = fetch_imb_markers(now)
        time.sleep(REQUEST_INTERVAL)

    if not args.backfill and _snapshot_is_fresh(WIKI_OUT_DIR):
        print("[somali_hijackings] Snapshot <12h old, skipping fetch.")
    else:
        wiki_df = fetch_wiki_hijackings(now)

    stamp = now.strftime("%Y%m%d")

    if imb_df is not None and not imb_df.empty:
        path = write_partitioned(
            imb_df, str(IMB_OUT_DIR), f"piracy_incidents_{mode}_{stamp}.parquet"
        )
        print(f"  Wrote {len(imb_df):,} rows -> {path}")

    if wiki_df is not None and not wiki_df.empty:
        path = write_partitioned(
            wiki_df, str(WIKI_OUT_DIR), f"somali_hijackings_{mode}_{stamp}.parquet"
        )
        print(f"  Wrote {len(wiki_df):,} rows -> {path}")

    print("\n--- PIRACY PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
