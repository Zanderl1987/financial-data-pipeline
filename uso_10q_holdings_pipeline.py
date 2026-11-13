#!/usr/bin/env python3
"""
USO Holdings from SEC 10-Q / 10-K Pipeline:
  Quarterly holdings for the United States Oil Fund (USO), read from the
  Schedule of Investments in its 10-Q and 10-K filings on SEC EDGAR. Writes to
  Iceberg table constituents.fund_holdings with source='edgar_10q:USO'.

  Why this source: USO is a commodity pool, not a 1940-Act fund, so it files no
  N-PORT. USCF's own holdings API (uscf_holdings_pipeline.py) needs a Bearer
  token scraped from the site's api_key.php; that access pattern was flagged as
  unauthorized in review (2026-11-13) and is not scheduled. EDGAR is public,
  keyless, and only asks for a declared User-Agent (EDGAR_USER_AGENT) and
  <=10 req/s; this pipeline makes 2 requests per filing.

  Cadence: quarterly, filed ~40 days after quarter end (10-K ~60). Rows are
  point-in-time as of `reporting_period_end`; `snapshot_date` is set to the
  same date so daily re-runs produce identical keys instead of a fresh copy of
  the same quarter every day.

  Parsing: the Schedule of Investments is an HTML table section headed
  "Schedule of Investments ... At <Month D, YYYY>". The filing also prints the
  prior year-end schedule for comparison; only the section dated at the
  filing's reportDate is read. Row kinds:
    - Futures:  "NYMEX WTI Crude Oil Futures CL November 2026 contracts, ..."
                notional, contracts, unrealized gain/loss, % capital
    - T-bills:  "Matures on 10/08/2026, yield 3.85 %"  principal, value, %
    - MMFs:     "<fund name>, 3.82 % #"                 shares, value, %
    - Swaps:    "<index name> (a)" ... notional, fair value, upfront, unrealized
  weight_pct for futures and swaps is exposure (notional / fair value over
  partners' capital, from the XBRL us-gaap:PartnersCapital fact), not the
  schedule's "% of partners' capital" column, which for derivatives is the
  unrealized gain as a share of capital. For T-bills and MMFs the reported
  percentage is used as-is.

  CLI:
    python uso_10q_holdings_pipeline.py              # latest filing
    python uso_10q_holdings_pipeline.py --filings 8  # latest 8 (backfill)

  Catalog:  storage/iceberg/constituents_catalog.db
  Warehouse: storage/iceberg/constituents/
"""

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

USO_CIK = 1327068
FUND_TICKER = "USO"
FUND_NAME = "United States Oil Fund, LP"
SOURCE = f"edgar_10q:{FUND_TICKER}"

STORAGE_ROOT = Path(__file__).parent / "storage"
ICEBERG_WAREHOUSE = STORAGE_ROOT / "iceberg"
CATALOG_DB = ICEBERG_WAREHOUSE / "constituents_catalog.db"

EDGAR_SLEEP = 0.2

_NUM = re.compile(r"^\(?-?\$?\s*[\d,]+(?:\.\d+)?\s*\)?$")
_DATE_HEAD = re.compile(r"\bAt\s+([A-Z][a-z]+ \d{1,2}, \d{4})")
_TBILL = re.compile(r"Matures on (\d{1,2}/\d{1,2}/\d{4}),\s*yield\s*([\d.]+)\s*%", re.I)
_MMF_RATE = re.compile(r",\s*([\d.]+)\s*%\s*#?\s*$")
_SECTION_END = re.compile(r"Statements? of (Operations|Financial Condition|Changes)", re.I)


def _headers() -> dict:
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        raise SystemExit("EDGAR_USER_AGENT is not set")
    return {"User-Agent": ua}


def _to_num(s: str) -> float | None:
    s = s.strip()
    if not s or s in ("—", "-", "–"):
        return None
    neg = s.startswith("(") or s.endswith(")") or s.startswith("-")
    digits = re.sub(r"[^\d.]", "", s)
    if not digits:
        return None
    v = float(digits)
    return -v if neg else v


