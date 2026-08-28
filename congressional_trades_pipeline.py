#!/usr/bin/env python3
"""
Congressional Trades Pipeline.

US House and Senate stock trade disclosures (STOCK Act Periodic Transaction
Reports), parsed from the OFFICIAL government sources. No API key required.

Sources:
  House:  Clerk of the House financial disclosure archive
          - annual index ZIP  -> {year}FD.xml (one <Member> per filing)
          - per-filing PDF    -> /public_disc/ptr-pdfs/{year}/{DocID}.pdf
  Senate: Senate Electronic Financial Disclosure (efdsearch.senate.gov)
          - CSRF + prohibition-agreement handshake, then a JSON search endpoint
          - per-filing HTML detail view

The community "stock watcher" S3 aggregators this pipeline originally used
started returning HTTP 403 around 2026-07-23 and are not coming back; these
are the upstream sources those aggregators were themselves scraping.

Notes on the data:
  - Disclosures report AMOUNT BRACKETS, never exact amounts. The raw bracket
    string is kept as `amount_range`, with `amount_min`/`amount_max` derived.
  - `transaction_date` and `disclosure_date` legally differ by up to 45 days.
    Any downstream signal MUST key off disclosure_date or it has look-ahead.
  - Pre-2018 House filings include paper filings scanned as images; text
    extraction yields nothing for those. They are counted and skipped, not OCR'd.

CLI:
  python congressional_trades_pipeline.py                  # current year
  python congressional_trades_pipeline.py --backfill       # full history
  python congressional_trades_pipeline.py --chamber house  # one chamber only
  python congressional_trades_pipeline.py --years 2024 2025

Outputs:
  storage/raw/congressional_trades/house/congressional_house_{mode}_{YYYY}.parquet
  storage/raw/congressional_trades/senate/congressional_senate_{mode}_{YYYY}.parquet
"""

import argparse
import datetime
import io
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd
import requests

from storage_utils import write_partitioned

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - environment guard
    fitz = None

try:
    import lxml.html as LH
except ImportError:  # pragma: no cover - environment guard
    LH = None

# -- Endpoints --------------------------------------------------------------
HOUSE_INDEX_URL = ("https://disclosures-clerk.house.gov/public_disc"
                   "/financial-pdfs/{year}FD.ZIP")
HOUSE_PTR_URL = ("https://disclosures-clerk.house.gov/public_disc"
                 "/ptr-pdfs/{year}/{doc_id}.pdf")
SENATE_BASE = "https://efdsearch.senate.gov"
SENATE_HOME = SENATE_BASE + "/search/home/"
SENATE_DATA = SENATE_BASE + "/search/report/data/"
SENATE_SEARCH = SENATE_BASE + "/search/"

# Senate report_type 11 = Periodic Transaction Report
SENATE_PTR_TYPE = "[11]"

# First year each source has electronic filings worth fetching. The House
# archive exposes a 2014 index but it contains no periodic transaction
# reports (verified 2026-08-28); the first real PTR year is 2015.
HOUSE_FIRST_YEAR = 2015
SENATE_FIRST_YEAR = 2012

BASE_DIR = os.path.join("storage", "raw", "congressional_trades")
HOUSE_DIR = os.path.join(BASE_DIR, "house")
SENATE_DIR = os.path.join(BASE_DIR, "senate")
# Resume checkpoints must live OUTSIDE storage/raw/: query.py's CATALOG glob
# is congressional_trades/**/*.parquet, so a checkpoint parquet under the raw
# tree gets unioned into the table -- and, having no year=/month= directory,
# raises "Hive partition mismatch" and takes the whole view down.
CACHE_DIR = os.path.join("storage", "cache", "congressional_trades")

USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "financial-data-pipeline (contact via github.com/Zanderl1987)")

MAX_RETRIES = 3
REQUEST_PAUSE = 0.35      # seconds between requests to a .gov host
CHECKPOINT_EVERY = 200    # filings between resumable checkpoint writes

