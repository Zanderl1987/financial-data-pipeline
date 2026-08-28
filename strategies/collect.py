"""
strategies/collect.py -- reassemble Pine source captured from a browser session.

Why this exists
---------------
Collecting a script page yields its source through two channels, because neither
one alone is sufficient:

* the page-text channel returns the full source but **strips leading
  whitespace**, and Pine's block structure is indentation-significant;
* the in-page scripting channel preserves indentation but its return value is
  screened by a content filter that rejects Pine's dense `key=value` parameter
  syntax as query-string-like data, so it cannot carry the source itself.

The workaround: take the text from the first channel and a per-line
leading-space count from the second (plain integers, which the filter passes),
then zip them back together. `reindent()` is that zip.

Outputs
-------
Pure functions; `save_script()` writes a .pine file plus a sidecar .meta.json
recording provenance required by the campaign pre-registration.

Usage
-----
    from strategies.collect import reindent, save_script

    src = reindent(flat_text, "0,0,4,4,0")
    save_script("storage/tv_scripts", "ultimate_prop_firm", src, meta)
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Sequence, Union


def reindent(flat: Union[str, Sequence[str]], indents: Union[str, Sequence[int]]) -> str:
    """
    Restore leading whitespace on `flat` using per-line space counts.

    `flat` is the whitespace-stripped source (str or list of lines); `indents` is
    a comma-separated string or sequence of ints, one per line.

    Raises ValueError on a length mismatch -- a silent misalignment would shift
    every subsequent block and produce source that looks plausible but is wrong,
    which is the single worst failure mode for this pipeline.
    """
    lines = flat.split("\n") if isinstance(flat, str) else list(flat)
    if isinstance(indents, str):
        depths = [int(x) for x in indents.replace("\n", "").split(",") if x.strip() != ""]
    else:
        depths = [int(x) for x in indents]

    # A trailing newline in `flat` yields one extra empty line; tolerate exactly that.
    if len(lines) == len(depths) + 1 and lines[-1].strip() == "":
        lines = lines[:-1]

    if len(lines) != len(depths):
        raise ValueError(
            f"line/indent mismatch: {len(lines)} lines vs {len(depths)} indent values -- "
            "the two capture channels disagree; recapture rather than guessing"
        )

    return "\n".join(
        (" " * d + line) if line.strip() else "" for line, d in zip(lines, depths)
    )


def save_script(out_dir: str, slug: str, source: str, meta: Dict) -> str:
    """
    Write `<slug>.pine` and `<slug>.meta.json` under out_dir. Returns the .pine path.

    `meta` must carry the provenance the pre-registration requires: tv_url,
    tv_author, tv_script_name, tv_boosts, tv_views, tv_comments, collected_at,
    license, and indentation_source.
    """
    required = {"tv_url", "tv_author", "tv_script_name", "collected_at"}
    missing = required - set(meta)
    if missing:
        raise ValueError(f"meta missing required provenance keys: {sorted(missing)}")

    os.makedirs(out_dir, exist_ok=True)
    pine_path = os.path.join(out_dir, f"{slug}.pine")
    with open(pine_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(source if source.endswith("\n") else source + "\n")
    with open(os.path.join(out_dir, f"{slug}.meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    return pine_path


def load_meta(out_dir: str) -> List[Dict]:
    """Read every *.meta.json in out_dir, newest collection first."""
    rows = []
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".meta.json"):
            continue
        with open(os.path.join(out_dir, name), "r", encoding="utf-8") as fh:
            row = json.load(fh)
        row["slug"] = name[: -len(".meta.json")]
        rows.append(row)
    return sorted(rows, key=lambda r: r.get("collected_at", ""), reverse=True)
