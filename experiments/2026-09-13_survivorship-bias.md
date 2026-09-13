# Delisting-inclusive equity panel: survivorship-bias measurement (2026-09-13)

Reproduce everything here with:

    C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_survivorship-bias.py --umd
    C:\ProgramData\anaconda3\python.exe experiments/2026-09-13_survivorship-bias.py --panel yfinance_universe_prices

Context: UMD and KLN both came back null on the in-house panels (writeup
`2026-09-12_equity-factors-sweep.md`). The one open caveat was alive-2026
survivorship: `yfinance_universe_prices` has no delisted names, which inflates
the short-leg return of any L/S factor and compresses the spread. The agreed
"honest next test" was a delisting-inclusive panel from the Sharadar SEP
delisting reference joined to in-store price history. This pass runs that
measurement BEFORE building anything, because the pipeline docstring warned the
price history itself may only exist behind a paid subscription.

## What the data can and cannot support

The Sharadar TICKERS table (free tier, `delisting_reference`, SEP universe,
snapshot 2026-09-12) is a complete security master: 20,966 equities ever priced,
6,325 alive and 14,641 delisted, each with first/last traded date. It does NOT
contain price history. The question is whether in-store price tables carry any
of the dead names' history.

**Two structural facts must be respected when matching (ditto the pipeline
docstring):**
1. Tickering is recycled. WM is Waste Management now, was Washington Mutual;
   WB is Weibo now, was Wachovia. A panel keyed on `ticker` silently splices one
   company's history onto another's.
2. firstpricedate is a 1986 floor for many older names; only 1998+ delistings
   are real per SEP (2 in 1997, 455 in 1998).

Matching rule used here: a delisted name is recovered **genuinely** only when
its panel series BOTH starts within ~370d of the SEP firstpricedate AND ends
within ~370d of the SEP lastpricedate. Anything else is either recycled-ticker
pollution (series runs to the present: a different live company holding the dead
symbol) or a partial/fragmented series.

## Results

### `prices` panel (27,759 symbols — the wide multi-source backadjusted table)

| delisted decade | SEP delisted | genuine recoveries | recovery |
|---|---|---|---|
| 1990s | 1,387 | 0 | 0.00% |
| 2000s | 5,425 | 0 | 0.00% |
| 2010s | 3,623 | 1 | 0.03% |
| 2020s | 4,206 | 166 | 3.95% |

- Of 14,641 delisted names, 1,067 share a `ticker` string with a `prices`
  symbol — but **900 of those are recycled-ticker pollution** (their series run
  to 2026; they are different live companies), so only 167 are genuine.
- Genuine recoveries are almost entirely 2019+ delistings (e.g. LPSN, LEG,
  NTZ), i.e. names that recently died and are still sitting in the snapshot.
  Historical dead names (the ones a momentum backtest's short leg actually
  wants) are essentially absent: **0 recoveries in the 1990s and 2000s.**

Point-in-time coverage of the true SEP-alive universe by the `prices` panel
(full universe alive at each month-end, not just today-alive names):

- ~18% in 1995, ~20% 2000, ~27% 2005, ~40% 2010, ~48% 2015, ~61% 2020, ~85%
  2025, ~91% 2026.

The coverage rise is the accretion of the *currently* living universe, not the
dead: the ~70-80% of each historical market that later died is permanently
absent. The recent numbers look fine only because SEP's alive set is nearly all
present in the panel; the hole is exactly where a momentum study needs history.

### `yfinance_universe_prices` panel (2,570 symbols — the Russell-3000 survivor snapshot UMD/KLN actually ran on)

- 2,551 of 6,325 alive SEP names present; **16 of 14,641 delisted string
  matches, of which 15 are genuine** (0.36% of the 2020s delisted; 0 everywhere
  else — 0/1,387 1990s, 0/5,425 2000s, 0/3,623 2010s).
- 40% coverage of the full SEP-alive universe by 2026 (the panel is a
  large/mid-cap slice by design), but near-zero recovery of dead names in any
  decade.

## What this does to the UMD / KLN verdicts

Direction of the bias: the missing names are disproportionately the distressed
names that crashed then delisted — exactly the bottom decile a momentum short
leg wants. Dropping them inflates the measured short-leg return, so the measured
L/S spread is a LOWER bound on the true spread: the null verdicts are
**conservative**, not flattering. A delisting-inclusive panel could only move
the spread toward positive.

Constructive bound (UMD r12-1, 356 months, survivor-panel Sharpe -0.11): if a
fraction f of the true short-decile universe is missing and those missing names
average mu per month (<=0), the true spread gains -f*mu per month:

| f missing-among-short-decile | mu missing names | monthly gain | corrected Sharpe |
|---|---|---|---|
| 20% | -2% | +0.40 pp | 0.04 |
| 20% | -5% | +1.00 pp | 0.27 |
| 35% | -2% | +0.70 pp | 0.15 |
| 35% | -5% | +1.75 pp | 0.56 |

Even at the most generous corner (35% of the short-decile universe missing,
every one losing 5%/mo in the holding month), UMD reaches ~0.56 Sharpe
(t ~ 3.0 over 30yr). Under realistic inputs (f ~20-25% of a decile populated by
mid/large caps that already passed the $5/$10M screen, mu -2-3%) it stays near
0.1-0.3 Sharpe. The survivorship correction does not rescue momentum to a
promotable level; the honest reading is "null, bounded short-side upside too
small to reverse the verdict." KLN shares the same short leg and the same
mechanism.

## Verdict / decision

**The free delisting-inclusive panel is a measured dead end, consistent with the
2026-09-06 Phase-2 vetting** (yfinance empty for delisted; no free source):
- Genuine dead-name recovery: 0.00% / 0.00% / 0.03% / 3.95% by decade in the
  wide panel; essentially zero in the panel the factors actually ran on.
- The 900 recycled-ticker string matches are worse than a gap (silent wrong-
  company splicing); only a firstpricedate/lastpricedate-bounded match rule is
  safe, which is what this pass implements.
- Separately measured: naive `ticker`-keyed joins against `prices` produce
  6,865 SEP hits, but the genuine dead-name subset is 167. Any catalog work must
  use the bounded rule.

A real delisting-inclusive panel requires the paid Sharadar SEP/DAILY
subscription (or CRSP/Norgate-class history). That is Zander's purchase call,
NOT a build we can complete free — same decision gate as Phase 2 second half.
Until then this is recorded as a measurement with a documented upper bound, not
an open question. The near-term rigor gains are different and cheaper: (a)
re-run the UMD/KLN sweep as-is but report the coverage numbers above alongside,
(b) optionally try the ~30 genuinely-recovered recent dead names (usually
recent-failure names like LPSN/LEG/FBRX) as a small-sided sandwich test, though
166-167 names at t~2019+ cannot move a 30-year verdict by itself.

## Files

- `experiments/2026-09-13_survivorship-bias.py` — the measurement (recovery by
  decade, point-in-time coverage, constructive UMD bound; `--panel` switch, `--umd`).
- Data: `delisting_reference` (Sharadar SEP TICKERS), `prices`,
  `yfinance_universe_prices`, `storage/reports/eval/umd_equity_momentum_daily.parquet`.