_last_request = [0.0]


def _throttle():
    """Keep a minimum interval between outbound requests."""
    elapsed = time.time() - _last_request[0]
    if elapsed < REQUEST_PAUSE:
        time.sleep(REQUEST_PAUSE - elapsed)
    _last_request[0] = time.time()


def _get(session, url, **kwargs):
    """GET with throttle + retry/backoff. Returns a Response or None."""
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = session.get(url, timeout=120, **kwargs)
        except requests.RequestException as exc:
            print(f"    request error (attempt {attempt}): {exc}")
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code == 404:
                return None          # genuinely absent, do not retry
            print(f"    HTTP {resp.status_code} (attempt {attempt}) {url}")
        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)
    return None


# -- Shared parsing helpers -------------------------------------------------

_AMOUNT_RE = re.compile(r"\$\s?([\d,]+)")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# House PTR PDFs render field labels in a small-caps font that maps lowercase
# letters into the IPA Extensions block. Any word containing one of those is a
# metadata label ("Filing Status:", "Subholding Of:", "Description:"), not data.
_SMALLCAPS_RE = re.compile(r"[ɐ-ʯ]")
# Canonical House asset format: "Name (TICKER) [ST]".
_HOUSE_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z]{2})\]")
# Some filers write the exchange-qualified form instead: "NYSEARCA: DIA [OT]".
_HOUSE_EXCHANGE_RE = re.compile(
    r"[A-Z]{2,8}:\s*([A-Z][A-Z0-9.\-]{0,6})\s*\[([A-Z]{2})\]")


def _to_date(raw):
    """Normalize a date-ish string to YYYY-MM-DD, or None."""
    if not raw:
        return None
    ts = pd.to_datetime(str(raw).strip(), errors="coerce")
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


def parse_amount_range(text):
    """
    Split a disclosure amount bracket into (raw, min, max).

    "$1,001 - $15,000"  -> ("$1,001 - $15,000", 1001.0, 15000.0)
    "Over $50,000,000"  -> ("Over $50,000,000", 50000000.0, None)
    "$1,000,001 +"      -> ("$1,000,001 +", 1000001.0, None)
    """
    if not text:
        return None, None, None
    raw = " ".join(str(text).split())
    nums = [float(m.replace(",", "")) for m in _AMOUNT_RE.findall(raw)]
    if not nums:
        return raw, None, None
    if len(nums) == 1:
        # An open-ended top bracket has a floor but no ceiling.
        return raw, nums[0], None
    return raw, min(nums), max(nums)


def _finalize(rows, chamber, fetched_at):
    """Build the output DataFrame with the repo's standard column contract."""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["chamber"] = chamber
    df["fetched_at"] = fetched_at
    df = df[df["member_name"].notna() & (df["member_name"] != "")]
    df = df.reset_index(drop=True)
    # Position within the filing. A single disclosure can legitimately repeat
    # the same ticker/date/amount, so nothing else in the row is unique --
    # (chamber, doc_id, row_index) is the natural key curated.py dedups on,
    # and it is stable across re-fetches because filings are immutable.
    df["row_index"] = df.groupby("doc_id").cumcount()
    # `date` mirrors transaction_date so query.py's generic date filters work.
    df["date"] = df["transaction_date"]
    return df


# -- House ------------------------------------------------------------------

