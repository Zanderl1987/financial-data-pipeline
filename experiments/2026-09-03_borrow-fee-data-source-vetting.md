# Per-symbol borrow-fee data source — GO/NO-GO vetting

**Date:** 2026-09-03
**Trigger:** TASKS.md's "Short borrow cost" item — `evaluation/execution.py`'s
`CostModel.borrow_fee_bps` and `evaluation/trades.py`'s short-trade accrual use one flat
annualized rate for every shorted name, understating risk specifically on the hardest-to-
borrow names, which is exactly where it matters most. "Would need a per-symbol/per-date
borrow-fee data source, which doesn't exist in this repo yet (a real NO-GO/GO vetting
question of its own)." Research only — no pipeline code written, nothing installed.

## Question

Is there a real, ideally free/keyless, per-symbol borrow-fee (stock loan rate /
hard-to-borrow fee) data source this repo could build a pipeline against, matching the
`data-source-vetting` skill's standard (ToS, access model, rate limits, data shape/depth
probed before writing a word of pipeline code)?

## Method

Checked the well-known securities-lending data vendors first to rule the paid-only ones
out quickly, then searched specifically for free/keyless angles: Interactive Brokers'
own public infrastructure, Fintel's free tier, Schwab's existing API surface (this repo
already has a working `schwabdev` OAuth pipeline — see `CLAUDE.md`'s Schwab section — so
checking whether it already covers this was cheap), and any FINRA/SEC byproduct data.
This repo's own `docs/SHORT_INTEREST_SOURCES.md` was checked first to confirm none of the
three short-interest sources already wired in (yfinance snapshot, FINRA Reg SHO biweekly,
SEC FTD) are actually borrow-FEE data — short interest (how many shares are shorted) and
borrow fee (the dollar cost to borrow them) are different, commonly-conflated concepts;
confirmed the gap is real, not already half-solved. For the strongest lead found (below),
attempted a live connectivity probe from this session's own network, not just a docs read.

## Findings

### Paid-only, ruled out without belaboring

