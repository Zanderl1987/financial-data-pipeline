"""
strategies/pull_tv_catalog.py -- paced, resumable TradingView strategy collection.

Brings the collection the campaign has always used into a single, polite,
restartable tool. Two distinct passes:

* ENUMERATION -- walks the "Scripts / Strategies / Most popular" listing pages
  (`/scripts/page-N/?script_type=strategies`) in a real browser and records every
  script card {url, slug, title} into `storage/tv_scripts/_pull_manifest.json`
  plus a human-readable roster. Optional `--from-sitemap` starts from
  `sitemap-scripts.xml` instead (fuller, but mixes indicators in -- the listing
  is the strategies frame).

* EXTRACTION -- for each manifest entry without an existing .pine, fetches the
  script page and the `pine-facade.tradingview.com` blob that backs the
  "Source code" tab (2 plain HTTP GETs, no browser, no reCAPTCHA), reassembles
  the source, and writes `<slug>.pine` + `<slug>.meta.json` via
  `collect.save_script`.

Polite-by-default so we don't trip usage controls:

* `--delay` seconds (default 2.5) + jitter between requests.
* A wall-shaped response (429 / "unusual traffic") backs off `--delay`*8 and
  retries once, then stops rather than hammering.
* A script whose pine-facade blob has no source (access-gated /
  recently-deleted / malformed) is marked `no_source` and skipped fast.
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
from typing import Any, Dict, List, Optional

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
ENUM_STATE = os.path.join(OUT, "_pull_enum_state.json")
SITEMAP = TV + "/sitemaps/www_tradingview_com/sitemap-scripts.xml"
LISTING = TV + "/scripts/page-{n}/?script_type=strategies"
PINE_FACADE = "https://pine-facade.tradingview.com/pine-facade/get/{id}/{v}?no_4xx=true"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_SSR_RE = re.compile(r"ssrIdeaData\":(\{.*?)\};", re.S)
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


def fetch_ssr(url: str, timeout: int = 60) -> Dict:
    """GET a script page and return its `ssrIdeaData` JSON (or raise)."""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    text = r.text.lower()
    if "unusual traffic" in text or "access denied" in r.text:
        raise RuntimeError("abuse wall")
    for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.S | re.I):
        if "ssrIdeaData" not in s or len(s) < 5000:
            continue
        idx = s.find("ssrIdeaData")
        start = s.find("{", idx)
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            return json.loads(s[start:end])
        except Exception:
            continue
    raise RuntimeError("ssrIdeaData not found")


def ssr_meta(data: Dict) -> Dict:
    """Provenance from the page SSR payload (author, stats, type, names)."""
    user = data.get("user") or {}
    script = data.get("script") or {}
    meta: Dict[str, Optional[object]] = {
        "tv_author": user.get("username"),
        "tv_boosts": data.get("likes_count"),
        "tv_views": data.get("views"),
        "tv_comments": data.get("comments_count"),
        "tv_script_name": data.get("name"),
        "script_type": script.get("script_type"),
        "script_access": script.get("access"),
    }
    return meta


def fetch_pine_facade(id_part: str, version: Any, timeout: int = 60) -> Dict:
    """GET the pine-facade source blob; raises on transport errors."""
    r = requests.get(PINE_FACADE.format(id=id_part, v=version),
                     headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code == 429 or "unusual traffic" in r.text.lower():
        raise RuntimeError("abuse wall")
    r.raise_for_status()
    return r.json()


def pine_version_of(src: str) -> Optional[int]:
    m = re.search(r"//@version\s*[= ]\s*(\d+)", src)
    return int(m.group(1)) if m else None


def license_of(src: str) -> str:
    head = src[:600].lower()
    for lic, label in (
        ("mozilla public license", "MPL-2.0 (TradingView default)"),
        ("apache", "Apache 2.0"),
        ("gnu general public license", "GPL"),
        ("mit license", "MIT"),
        ("creative commons", "CC BY"),
    ):
        if lic in head:
            return label
    return "unknown"


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
        state = _read(ENUM_STATE, {"last_page": 0})
        start_page = int(state.get("last_page", 0)) + 1
        prev_uniq = None
        for n in range(start_page, max_pages + 1):
            url = LISTING.format(n=n)
            new_on_page = 0
            try:
                pg.goto(url, timeout=60000, wait_until="domcontentloaded")
                pg.wait_for_timeout(wait * 1000)
                anchors = pg.eval_on_selector_all(
                    "a",
                    """els => els.map(e => ({h: e.getAttribute('href'), t: e.textContent.trim()}))
                                .filter(x => x.h && x.h.includes('/script/')
                                         && !x.h.includes('/scripts/'))""")
                uniq = {}
                for a in anchors:
                    base = a["h"].split("#")[0].split("?")[0]
                    uniq.setdefault(base, a)
                dupe_page = prev_uniq is not None and uniq and uniq.keys() == prev_uniq
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
                state["last_page"] = n
                _write(ENUM_STATE, state)
                save_manifest(rows)
                if not uniq:
                    print("page rendered 0 cards -- listing done")
                    break
                if dupe_page:
                    print("page identical to previous -- listing done")
                    break
                prev_uniq = set(uniq.keys())
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
    if skip_existing:
        # Seed progress for manifest slugs whose URL already has a collected
        # meta, even when the legacy slug name doesn't match this tool's
        # convention.
        seen_done = {s for s, v in prog.items() if v.get("status") == "done"}
        seeded = 0
        if os.path.isdir(OUT):
            for name in os.listdir(OUT):
                if not name.endswith(".meta.json"):
                    continue
                try:
                    with open(os.path.join(OUT, name), encoding="utf-8") as fh:
                        u = json.load(fh).get("tv_url")
                except Exception:
                    continue
                if not u:
                    continue
                u = u.rstrip("/")
                for r in rows:
                    if r["url"].rstrip("/") == u and r["slug"] not in seen_done:
                        prog[r["slug"]] = {"status": "done", "reason": "already collected"}
                        seeded += 1
        save_progress(prog)
        if seeded:
            print(f"seeded {seeded} slugs as done (already collected)")

    skipped_by_progress = {s for s, v in prog.items() if v.get("status") in ("done", "no_source")}
    todo = [r for r in rows
            if r["slug"] not in skipped_by_progress
            and "blocked" not in str(prog.get(r["slug"]))]
    todo = todo[: max_items] if max_items else todo
    print(f"extraction queue: {len(todo)} (manifest {len(rows)})")

    done = failed = no_source = 0
    for i, item in enumerate(todo, 1):
        slug = item["slug"]
        url = item["url"]
        status = "done"
        reason = ""
        src = None
        meta: Dict = {}
        for attempt in (1, 2):
            try:
                data = fetch_ssr(url)
                script = data.get("script") or {}
                id_part = script.get("script_id_part")
                version_maj = script.get("version_maj")
                src_meta = ssr_meta(data)
                if not id_part or version_maj is None:
                    status = "failed"
                    reason = "no script id in SSR payload"
                    break
                blob = fetch_pine_facade(id_part, version_maj)
                src = blob.get("source") or ""
                if not src.strip():
                    status = "no_source"
                    reason = f"empty source (access={blob.get('scriptAccess')})"
                    break
                src += "" if src.endswith("\n") else "\n"
                meta = dict(src_meta)
                meta["license"] = license_of(src)
                meta["pine_version"] = pine_version_of(src)
                status = "done"
                break
            except RuntimeError as e:
                if str(e) == "abuse wall":
                    if attempt == 1:
                        print(f"{slug}: block wall, backing off {delay*8:.0f}s")
                        time.sleep(delay * 8)
                        continue
                    status = "blocked"
                    reason = "abuse wall"
                    break
                status = "no_source"
                reason = str(e)[:120]
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
                "tv_script_name": meta.get("tv_script_name") or item.get("title"),
                "tv_boosts": meta.get("tv_boosts") or item.get("tv_boosts"),
                "tv_views": meta.get("tv_views") or item.get("tv_views"),
                "tv_comments": meta.get("tv_comments") or item.get("tv_comments"),
                "collected_at": date.today().isoformat(),
                "tv_author": meta.get("tv_author") or item.get("tv_author"),
                "indentation_source": "pine-facade (pull_tv_catalog)",
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
                    help="seconds to let a rendering listing page settle before reading cards")
    ap.add_argument("--max", type=int, default=0, help="cap enum pages / extract items")
    ap.add_argument("--max-pages", type=int, default=1000, help="enum pages cap (alias of --max)")
    ap.add_argument("--no-skip-existing", action="store_true",
                    help="re-collect slugs that already have a .pine")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not args.enum_only and not args.extract_only:
        args.enum_only = True
        args.extract_only = True

    max_pages = args.max_pages or args.max or 1000
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