def fetch_house_index(session, year):
    """
    Return the year's Periodic Transaction Report filings from the Clerk's
    annual index ZIP, as a list of dicts.
    """
    resp = _get(session, HOUSE_INDEX_URL.format(year=year))
    if resp is None:
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
    except zipfile.BadZipFile:
        print(f"    {year}: index is not a valid ZIP")
        return []
    names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not names:
        print(f"    {year}: no XML in index ZIP")
        return []
    root = ET.fromstring(zf.read(names[0]).decode("utf-8-sig"))

    filings = []
    for member in root:
        if (member.findtext("FilingType") or "").strip() != "P":
            continue
        doc_id = (member.findtext("DocID") or "").strip()
        if not doc_id:
            continue
        first = (member.findtext("First") or "").strip()
        last = (member.findtext("Last") or "").strip()
        state_dst = (member.findtext("StateDst") or "").strip()
        filings.append({
            "doc_id": doc_id,
            "member_name": " ".join(p for p in (first, last) if p),
            "state": state_dst[:2] or None,
            "district": state_dst[2:] or None,
            "disclosure_date": _to_date(member.findtext("FilingDate")),
        })
    return filings


def _house_columns(page):
    """
    Derive x-boundaries for the transaction table's columns from the header
    row on this page. Returns None if the page has no transaction table.

    The PDFs are generated with stable per-column x positions, so mapping each
    word to a column by its x0 is far more reliable than regexing linear text
    (asset names, amounts and dates all wrap unpredictably).
    """
    header = {}
    seen_notification = False
    for x0, _y0, _x1, _y1, word, *_ in sorted(
            page.get_text("words"), key=lambda w: (round(w[1], 1), w[0])):
        key = word.strip().rstrip(":")
        if key in ("Owner", "Asset", "Transaction", "Amount"):
            header.setdefault(key, x0)
        elif key == "Notification":
            header.setdefault("Notification", x0)
            seen_notification = True
        elif key == "Date" and not seen_notification:
            header.setdefault("Date", x0)
    needed = ("Owner", "Asset", "Transaction", "Date", "Notification", "Amount")
    if not all(k in header for k in needed):
        return None
    return {
        "owner": header["Owner"],
        "asset": header["Asset"],
        "type": header["Transaction"],
        "date": header["Date"],
        "notif": header["Notification"],
        "amount": header["Amount"],
    }


def _assign_column(x, cols):
    """Map a word's x0 to one of the table's columns."""
    name = "id"
    for key in ("owner", "asset", "type", "date", "notif", "amount"):
        if x >= cols[key] - 4:
            name = key
        else:
            break
    return name


def _page_lines(page, cols):
    """Group a page's words into (y, {column: text}) lines, top to bottom."""
    lines = {}
    for x0, y0, _x1, _y1, word, *_ in page.get_text("words"):
        lines.setdefault(round(y0, 1), []).append((x0, word))
    out = []
    for y in sorted(lines):
        cells = {}
        for x0, word in sorted(lines[y]):
            col = _assign_column(x0, cols)
            cells[col] = (cells.get(col, "") + " " + word).strip()
        out.append((y, cells))
    return out


def parse_house_ptr(pdf_bytes, filing):
    """
    Extract transaction rows from one House PTR PDF.

    Returns [] for image-only (paper) filings -- the caller counts those.
    """
    rows = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            cols = _house_columns(page)
            if cols is None:
                continue
            current = None
            for _y, cells in _page_lines(page, cols):
                joined = " ".join(cells.values())
                if _SMALLCAPS_RE.search(joined):
                    # A field label line (Filing Status / Subholding Of /
                    # Description) ends the current row's wrapped text and
                    # carries no transaction data of its own.
                    current = None
                    continue
                date_cell = cells.get("date", "").strip()
                amount_cell = cells.get("amount") or ""
                if _DATE_RE.match(date_cell) and "$" in amount_cell:
                    raw, lo, hi = parse_amount_range(cells.get("amount"))
                    current = {
                        "doc_id": filing["doc_id"],
                        "member_name": filing["member_name"],
                        "state": filing["state"],
                        "district": filing["district"],
                        "owner": cells.get("owner") or None,
                        "asset_description": cells.get("asset", "").strip(),
                        "transaction_type": cells.get("type") or None,
                        "transaction_date": _to_date(date_cell),
                        "disclosure_date": (_to_date(cells.get("notif"))
                                            or filing["disclosure_date"]),
                        "amount_range": raw,
                        "amount_min": lo,
                        "amount_max": hi,
                    }
                    rows.append(current)
                elif current is not None:
                    # Continuation line: asset names and amounts both wrap.
                    if cells.get("asset"):
                        current["asset_description"] = (
                            current["asset_description"] + " "
                            + cells["asset"]).strip()
                    if cells.get("amount"):
                        merged = ((current["amount_range"] or "")
                                  + " " + cells["amount"])
                        raw, lo, hi = parse_amount_range(merged)
                        current["amount_range"] = raw
                        current["amount_min"] = lo
                        current["amount_max"] = hi
    finally:
        doc.close()

    for row in rows:
        desc = row["asset_description"] or ""
        match = (_HOUSE_TICKER_RE.search(desc)
                 or _HOUSE_EXCHANGE_RE.search(desc))
        row["ticker"] = match.group(1) if match else None
        row["asset_type"] = match.group(2) if match else None
        row["comment"] = None
    return rows