- **S3 Partners, IHS Markit / Markit Securities Finance, ORTEX (full data)**: institutional
  securities-lending data vendors, subscription/enterprise pricing, no free or keyless
  tier that covers real borrow-fee rates. Not investigated further — this matches the
  category this repo has already ruled out elsewhere for adjacent needs (e.g. FINRA's
  full NMS short-interest requiring registered API credentials this repo doesn't have).
- **Fintel.io**: DOES carry borrow-fee/cost-to-borrow data (start/min/max/latest rate,
  updated ~every 30 min) alongside its short-interest data — genuinely the right kind of
  data. But it's a paid REST API (`FINTEL_API_KEY`, plans $10.95-$95/mo); no evidence of a
  free API tier for this specifically (the free tier is web-page viewing, not
  programmatic bulk access). **NO-GO for a free pipeline**, but noted as the fallback paid
  option if a keyless source ever breaks.

### Schwab (this repo's existing brokerage integration) — confirmed NO-GO, not just absent

Checked rather than assumed, since this repo already has real, hard-won Schwab API limits
on record (`CLAUDE.md`: "Trader API (positions/transactions) 401s until enabled"; "no
historical options"). No evidence anywhere — official docs, third-party API trackers, or
community libraries (`schwabr`, `SchwabPy`) — of a hard-to-borrow-rate or short-locate
endpoint in either the Schwab Market Data or Trader API. Consistent with the existing
pattern: Schwab's retail API surface is narrower than a full brokerage platform's
internal tools. **NO-GO** — nothing to build against.

### Interactive Brokers' public FTP feed — the real candidate, GO (with one caveat)

IBKR publishes its **entire stock-loan database** (fee rates, rebate rates, share
availability) as a **plain-text file over FTP, with a shared, password-less login** —
not gated behind a funded/live brokerage account:

- **Host**: `ftp3.interactivebrokers.com`, **username**: `shortstock`, no password.
- **Path**: one file per country, e.g. `usa.txt` for US-listed names (also
  `germany.txt`, etc. per-country files exist).
- **Format**: pipe-delimited, header row `#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|
  AVAILABLE|` — symbol, currency, name, contract type, ISIN, rebate rate, fee rate,
  shares available. `FEERATE` is the field that maps onto `CostModel.borrow_fee_bps`
  (annualized, per name) once a live pull confirms its exact units (bps vs. a decimal
  percent) — a 5-minute check on first real implementation, not a research blocker.
- **Confirmed independently, not from one source**: a Portfolio123 forum thread, an
  Elite Trader forum thread, and a working, MIT-style open-source script
  (`tangentstorm/gme-data`'s `borrowable.py`, uses plain `ftplib.FTP('ftp3.
  interactivebrokers.com'); ftp.login('shortstock')`) all describe and use the exact
  same host/login/format. **IBorrowDesk** (iborrowdesk.com), a small third-party site
  that visualizes borrow fees, is itself built by mirroring this same feed — further
  independent confirmation it's real, working, and update-worthy data, though the
  Portfolio123 thread notes IBorrowDesk's OWN derived data has had quality issues
  (mirroring bugs on their end, not IBKR's raw feed).
- **Depth**: **snapshot-only, no history** — an IBKR user in the Portfolio123 thread
  states this directly ("there is no historical data on the FTP... [users] downloaded
  between 4a and 7a each day" to build their own history). Same shape as this repo's
  existing `tradingview_pipeline.py`/`schwab_movers_pipeline.py` "daily accumulator"
  pattern (`CLAUDE.md`'s "Open work" section) — history only exists from the day a
  pipeline starts running it, which is an accepted, already-precedented tradeoff here,
  not a new one.
- **Rate limits**: none documented or reported by any source found; a daily pull (the
  cadence real users report) is clearly fine, and this repo's own convention (one run/day
  via `run_all.py`) matches that with room to spare.
- **Legality/ToS**: no ToS prohibition found anywhere searched; the credential is public,
  documented in multiple independent public forum threads and a public GitHub project
  using it exactly this way for years, and IBKR's own marketing pages point at "Short
  Sale Cost" / "Short-Securities Availability" tools built on the same underlying data —
  this reads as IBKR intentionally publishing the feed for exactly this kind of use, not
  a leaked or unauthorized-access situation.

**Connectivity, checked from BOTH the research sandbox and the actual dev machine that
runs this repo's pipelines (2026-09-03, follow-up to the fork's own probe)**: identical
failure signature in both places. `curl ftp://shortstock@ftp3.interactivebrokers.com/
usa.txt` times out (curl exit 28); a raw TCP connect to port 21 on that host also times
out (`/dev/tcp` probe, 8s); an HTTPS fallback to the same host times out too (status
000). DNS resolves cleanly to `206.106.137.27` in both environments, and a general
internet sanity check (`https://example.com`) succeeds immediately right alongside the
failures — so this is specifically port 21 / that host being unreachable, not a broader
network outage. Since the SAME pattern now reproduces from a real Windows dev box (not
just an agent sandbox), the sandbox-egress-allowlist explanation the fork offered is
less likely to be the whole story. The much more common real-world explanation for "port
21 specifically dead, HTTPS/DNS/everything else fine" is a router/ISP/firewall-level FTP
block — FTP is a legacy protocol many consumer routers and ISPs throttle or drop by
default (unlike a corporate proxy allowlist, this doesn't require any deliberate
sandboxing to produce). This is a genuinely open, unresolved connectivity question, not
confirmed either way as "IBKR blocks non-account requests" or "purely an artifact of
where this was tested from" — it needs a check from a network known to allow outbound
FTP (a different ISP/network, a VPN, or a cloud VM) before committing to building a
pipeline against it.

## Verdict

**Conditionally GO — the data source itself checks out completely, but a real,
now-twice-confirmed connectivity blocker means this is NOT ready to build against yet.**
Interactive Brokers' `usa.txt` FTP feed is real, keyless (a shared public login, not an
account-gated one), well-documented across multiple independent sources, plain-text and
easy to parse, and directly serves per-symbol borrow-fee data (not a proxy like short
interest or FTD, which this repo already has three sources of and which are NOT the same
signal). It is snapshot-only — same accepted tradeoff as this repo's other daily
accumulators — so `borrow_fee_bps` per name only becomes richly available from the day a
pipeline starts running, with the flat rate remaining the honest fallback for any date
before that.

**But outbound port 21 to that host is unreachable from both places this was tested**
(the research fork's sandbox AND this repo's actual dev machine), while DNS and general
internet both work fine in both places. Next step before writing
`ibkr_borrow_fee_pipeline.py`: retest from a network known to permit outbound FTP (a
different ISP connection, a VPN, or a small cloud VM) to determine whether this is a
local router/ISP FTP block (fixable — a network/router setting, or just running the pull
from a different machine/network) or something IBKR-side (which would make this a real
NO-GO). **This is exactly the kind of infrastructure check that's a "does Zander want to
spend time on this" question, not something to route around unilaterally** — no attempt
was made here to bypass the block (e.g. via a proxy or alternate protocol trick), matching
the standing policy against circumventing network/access restrictions.

**If the connectivity question resolves against building this** (confirmed IBKR-side
block, or no accessible network available): the fallback is Fintel's paid API (confirmed
to carry the right data, just not free) rather than another free search — this vetting
pass didn't find a second free candidate, so that would be a real NO-GO requiring a
paid-tier decision, not a prompt to keep searching for a third free option.

Sources:
- [IBKR Short-Securities Availability](https://www.interactivebrokers.com/en/trading/short-securities-availability.php)
- [IBKR Securities Financing](https://www.interactivebrokers.com/en/trading/securities-financing.php)
- [IBKR Short Sale Cost](https://www.interactivebrokers.com/en/pricing/short-sale-cost.php)
- [IBKR Securities Lending Dashboard](https://www.interactivebrokers.com/en/trading/securities-lending-dashboard.php)
- [tangentstorm/gme-data borrowable.py (GitHub)](https://github.com/tangentstorm/gme-data/blob/main/borrowable.py)
- [Historical data on IB short lending fee and share availability (Portfolio123 forum)](https://community.portfolio123.com/t/historical-data-on-ib-short-lending-fee-and-share-availability/58254)
- [Querying IB's borrow fee rate directly from Excel (Elite Trader forum)](https://www.elitetrader.com/et/threads/querying-ibs-borrow-fee-rate-directly-from-excel.347024/)
- [IBorrowDesk](https://www.iborrowdesk.com/)
- [hahalml/I-Borrow-Desk (GitHub)](https://github.com/hahalml/I-Borrow-Desk)
- [Fintel short-interest/borrow-rate API](https://fintel.io/ss/us/api)
- [Fintel.io Review 2026 (curvedtrading.com)](https://curvedtrading.com/articles/en/reviews/fintel-io-review/)
- [Charles Schwab API tracker](https://apitracker.io/a/schwab)
- [schwabr R package docs (CRAN)](https://cran.r-project.org/web/packages/schwabr/schwabr.pdf)
