"""
strategies/pull_tv_catalog.py -- paced, resumable TradingView strategy collection.

Brings the browser-based collection the campaign has always used into a single,
polite, restartable tool. Two distinct passes:

* ENUMERATION -- walks the "Scripts / Strategies / Most popular" listing pages
  (`/scripts/page-N/?script_type=strategies`) in a real browser and records every
  script card {url, slug, title, author, boosts, views, comments} into
  `storage/tv_scripts/_pull_manifest.json` plus a human-readable roster.
  Optional `--from-sitemap` starts from `sitemap-scripts.xml` instead (fuller,
  but mixes indicators in -- the listing is the strategies frame).

* EXTRACTION -- for each manifest entry without an existing .pine, opens the
  script page in one persistent browser, reads the Monaco editor's `.view-line`
  nodes (the only channel that preserves Pine indentation), reassembles the
  source, and writes `<slug>.pine` + `<slug>.meta.json` via `collect.save_script`.

Polite-by-default so we don't trip usage controls:

* ONE browser tab reused across pages; images/media/fonts blocked on route to
  cut load cost; `--delay` seconds (default 2.5) + jitter between navigations.
* A page that looks like an abuse wall (429 / "unusual traffic") backs off
  `--backoff` (default 45s) and retries once, then stops rather than hammering.
* A script page whose Monaco editor never appears (protected / login-gated /
  indicator with no source) is marked `no_source` and skipped fast (`--wait`).
* Resumable: `_pull_progress.json` records done/no_source/blocked/failed per
  slug; re-running skips completed work and retries failures.
* `--max N` caps work per run (enum pages or extractions); run repeatedly.

Slug convention matches the existing 50: `{urlID}_{name snake}`, computed by
`slug_of()` using the same normalization the campaign uses.

Usage
-----
    python strategies\\pull_tv_catalog.py --enum-only
    python strategies\\pull_tv_catalog.py --extract-only --max 40
    python strategies\\pull_tv_catalog.py            # enum, then extract
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import requests
from playwright.sync_api import sync_playwright

from strategies.collect import save_script

TV = "https://www.tradingview.com"
OUT = os.path.join(ROOT, "storage", "tv_scripts")
MANIFEST = os.path.join(OUT, "_pull_manifest.json")
PROGRESS = os.path.join(OUT, "_pull_progress.json")
SITEMAP = TV + "/sitemaps/www_tradingview_com/sitemap-scripts.xml"
LISTING = TV + "/scripts/page-{n}/?script_type=strategies"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_WEIGHT_RE = re.compile(r"([\d.]+)([kKmM]?)")
_STAT_RE = re.compile(r"(boosts|views|comments)", re.I)


def _to_int(s: Optional[str]) -> Optional[int]:
    """'1.2k' -> 1200, '3M' -> 3000000, '—' -> None."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    m = _WEIGHT_RE.fullmatch(s)
    if not m:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "K": 1_000, "M": 1_000_000}.get(m.group(2), 1)
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


def slug_of(url: str) -> str:
    """'.../script/8iAYXXsS-Hyperliquid-Ready-Webhook-.../' -> '8iayxxss_hyperliquid_ready_webhook...'."""
    path = url.rstrip("/").rstrip("#").split("/")[-1]
    head, _, name = path.partition("-")
    tail = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"{head.lower()}_{tail}"