def list_filings(n: int) -> list[dict]:
    """Newest-first 10-Q/10-K filings for USO: accession, primary doc, period."""
    r = requests.get(f"https://data.sec.gov/submissions/CIK{USO_CIK:010d}.json",
                     headers=_headers(), timeout=60)
    r.raise_for_status()
    rec = r.json()["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form in ("10-Q", "10-K"):
            out.append({
                "form": form,
                "accession": rec["accessionNumber"][i],
                "filing_date": date.fromisoformat(rec["filingDate"][i]),
                "period": date.fromisoformat(rec["reportDate"][i]),
                "doc": rec["primaryDocument"][i],
            })
            if len(out) >= n:
                break
    return out


def fetch_filing_html(f: dict) -> str:
    acc = f["accession"].replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{USO_CIK}/{acc}/{f['doc']}"
    time.sleep(EDGAR_SLEEP)
    r = requests.get(url, headers=_headers(), timeout=120)
    r.raise_for_status()
    return r.text


def _lines(html: str) -> list[tuple[str, list[str]]]:
    """Flatten the document into ('row', cells) for table rows and
    ('text', [text]) for paragraphs outside tables, in document order."""
    from lxml import html as lh

    root = lh.fromstring(html.encode("utf-8"))
    out = []
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        tag = tag.split("}")[-1].lower()
        if tag == "tr":
            cells = [re.sub(r"\s+", " ", c.text_content()).replace("​", "").strip()
                     for c in el if isinstance(c.tag, str) and c.tag.lower() in ("td", "th")]
            cells = [c for c in cells if c]
            if cells:
                out.append(("row", cells))
        elif tag in ("p", "div") and not any(a.tag == "tr" for a in el.iterancestors()):
            if any(isinstance(c.tag, str) and c.tag.lower() in ("p", "div", "table") for c in el):
                continue
            t = re.sub(r"\s+", " ", el.text_content()).replace("​", "").strip()
            if t:
                out.append(("text", [t]))
    return out


def _schedule_rows(lines, period: date) -> list[list[str]]:
    """Table rows inside the Schedule of Investments dated at `period`. Text
    lines are kept too (as one-cell rows): some section headers, e.g. "Open OTC
    Commodity Swap Contracts", sit outside the tables."""
    want = period.strftime("%B ") + str(period.day) + period.strftime(", %Y")
    inside, rows = False, []
    for i, (kind, cells) in enumerate(lines):
        joined = " ".join(cells)
        if "Schedule of Investments" in joined:
            ctx = " ".join(" ".join(c) for _, c in lines[i:i + 3])
            m = _DATE_HEAD.search(ctx)
            if m:
                inside = (m.group(1) == want)
                continue
        if inside and kind == "text" and _SECTION_END.search(joined):
            inside = False
        if inside:
            rows.append(cells)
    return rows


def _merge_numeric_cells(cells: list[str]) -> list[str]:
    """Join split tokens like '$', '(', '993', ')', '%' into whole numbers."""
    out: list[str] = []
    buf = ""
    for c in cells:
        if c in ("$", "("):
            buf += c
            continue
        if c in (")", "%", ")%") and out:
            out[-1] += c
            continue
        out.append(buf + c)
        buf = ""
    return out


def partners_capital(html: str, period: date) -> float | None:
    """us-gaap:PartnersCapital at `period` from the inline XBRL, no dimensions."""
    instants = {}
    for m in re.finditer(r"<xbrli:context\b[^>]*id=\"([^\"]+)\"(.*?)</xbrli:context>", html, re.S):
        cid, body = m.group(1), m.group(2)
        if "<xbrli:segment" in body:
            continue
        im = re.search(r"<xbrli:instant>(\d{4}-\d{2}-\d{2})</xbrli:instant>", body)
        if im:
            instants[cid] = im.group(1)
    for m in re.finditer(r"<ix:nonFraction\b([^>]*)>([^<]*)</ix:nonFraction>", html):
        attrs, text = m.group(1), m.group(2)
        if 'name="us-gaap:PartnersCapital"' not in attrs:
            continue
        cm = re.search(r'contextRef="([^"]+)"', attrs)
        if not cm or instants.get(cm.group(1)) != period.isoformat():
            continue
        v = _to_num(text)
        if v is None:
            continue
        sm = re.search(r'scale="(-?\d+)"', attrs)
        v *= 10 ** int(sm.group(1)) if sm else 1
        if 'sign="-"' in attrs:
            v = -v
        return v
    return None


def parse_schedule(html: str, period: date, filing_date: date) -> pd.DataFrame:
    lines = _lines(html)
    rows = _schedule_rows(lines, period)
    if not rows:
        raise RuntimeError(f"no Schedule of Investments section dated {period}")
    capital = partners_capital(html, period)
    if not capital:
        raise RuntimeError(f"no PartnersCapital XBRL fact at {period}")

    recs = []
    section = None
    for raw in rows:
        cells = _merge_numeric_cells(raw)
        label = cells[0]
        nums = [_to_num(c) for c in cells[1:] if _NUM.match(c.replace("%", "").strip())]
        low = label.lower()
        if "futures contracts" in low and not nums:
            section = "FUT"
            continue
        if "treasury" in low and not nums:
            section = "GOVT"
            continue
        if "money market" in low and not nums:
            section = "MMF"
            continue
        if "swap contracts" in low and not nums:
            section = "SWAP"
            continue
        if low.startswith("total") or not nums:
            continue

        rec = {"holding_name": label.rstrip(" *#^˄").strip(), "asset_category": section,
               "maturity_date": None, "coupon_pct": None, "ytm_pct": None, "par_value": None,
               "shares_held": None, "market_value_usd": None, "weight_pct": None}
        if "Futures" in label and len(nums) >= 2:
            # notional, contracts, unrealized gain/loss, % capital (of the gain)
            rec.update(asset_category="FUT", market_value_usd=nums[0], shares_held=nums[1],
                       weight_pct=nums[0] / capital * 100)
            m = re.search(r"Futures\s+([A-Z]{1,3})\s+([A-Z][a-z]+ \d{4})", label)
            if m:
                rec["holding_ticker"] = f"{m.group(1)} {m.group(2)}"
        elif (m := _TBILL.search(label)) and len(nums) >= 3:
            mat = datetime.strptime(m.group(1), "%m/%d/%Y").date()
            rec.update(asset_category="GOVT", holding_name=f"U.S. Treasury Bill {mat:%m/%d/%Y}",
                       maturity_date=mat, coupon_pct=0.0, ytm_pct=float(m.group(2)),
                       par_value=nums[0], market_value_usd=nums[1], weight_pct=nums[2])
        elif section == "MMF" and len(nums) >= 3:
            mm = _MMF_RATE.search(label)
            rec.update(asset_category="MMF", ytm_pct=float(mm.group(1)) if mm else None,
                       holding_name=_MMF_RATE.sub("", label).strip(),
                       shares_held=nums[0], market_value_usd=nums[1], weight_pct=nums[2])
        elif section == "SWAP" and len(nums) >= 2:
            # Row: index, fund-pays %, counterparty, frequency, expiry, notional,
            # fair value, upfront, unrealized. Take the numbers after the expiry.
            dates = [c for c in cells if re.fullmatch(r"\d{2}/\d{2}/\d{4}", c)]
            after = cells[cells.index(dates[0]) + 1:] if dates else cells[1:]
            money = [_to_num(c) for c in after if _NUM.match(c)]
            if len(money) < 2:
                continue
            rec.update(asset_category="SWAP", par_value=money[0], market_value_usd=money[1],
                       weight_pct=money[1] / capital * 100,
                       holding_name=re.sub(r"\s*\([a-z]\)\s*$", "", label).strip(),
                       maturity_date=(datetime.strptime(dates[0], "%m/%d/%Y").date()
                                      if dates else None))
            cp = [c for c in cells if c.endswith(("Ltd.", "Generale", "Bank", "AG", "plc", "N.A."))]
            if cp:
                rec["issuer_name"] = cp[0]
        else:
            continue
        recs.append(rec)

    if not recs:
        raise RuntimeError(f"schedule dated {period} parsed to zero holdings")
    df = pd.DataFrame(recs)
    for col in ("holding_ticker", "issuer_name"):
        if col not in df.columns:
            df[col] = None
    df["snapshot_date"] = period
    df["reporting_period_end"] = period
    df["filing_date"] = filing_date
    df["fund_ticker"] = FUND_TICKER
    df["fund_name"] = FUND_NAME
    df["fund_cik"] = USO_CIK
    df["source"] = SOURCE
    log.info("[USO %s] %d holdings, partners' capital $%.0f, weights sum %.1f%%",
             period, len(df), capital, df["weight_pct"].sum())
    return df


def write_to_iceberg(df: pd.DataFrame) -> int:
    """Replace this source's USO rows (other USO sources are left alone)."""
    import pyarrow as pa
    from pyiceberg.catalog import load_catalog
    from pyiceberg.expressions import And, EqualTo, In

    catalog = load_catalog(
        "constituents", type="sql",
        uri=f"sqlite:///{CATALOG_DB.as_posix()}",
        warehouse=f"file://{ICEBERG_WAREHOUSE.as_posix()}",
    )
    table = catalog.load_table("constituents.fund_holdings")

    df = df.copy()
    for col in ["snapshot_date", "filing_date", "reporting_period_end", "maturity_date"]:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")

    arrow_schema = table.schema().as_arrow()
    for field in arrow_schema:
        if field.name not in df.columns:
            df[field.name] = None
    df = df[[f.name for f in arrow_schema]]
    arrow = pa.Table.from_pandas(df, schema=arrow_schema, preserve_index=False)

    # Replace only the quarters fetched in this run, so the default 1-filing
    # daily run doesn't wipe the backfilled history.
    periods = sorted({d.isoformat() for d in df["reporting_period_end"]})
    table.overwrite(arrow, overwrite_filter=And(
        EqualTo("fund_ticker", FUND_TICKER),
        EqualTo("source", SOURCE),
        In("reporting_period_end", periods),
    ))
    log.info("[Iceberg] USO: %d rows written (source=%s)", len(arrow), SOURCE)

    try:
        from iceberg_utils import expire_old_snapshots
        table.refresh()
        expire_old_snapshots(table, retain_days=30, log=log)
    except Exception as e:
        log.warning("[Iceberg] Snapshot expiration failed (non-fatal): %s", e)
    return len(arrow)


def main() -> int:
    ap = argparse.ArgumentParser(description="USO holdings from SEC 10-Q/10-K schedules")
    ap.add_argument("--filings", type=int, default=1, help="newest N filings (default 1)")
    ap.add_argument("--dry-run", action="store_true", help="parse and print, don't write")
    args = ap.parse_args()

    frames = []
    for f in list_filings(args.filings):
        try:
            frames.append(parse_schedule(fetch_filing_html(f), f["period"], f["filing_date"]))
        except Exception as e:
            log.error("[USO %s %s] FAILED: %s", f["form"], f["period"], e)
    if not frames:
        log.error("No holdings parsed.")
        return 1
    df = pd.concat([f.astype(object) for f in frames], ignore_index=True)
    if args.dry_run:
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(df[["reporting_period_end", "asset_category", "holding_ticker", "holding_name",
                      "par_value", "shares_held", "market_value_usd", "weight_pct",
                      "maturity_date", "ytm_pct"]].to_string())
        return 0
    return 0 if write_to_iceberg(df) else 1


if __name__ == "__main__":
    sys.exit(main())
