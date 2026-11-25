"""Pre-launch check of the six paper accounts with the real secrets (plan Task 10).

Runs once on GitHub Actions (paper_key_check.yml), since the keys live only in
GitHub secrets. The repo is public, so stdout carries yes/no results only:
never equity, account numbers, order ids or keys.

Checks per account: the key works, the account is ACTIVE, its equity equals the
planned start ($5,000 / $10,000), it holds no positions or open orders, the six
keys point at six different accounts, and its daily portfolio history has no
zero-equity points and labels that history_dates() maps onto trading days.
On the first account only: a 1-share market-on-close order with an id outside
the job's "pt|" namespace is placed (so the job's order readers ignore it),
read back by its client id, then cancelled. Last: one record is written to the run-log repo
(checks/key_check.jsonl, which the dashboard does not read) to prove the token
can write.
"""
from __future__ import annotations

import os
import sys
import time as _time
import uuid
from datetime import date, datetime, time, timedelta

from paper.accounts import ACCOUNTS
from paper.broker import NY, Broker
from paper.dashboard_data import history_dates
from paper.decide import OrderIntent, parse_client_order_id
from paper.runlog import append_run

ORDER_SYMBOL = "SPY"
CHECK_PREFIX = "keycheck|"
LOG_PATH = "checks/key_check.jsonl"
# a cls order fills at 16:00 ET; never place the test order this close to it
NO_ORDER_FROM, NO_ORDER_UNTIL = time(15, 30), time(16, 15)
CANCEL_WAIT_S = 20


def yn(ok: bool) -> str:
    return "yes" if ok else "NO"


def check_account(spec, b) -> tuple[bool, dict, str | None]:
    """(ok, results, account_number). results values are 'yes'/'NO' or short
    status words; the account number stays in memory for the distinct check."""
    a = b.account()
    status = str(getattr(a.status, "value", a.status)).upper()
    equity = float(a.equity)
    res = {
        "active": yn(status == "ACTIVE"),
        "start_equity": yn(abs(equity - spec.start_equity) < 1.0),
        "no_positions": yn(len(b.positions()) == 0),
        "no_open_orders": yn(len(b.open_orders()) == 0),
    }
    today = datetime.now(NY).date()
    try:
        h = b.portfolio_history(today - timedelta(days=14))
    except Exception as e:
        res["history"] = f"NO ({type(e).__name__})"
        return False, res, str(a.account_number)
    eq = [x for x in (getattr(h, "equity", None) or [])]
    ts = [int(t) for t in (getattr(h, "timestamp", None) or [])]
    res["history_points"] = str(len(ts))
    res["no_zero_equity"] = yn(all(x is not None and float(x) > 0 for x in eq))
    hours = sorted({datetime.fromtimestamp(t, NY).hour for t in ts})
    res["label_hours_et"] = ",".join(str(x) for x in hours) or "none"
    if ts:
        days = set(b.calendar_days(today - timedelta(days=21), today))
        res["labels_on_trading_days"] = yn(all(d in days for d in history_dates(ts)))
    ok = all(v != "NO" for v in res.values())
    return ok, res, str(a.account_number)


def order_check(b, now_et: datetime) -> tuple[bool, dict]:
    """Place, verify ignored, cancel. Refuses near the close."""
    if NO_ORDER_FROM <= now_et.time() <= NO_ORDER_UNTIL:
        return False, {"order": "skipped (too close to 16:00 ET); rerun earlier"}
    cid = f"{CHECK_PREFIX}{now_et.date().isoformat()}|{uuid.uuid4().hex[:8]}"
    res = {"id_outside_job_namespace": yn(parse_client_order_id(cid) is None)}
    try:
        oid = b.submit(OrderIntent(ORDER_SYMBOL, "buy", 1, "check", cid))
    except Exception as e:
        res["order_accepted"] = f"NO ({type(e).__name__}: {str(e)[:120]})"
        return False, res
    res["order_accepted"] = "yes"
    final = "unknown"
    try:
        o = b.get_order(oid)
        res["accepted_status"] = str(getattr(o.status, "value", o.status))
        res["time_in_force_cls"] = yn(
            str(getattr(o.time_in_force, "value", o.time_in_force)).lower() == "cls")
        # Alpaca must hand our client_order_id back verbatim: the job's
        # once-a-day guard and its position bookkeeping both read it
        res["client_id_round_trip"] = yn(cid in b.orders_today(cid))
    finally:
        try:
            b.cancel(oid)
        except Exception as e:
            res["cancel_call"] = f"NO ({type(e).__name__})"
        deadline = _time.monotonic() + CANCEL_WAIT_S
        while True:
            final = str(getattr(b.get_order(oid).status, "value", "")).lower()
            if final in ("canceled", "cancelled", "filled") or _time.monotonic() > deadline:
                break
            _time.sleep(1)
    res["cancelled"] = yn(final in ("canceled", "cancelled"))
    if res["cancelled"] == "NO":
        res["ACTION"] = (f"order not confirmed cancelled (status {final}): cancel the "
                         f"{ORDER_SYMBOL} order by hand in Alpaca before 16:00 ET")
    return all("NO" not in str(v) for v in res.values()), res


def log_check(repo: str, token: str, now_et: datetime, run_id: str, log=append_run) -> str:
    if not repo or not token:
        return "NO (PAPER_LOG_REPO / PAPER_LOG_TOKEN missing)"
    try:
        log({"date": now_et.date().isoformat(), "account": "all", "status": "key_check",
             "n_orders": 0, "notes": "", "run_id": run_id}, repo, token, path=LOG_PATH)
        return "yes"
    except Exception as e:
        return f"NO ({type(e).__name__})"


def run(broker_factory=Broker, env=os.environ, log=append_run, now_et=None) -> int:
    all_ok, numbers = True, {}
    first = first_key = None
    for spec in ACCOUNTS:
        key, secret = env.get(spec.env_key), env.get(spec.env_secret)
        if not key or not secret:
            print(f"{spec.key}: NO (secret {spec.env_key} / {spec.env_secret} missing)")
            all_ok = False
            continue
        if not key.startswith("PK"):
            print(f"{spec.key}: NO (key is not a paper key: paper keys start with PK)")
            all_ok = False
            continue
        try:
            b = broker_factory(key, secret)
            ok, res, number = check_account(spec, b)
        except Exception as e:
            print(f"{spec.key}: NO (key rejected or API error: {type(e).__name__})")
            all_ok = False
            continue
        numbers.setdefault(number, []).append(spec.key)
        if first is None:
            first, first_key = b, spec.key
        all_ok &= ok
        print(f"{spec.key}: " + " ".join(f"{k}={v}" for k, v in res.items()))
    dupes = [v for v in numbers.values() if len(v) > 1]
    print("distinct accounts: " + ("yes" if not dupes else
          "NO (same account behind " + "; ".join("+".join(v) for v in dupes) + ")"))
    all_ok &= not dupes
    now_et = now_et or datetime.now(NY)
    if first is not None:
        ok, res = order_check(first, now_et)
        print(f"order test ({first_key}): " + " ".join(f"{k}={v}" for k, v in res.items()))
        all_ok &= ok
    lg = log_check(env.get("PAPER_LOG_REPO", ""), env.get("PAPER_LOG_TOKEN", ""),
                   now_et, env.get("GITHUB_RUN_ID", ""), log=log)
    print(f"run log write: {lg}")
    all_ok &= lg == "yes"
    print("KEY CHECK PASS" if all_ok else "KEY CHECK FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run())