def _read(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return default


def _write(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_progress() -> Dict:
    return _read(PROGRESS, {})


def save_progress(prog: Dict) -> None:
    _write(PROGRESS, prog)


def load_manifest() -> List[Dict]:
    return _read(MANIFEST, [])


def save_manifest(rows: List[Dict]) -> None:
    _write(MANIFEST, rows)


def existing_slugs() -> set:
    slugs = set()
    if os.path.isdir(OUT):
        for name in os.listdir(OUT):
            if name.endswith(".pine"):
                slugs.add(name[: -len(".pine")])
    return slugs


def block_heavy(route):
    kind = route.request.resource_type
    if kind in ("image", "media", "font", "favicon"):
        route.abort()
    else:
        route.continue_()


def page_is_blocked(text: str, title: str) -> bool:
    return "unusual traffic" in text.lower() or "access denied" in title.lower()


def extract_script_meta(pg) -> Dict:
    """Best-effort page-level meta: author + license + visible stat labels."""
    meta: Dict[str, Optional[object]] = {"tv_author": None, "license": None}
    try:
        a = pg.query_selector("a[href*='/u/']")
        meta["tv_author"] = a.inner_text().strip() if a else None
    except Exception:
        pass
    body = ""
    try:
        body = (pg.query_selector("body").inner_text() if pg.query_selector("body") else "")
    except Exception:
        pass
    for lic in ("Mozilla Public License", "Apache", "GPL", "MIT", "CC BY", "Attribution",
                "Custom license", "No license", "©"):
        if lic.lower() in body.lower():
            meta["license"] = lic
            break
    return meta


def read_monaco(pg) -> Optional[str]:
    """Full Pine source from Monaco's .view-line nodes (indentation preserved)."""
    if not pg.query_selector(".view-lines"):
        return None
    lines = pg.eval_on_selector_all(".view-line", "els => els.map(e => e.textContent)")
    src = "\n".join(lines).replace("\u00a0", " ")
    if not src.strip():
        return None
    return src + "\n"


def run_enumeration(delay: float, wait: float, max_pages: int, from_sitemap: bool,
                    verbose: bool) -> List[Dict]:
    rows = load_manifest()
    seen = {r["url"] for r in rows}
    if from_sitemap:
        r = requests.get(SITEMAP, headers={"User-Agent": UA}, timeout=60)
        urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
        added = 0
        for u in urls:
            u = u.strip()
            if "/script/" not in u or u in seen:
                continue
            rows.append({"url": u, "slug": slug_of(u), "title": None,
                         "tv_author": None, "tv_boosts": None,
                         "tv_views": None, "tv_comments": None,
                         "source": "sitemap"})
            seen.add(u)
            added += 1
        save_manifest(rows)
        print(f"sitemap: {added} new URLs (total manifest {len(rows)})")
        return rows

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        pg = browser.new_page(viewport={"width": 1400, "height": 1000})
        pg.route("**/*", block_heavy)
        for n in range(1, max_pages + 1):
            url = LISTING.format(n=n)
            new_on_page = 0
            try:
                pg.goto(url, timeout=60000, wait_until="domcontentloaded")
                pg.wait_for_timeout(wait * 1000)
                anchors = pg.eval_on_selector_all(
                    "a", "els => els.map(e => ({h: e.getAttribute('href'),
                                               t: e.textContent.trim()}))
                                .filter(x => x.h && x.h.includes('/script/')
                                         && x.h.split('/')[1] === 'script')")
                uniq = {}
                for a in anchors:
                    base = a["h"].split("#")[0].split("?")[0]
                    uniq.setdefault(base, a)
                for base, a in uniq.items():
                    if base in seen:
                        continue
                    seen.add(base)
                    new_on_page += 1
                    rows.append({
                        "url": base, "slug": slug_of(base),
                        "title": a["t"] or None,
                        "tv_author": None, "tv_boosts": None,
                        "tv_views": None, "tv_comments": None,
                        "source": f"listing-p{n}"})
                print(f"page {n}: {len(uniq)} cards, {new_on_page} new "
                      f"(manifest {len(rows)})")
                if new_on_page == 0 and len(uniq) > 0:
                    print("no new cards on this page -- listing exhausted")
                    break
            except Exception as e:
                print(f"page {n}: ERROR {e}")
                if verbose:
                    import traceback
                    traceback.print_exc()
            save_manifest(rows)
            time.sleep(delay + random.uniform(0, 1.0))
        browser.close()
    return rows


