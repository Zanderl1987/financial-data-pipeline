"""Append one JSON line per account-run to a private repo via the GitHub
contents API. Never prints the token; callers must not put equity or account
numbers in the record."""
from __future__ import annotations

import base64
import json

import requests

_FIELDS = ("date", "account", "status", "n_orders", "notes", "run_id")


def log_path(day: str) -> str:
    """runs/YYYY-MM.jsonl for a record's New York date (YYYY-MM-DD...). One
    file per month keeps each file far below the contents API's 1 MB limit."""
    return f"runs/{str(day)[:7]}.jsonl"


def _existing_text(body: dict) -> str:
    """Decoded content of an existing file. Raises rather than return "" when
    the API reports a non-empty file without its content (it omits content
    above 1 MB): writing "" + one line would erase the log."""
    content = body.get("content")
    missing = int(body.get("size") or 0) > 0 and not content
    if body.get("encoding", "base64") != "base64" or missing:
        raise RuntimeError("run log content not returned; refusing to overwrite")
    return base64.b64decode(content or "").decode("utf-8")


def append_run(record: dict, repo: str, token: str, path: str | None = None) -> None:
    if not repo or not token:
        raise ValueError("log repo/token not configured")
    rec = {k: record.get(k) for k in _FIELDS}
    path = path or log_path(rec["date"])
    line = json.dumps(rec, default=str) + "\n"
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    for attempt in range(2):
        r = requests.get(url, headers=headers, timeout=30)
        sha, old = None, ""
        if r.status_code == 200:
            body = r.json()
            sha = body["sha"]
            old = _existing_text(body)
        elif r.status_code != 404:
            r.raise_for_status()
        if old and not old.endswith("\n"):
            old += "\n"
        payload = {"message": f"paper run {rec['date']} {rec['account']}",
                   "content": base64.b64encode((old + line).encode("utf-8")).decode("ascii")}
        if sha:
            payload["sha"] = sha
        p = requests.put(url, headers=headers, json=payload, timeout=30)
        if p.status_code == 409 and attempt == 0:
            continue
        p.raise_for_status()
        return