def collect_house(session, years, mode, fetched_at):
    """Fetch, parse and write House PTRs for each requested year."""
    if fitz is None:
        print("  [house] PyMuPDF (fitz) not installed -- skipping House.")
        return

    os.makedirs(HOUSE_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    for year in years:
        print(f"\n[house {year}] fetching filing index...")
        filings = fetch_house_index(session, year)
        if not filings:
            print(f"  no periodic transaction reports for {year}")
            continue
        print(f"  {len(filings)} periodic transaction reports")

        checkpoint = os.path.join(CACHE_DIR, f"house_{year}_partial.parquet")
        rows, done = _load_checkpoint(checkpoint)
        if done:
            print(f"  resuming: {len(done)} filings already parsed")

        image_only = 0
        missing = 0
        for i, filing in enumerate(filings, 1):
            if filing["doc_id"] in done:
                continue
            resp = _get(session, HOUSE_PTR_URL.format(
                year=year, doc_id=filing["doc_id"]))
            if resp is None:
                missing += 1
            else:
                try:
                    parsed = parse_house_ptr(resp.content, filing)
                except Exception as exc:
                    print(f"    parse failed for {filing['doc_id']}: {exc}")
                    parsed = []
                if parsed:
                    rows.extend(parsed)
                else:
                    image_only += 1
            done.add(filing["doc_id"])

            if i % CHECKPOINT_EVERY == 0:
                _save_checkpoint(checkpoint, rows, done)
                print(f"    {i}/{len(filings)} filings, {len(rows)} rows")

        df = _finalize(rows, "house", fetched_at)
        if df.empty:
            print(f"  {year}: no rows parsed")
            continue
        path = write_partitioned(
            df, HOUSE_DIR, f"congressional_house_{mode}_{year}.parquet")
        print(f"  -> {path}  ({len(df):,} rows, "
              f"{df['member_name'].nunique()} members, "
              f"{image_only} unparseable/image filings, {missing} missing)")
        _clear_checkpoint(checkpoint)


# -- Senate -----------------------------------------------------------------

def senate_session():
    """
    Open an eFD session: fetch the CSRF token and accept the prohibition
    agreement. Every search call bounces without this handshake.
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    resp = _get(session, SENATE_HOME)
    if resp is None:
        return None
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', resp.text)
    if not match:
        print("  [senate] no CSRF token on the search home page")
        return None
    _throttle()
    session.post(SENATE_HOME,
                 data={"csrfmiddlewaretoken": match.group(1),
                       "prohibition_agreement": "1"},
                 headers={"Referer": SENATE_HOME}, timeout=60)
    return session


def fetch_senate_index(session, start_date, end_date):
    """Page through the eFD search endpoint for PTRs in a date window."""
    filings = []
    start = 0
    page_size = 100
    while True:
        _throttle()
        try:
            resp = session.post(
                SENATE_DATA,
                data={
                    "start": str(start),
                    "length": str(page_size),
                    "report_types": SENATE_PTR_TYPE,
                    "filer_types": "[]",
                    "submitted_start_date": start_date,
                    "submitted_end_date": end_date,
                    "search_text": "",
                    "csrfmiddlewaretoken": session.cookies.get("csrftoken"),
                },
                headers={"Referer": SENATE_SEARCH}, timeout=120)
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"    senate search failed at offset {start}: {exc}")
            break

        batch = payload.get("data", [])
        if not batch:
            break
        for row in batch:
            href = re.search(r'href="([^"]+)"', row[3])
            if not href:
                continue
            filings.append({
                "url": SENATE_BASE + href.group(1),
                "member_name": " ".join(
                    p for p in (row[0].strip(), row[1].strip()) if p),
                "disclosure_date": _to_date(row[4]),
            })
        start += page_size
        if start >= payload.get("recordsTotal", 0):
            break
    return filings


def parse_senate_ptr(html, filing):
    """Extract transaction rows from one eFD PTR detail page."""
    tree = LH.fromstring(html)
    rows = []
    for table in tree.xpath("//table"):
        headers = [h.text_content().strip().lower()
                   for h in table.xpath(".//thead//th")]
        if "transaction date" not in headers or "amount" not in headers:
            continue
        index = {name: i for i, name in enumerate(headers)}
        for tr in table.xpath(".//tbody/tr"):
            cells = tr.xpath("./td")
            if len(cells) < len(headers):
                continue

            def cell(name, _cells=cells, _index=index):
                pos = _index.get(name)
                if pos is None:
                    return None
                text = " ".join(_cells[pos].text_content().split())
                return None if text in ("", "--") else text

            # The asset cell nests Company/Description sub-divs; keep the
            # asset name itself and split the rest off.
            asset = cell("asset name") or ""
            asset = re.split(r"\s*(?:Company:|Description:)\s*", asset)[0].strip()

            raw, lo, hi = parse_amount_range(cell("amount"))
            rows.append({
                "doc_id": filing["url"].rstrip("/").split("/")[-1],
                "member_name": filing["member_name"],
                "state": None,
                "district": None,
                "owner": cell("owner"),
                "ticker": cell("ticker"),
                "asset_description": asset or None,
                "asset_type": cell("asset type"),
                "transaction_type": cell("type"),
                "transaction_date": _to_date(cell("transaction date")),
                "disclosure_date": filing["disclosure_date"],
                "amount_range": raw,
                "amount_min": lo,
                "amount_max": hi,
                "comment": cell("comment"),
            })
    return rows


def collect_senate(session, years, mode, fetched_at):
    """Fetch, parse and write Senate PTRs for each requested year."""
    if LH is None:
        print("  [senate] lxml not installed -- skipping Senate.")
        return

    os.makedirs(SENATE_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    for year in years:
        print(f"\n[senate {year}] searching disclosures...")
        filings = fetch_senate_index(
            session,
            f"01/01/{year} 00:00:00",
            f"12/31/{year} 23:59:59")
        if not filings:
            print(f"  no periodic transaction reports for {year}")
            continue
        print(f"  {len(filings)} periodic transaction reports")

        checkpoint = os.path.join(CACHE_DIR, f"senate_{year}_partial.parquet")
        rows, done = _load_checkpoint(checkpoint)
        if done:
            print(f"  resuming: {len(done)} filings already parsed")

        paper = 0
        for i, filing in enumerate(filings, 1):
            key = filing["url"]
            if key in done:
                continue
            if "/view/ptr/" not in key:
                # Paper filings are served as scanned images, not HTML tables.
                paper += 1
                done.add(key)
                continue
            resp = _get(session, key)
            if resp is not None:
                try:
                    rows.extend(parse_senate_ptr(resp.text, filing))
                except Exception as exc:
                    print(f"    parse failed for {key}: {exc}")
            done.add(key)

            if i % CHECKPOINT_EVERY == 0:
                _save_checkpoint(checkpoint, rows, done)
                print(f"    {i}/{len(filings)} filings, {len(rows)} rows")

        df = _finalize(rows, "senate", fetched_at)
        if df.empty:
            print(f"  {year}: no rows parsed")
            continue
        path = write_partitioned(
            df, SENATE_DIR, f"congressional_senate_{mode}_{year}.parquet")
        print(f"  -> {path}  ({len(df):,} rows, "
              f"{df['member_name'].nunique()} members, {paper} paper filings)")
        _clear_checkpoint(checkpoint)


# -- Resumable checkpoints --------------------------------------------------
# A full backfill is thousands of per-filing requests. Checkpointing every
# CHECKPOINT_EVERY filings means an interrupted run resumes instead of
# restarting -- the same reasoning as open_meteo_pipeline.py's chunk skipping.

def _done_path(path):
    """Sidecar listing every filing already attempted for this checkpoint."""
    return path + ".done"


def _load_checkpoint(path):
    """
    Return (parsed rows, set of already-attempted filing ids).

    The attempted-set lives in a plain-text sidecar rather than in the parquet
    itself: a filing that parsed to zero rows (an image-only paper scan, ~12%
    of House PTRs) contributes no row to infer it from, so deriving the set
    from the data would re-fetch every one of those on resume.
    """
    done = set()
    done_file = _done_path(path)
    if os.path.exists(done_file):
        with open(done_file, encoding="utf-8") as fh:
            done = {line.strip() for line in fh if line.strip()}
    if not os.path.exists(path):
        return [], done
    try:
        df = pd.read_parquet(path)
    except Exception:
        return [], done
    if not done and "doc_id" in df.columns:
        done = set(df["doc_id"].dropna().astype(str).unique())
    return df.to_dict("records"), done


def _save_checkpoint(path, rows, done):
    try:
        with open(_done_path(path), "w", encoding="utf-8") as fh:
            for item in sorted(done):
                print(item, file=fh)
        if rows:
            pd.DataFrame(rows).to_parquet(
                path, index=False, compression="snappy")
    except Exception as exc:
        print(f"    checkpoint write failed: {exc}")


def _clear_checkpoint(path):
    for target in (path, _done_path(path)):
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass


# -- CLI --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Congressional stock trade disclosures "
                    "(keyless, official sources)")
    parser.add_argument("--backfill", action="store_true",
                        help="Full history (House 2014+, Senate 2012+)")
    parser.add_argument("--years", nargs="+", type=int,
                        help="Explicit years to fetch (overrides --backfill)")
    parser.add_argument("--chamber", choices=["house", "senate", "both"],
                        default="both", help="Limit to one chamber")
    args = parser.parse_args()

    now = datetime.datetime.utcnow()
    fetched_at = now.isoformat()
    mode = "backfill" if (args.backfill or args.years) else "incremental"

    if args.years:
        house_years = senate_years = sorted(args.years)
    elif args.backfill:
        house_years = list(range(HOUSE_FIRST_YEAR, now.year + 1))
        senate_years = list(range(SENATE_FIRST_YEAR, now.year + 1))
    else:
        house_years = senate_years = [now.year]

    print("Congressional Trades Pipeline  "
          "(keyless, official disclosure sources)")
    print(f"  mode={mode}  house={house_years[0]}-{house_years[-1]}  "
          f"senate={senate_years[0]}-{senate_years[-1]}")

    if args.chamber in ("house", "both"):
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        collect_house(session, house_years, mode, fetched_at)

    if args.chamber in ("senate", "both"):
        session = senate_session()
        if session is None:
            print("\n[senate] could not open an eFD session -- skipping.")
        else:
            collect_senate(session, senate_years, mode, fetched_at)

    print("\n--- CONGRESSIONAL TRADES PIPELINE COMPLETE ---")


if __name__ == "__main__":
    main()
