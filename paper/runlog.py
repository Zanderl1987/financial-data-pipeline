"""Append one JSON line per account-run to a private repo via the GitHub
contents API. Never prints the token; callers must not put equity or account
numbers in the record."""
from __future__ import annotations

import base64
import json

import requests

_FIELDS = ("date", "account", "status", "n_orders", "notes", "run_id")


def append_run(record: dict, repo: str, token: str, path: str = "runs.jsonl") -> None:
    if not repo or not token:
        raise ValueError("log repo/token not configured")
    rec = {k: record.get(k) for k in _FIELDS}
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
            old = base64.b64decode(body["content"]).decode("utf-8")
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