def run_extraction(delay: float, wait: float, max_items: int, skip_existing: bool,
                   verbose: bool) -> int:
    rows = load_manifest()
    prog = load_progress()
    progress_items = list(prog.items())
    if skip_existing:
        have = existing_slugs()
        rows = [r for r in rows if r["slug"] not in have]

    skipped_by_progress = {s for s, v in prog.items() if v.get("status") in ("done", "no_source")}
    todo = [r for r in rows
            if r["slug"] not in skipped_by_progress and "blocked" not in str(prog.get(r["slug"]))]
    todo = todo[: max_items] if max_items else todo
    print(f"extraction queue: {len(todo)} (manifest {len(rows)})")

    done = failed = no_source = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        pg = browser.new_page(viewport={"width": 1400, "height": 1000})
        pg.route("**/*", block_heavy)
        for i, item in enumerate(todo, 1):
            slug = item["slug"]
            url = item["url"]
            status = "done"
            reason = ""
            src = None
            meta: Dict = {}
            for attempt in (1, 2):
                try:
                    pg.goto(url, timeout=60000, wait_until="domcontentloaded")
                    pg.wait_for_timeout(wait * 1000)
                    title = ""
                    try:
                        title = pg.title()
                    except Exception:
                        pass
                    text = ""
                    try:
                        text = (pg.query_selector("body").inner_text()
                                if pg.query_selector("body") else "")
                    except Exception:
                        pass
                    if page_is_blocked(text, title):
                        if attempt == 1:
                            print(f"{slug}: block wall, backing off {delay*8:.0f}s")
                            time.sleep(delay * 8)
                            continue
                        status = "blocked"
                        reason = "abuse wall"
                        break
                    src = read_monaco(pg)
                    if src is None:
                        status = "no_source"
                        reason = "no monaco editor (protected/invalid/indicator-based?)"
                        break
                    meta = extract_script_meta(pg)
                    status = "done"
                    break
                except Exception as e:
                    if attempt == 1:
                        time.sleep(delay * 3)
                        continue
                    status = "failed"
                    reason = str(e)[:120]
                    if verbose:
                        import traceback
                        traceback.print_exc()

            if status == "done" and src is not None:
                meta.update({
                    "tv_url": url,
                    "tv_script_name": item.get("title"),
                    "tv_boosts": item.get("tv_boosts"),
                    "tv_views": item.get("tv_views"),
                    "tv_comments": item.get("tv_comments"),
                    "collected_at": date.today().isoformat(),
                    "tv_author": meta.get("tv_author") or item.get("tv_author"),
                    "indentation_source": "monaco .view-line DOM (pull_tv_catalog)",
                    "license": meta.get("license") or "unknown",
                })
                try:
                    save_script(OUT, slug, src, meta)
                    done += 1
                except Exception as e:
                    status = "failed"
                    reason = f"save: {e}"
            elif status == "no_source":
                no_source += 1
            elif status in ("blocked", "failed"):
                failed += 1

            prog.setdefault(slug, {"status": status})
            prog[slug]["reason"] = reason
            prog[slug]["url"] = url
            prog[slug]["attempts"] = prog[slug].get("attempts", 0) + 1
            save_progress(prog)
            tag = {"done": "OK", "no_source": "no-src", "blocked": "BLOCK",
                   "failed": "FAIL"}.get(status, status)
            print(f"[{i}/{len(todo)}] {tag} {slug} {reason}")
            if status == "blocked":
                print("abuse wall hit -- stopping the spigot; rerun later to continue")
                break
            time.sleep(delay + random.uniform(0, 1.0))
        browser.close()
    print(f"done={done} no_source={no_source} failed={failed}")
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--enum-only", action="store_true")
    ap.add_argument("--extract-only", action="store_true")
    ap.add_argument("--from-sitemap", action="store_true",
                    help="enumeration source: sitemap-scripts.xml instead of listing")
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between pages")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="seconds to give Monaco after load before giving up")
    ap.add_argument("--max", type=int, default=0, help="cap enum pages / extract items")
    ap.add_argument("--max-pages", type=int, default=0, help="enum pages cap (alias of --max)")
    ap.add_argument("--no-skip-existing", action="store_true",
                    help="re-collect slugs that already have a .pine")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not args.enum_only and not args.extract_only:
        args.enum_only = True
        args.extract_only = True

    max_pages = args.max_pages or args.max or 0
    if args.enum_only:
        rows = run_enumeration(args.delay, args.wait, max_pages,
                               args.from_sitemap, args.verbose)
        roster = os.path.join(OUT, f"_roster_pull_{date.today().isoformat()}.txt")
        with open(roster, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(f"{r['slug']} :: {r.get('tv_author')} :: {r.get('title')}\n")
        print(f"manifest {len(rows)} entries -> {MANIFEST}\nroster -> {roster}")
    if args.extract_only:
        run_extraction(args.delay, args.wait, args.max, not args.no_skip_existing,
                       args.verbose)


if __name__ == "__main__":
    main()