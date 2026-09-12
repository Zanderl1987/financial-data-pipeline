# Strategy Catalog — Published Trading & Investment Strategies

Living catalog of publicly documented trading/investment strategies (white papers, journal
articles, replication code, downloadable factor data) that are candidates for deep
backtesting and forward optimization on this pipeline. Research compiled 2026-09-12 from
web searches; every URL was verified live during that session unless flagged `[verify]`.

**Relationship to the TV campaign:** this catalog covers *published/academic* strategies
and is deliberately separate from the TradingView `tv_strategy_catalog` (1,560 admitted
scripts, storage `tv_strategy_catalog/`). If a published strategy eventually lands as a
Pine port it can be cross-referenced by `strategy_id`, but admission pipelines differ.

## Significance framework (read before picking anything)

Every strategy below is graded against the modern multiple-testing and replication bar.
Rules of thumb we treat as binding:

- **Harvey–Liu–Zhu hurdle:** a long-short factor needs **t > ~3.0** to be credible
  given how many factors have been tried. "…and the Cross-Section of Expected Returns",
  *RFS* 2016, 316 factors catalogued. https://www.nber.org/papers/w20592
  Factor-by-factor t-stats: http://faculty.fuqua.duke.edu/~charvey/Factor-List.xlsx
- **Hou–Xue–Zhang replication:** **65% of 452 anomalies fail |t|<1.96** once microcaps are
  mitigated (NYSE breakpoints, value-weighting); 96% of "trading frictions" anomalies fail.
  "Replicating Anomalies", *RFS* 2020. https://doi.org/10.1093/rfs/hhy131
- **McLean–Pontiff decay:** out-of-sample returns are ~**26% lower**, post-publication
  ~**58% lower**. "Does Academic Research Destroy Stock Return Predictability?", *JF* 2016.
  https://doi.org/10.1111/jofi.12365
- **JKP (contra view):** with Bayesian multiple testing and global data, ~**80% of 153
  factors survive** out-of-sample. Jensen, Kelly & Pedersen, "Is There a Replication Crisis
  in Finance?", *JF* 2023. https://www.jkpfactors.com/

Most reported stats below are **gross of costs** as printed in the papers — the pipeline's
cost model (bps, spread, borrow-fee matrix, ADV participation) is exactly what separates
survivors from ghosts.

### Significance grades
- **A** — t > 3, replicates, still works out-of-sample in recent decades.
- **B** — historically strong (often t ≫ 3 full-sample) but degraded/contested recently,
  or marginal (t 2–3).
- **C** — weak by modern standards, dead after costs, or practically uninvestable.

---

## Tier 1 — Statistically robust strategies (A-graders, priority for deep backtest)

| Strategy | Family | Paper | Public data/code | Reported stats (gross unless noted) | Grade |
|---|---|---|---|---|---|
| Cross-sectional momentum (UMD) | momentum | Jegadeesh & Titman 1993, *JF* — https://www.bauer.uh.edu/rsusmel/phd/jegadeesh-titman93.pdf | French Library `F-F_Momentum_Factor_CSV.zip`; OSAP `Mom12m` | ~1.00%/mo (≈12%/yr) LS, t≈4.3; positive 67–71% of months | A |
| Time-series momentum | trend | Moskowitz, Ooi & Pedersen 2012, *JFE* — https://doi.org/10.1016/j.jfineco.2011.11.003 | AQR TSMOM data (58 markets 1985–2009 + updated monthly) | combined gross Sharpe ≈1.2; every instrument significant (t≈2–5) | A |
| Quality minus junk | quality | Asness, Frazzini & Pedersen 2019, *RAST* — https://doi.org/10.1007/s11142-018-9470-2 | AQR QMJ factors monthly (US 1956–, global 1986–) | 66 bps/mo 4-factor alpha, t=11.20 (US LS); ~45 bps/mo t=5.5 global; + in 23/24 countries | A |
| Gross profitability / RMW | profitability | Novy-Marx 2013 *JFE*; Fama-French 2015 *JFE* — https://doi.org/10.1016/j.jfineco.2012.10.003 | French 5-factor RMW; OSAP `CBOperProf` | RMW 0.25%/mo, t=4.09 (joint sorts 1963–2013) | A |
| Low-beta / low-vol (long side) | low-vol | Frazzini & Pedersen 2014, *JFE* (BAB); Ang-Hodrick-Xing-Zhang 2006 | AQR BAB equity factors (US + 23 int'l); French variance-portfolios | BAB 0.70%/mo t=7.12 (full sample); IVOL LS alpha −1.31%/mo t=−7.0 | B-ish/A long side |
| Timely value (HML-Dev) | value | Asness & Frazzini 2013, *JPM* | AQR Devil-in-HML factors monthly | +305–378 bps/yr 5-factor alpha vs classic HML | B |
| Carry (cross-asset) | carry | Koijen, Moskowitz, Pedersen & Vrugt 2018, *JFE* — https://doi.org/10.1016/j.jfineco.2017.11.002 | bundled in AQR Century xlsx; author pages | predicts returns cross-sectionally and in time series in every asset class; low cross-asset corr | A |
| Basis momentum | commodity cross-asset | Boons & Prado 2019, *JF* — https://ssrn.com/abstract=2587784 | github.com/ddxmy/Basis-Momentum (3rd party, verify) | Sharpe ≈1+ gross; orthogonal to carry and TSMOM | A/B |
| Return seasonalities (same calendar month) | seasonality | Keloharju, Linnainmaa & Nyberg 2016, *JF* — https://doi.org/10.1111/jofi.12398 | constructible from price data; NBER w20815 | same-calendar-month strategy ~13%/yr (1973–2013) | A |
| Intraday momentum (last half-hour) | microstructure | Gao, Han, Li & Zhou 2018, *JFE* — https://doi.org/10.1016/j.jfineco.2018.05.009 | constructible from 1-min bars; SPY 1993–2013 | Sharpe 1.08 vs 0.29 BH; survives costs post-2001 per authors | A/B |
| PEAD / SUE | earnings event | Ball & Brown 1968; Bernard & Thomas 1989 — https://www.jstor.org/stable/2491062 | OSAP `SUE`/`EarningsSurprise`; needs event execution | 6.31%/quarter hedge historically; modern ~2%/60d declining, weak large-cap | B |

**Notes / caveats on Tier 1:**

- **Momentum** is the single most replicated anomaly (HXZ: momentum category survives at
  highest rates) but is crash-prone (2009 momentum crash ≈ −70% in a quarter), reverses
  past ~12–18 months, loses in January historically, and its large-cap spread has narrowed
  since ~2015.
- **TSMOM** is the futures-side workhorse and directly buildable on our `futures` table
  (44 contracts since 1997-10): 12-month lookback, 1-month hold, ex-ante vol target.
- **QMJ/BAB** both need a shortable junk side; BAB's post-2010 decade is weak. Long-side
  low-vol is the investable, alpha-carrying part.
- **Carry** crashes cluster in global recessions (2008/09, 2014–15 CHF/EM); standalone FX
  carry weakened after ~2007.
- **PEAD** is the most "behaviorally real" anomaly but the magnitude has shrunk since
  ~1990 and it needs announcement-time execution; our `earnings_calendar` is ±6 weeks only
  — historical PEAD needs an earnings-data backfill (see Open work in CLAUDE.md).
- **Seasonality:** Heston & Sadka 2008 https://doi.org/10.1016/j.jfineco.2007.02.003
  (same-calendar-month continuation up to 20 annual lags).
- **Vol-managed portfolios** (Moreira & Muir 2017 https://doi.org/10.1111/jofi.12513) are
  *not* graded A: Cederburg et al. 2020 (*JFE*) show vol-timing does not improve
  out-of-sample across 103 strategies. Treat as an overlay, not a standalone alpha.

---

## Tier 2 — Historically strong but degraded / contested (B, C-graders)

| Strategy | Paper | Public data | Reported stats | Now |
|---|---|---|---|---|
| Classic value HML | Fama-French 1993 | French library | historically t>3 | 2007–2020 value crash; weak recent decades; redundant in FF5 |
| Size / SMB | Banz 1981 | French library | ~1.52%/mo (1936–75) | dead since ~1983 (t<1.5); uninvestable microcap concentration |
| Investment / CMA | Fama-French 2015; Cooper-Gulen-Schill 2008 | French library; OSAP `AssetGrowth` | CMA 0.14%/mo t=2.71 | big-stock leg t≈1; HXZ robust but magnitude shrinks ex-microcap |
| Net/composite share issuance | Pontiff-Woodgate 2008; Daniel-Titman 2006 | French net-share portfolios; OSAP | t often 4–5 post-1970 | overlapping with investment/asset-growth |
| Short-term reversal | Jegadeesh 1990; Lehmann 1990 | French ST Rev factor | ~2%/mo extreme-decile historical | **gross-to-net edge gone**; bid-ask bounce; FF3 alpha t=1.37 modern |
| Accruals | Sloan 1996 — https://www.cuhk.edu.hk/acy2/workshop/June2009Wasley/1996TAR).pdf | OSAP `Accruals` | +10.4%/yr t=4.71, + all 19 yrs | **dead since ~2000** (Green-Hand-Soliman 2011) |
| 52-week-high momentum | George & Hwang 2004 | constructible from price | 0.45%/mo t≈2.0 | t<3; overlaps J-T momentum |
| FX carry | Menkhoff et al 2012 http://www.jfinan.… | currencyfactors.com; LRV data https://web.mit.edu/adrienv/www/Data.html | single global FX-vol factor explains >90% of carry portfolio spread | strongest 1983–2007, weaker since |
| Trend-following (CTA) | Hurst, Ooi, Pedersen 2017 "A Century of Evidence…" — https://www.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/AQR-JPM-Fall-2017.pdf | AQR TSMOM updated factors | net Sharpe ≈0.5–0.6 over 135y, 67 markets | industry net Sharpe floor ~0.4; flat 2011–2020, strong 2020/2022 |
| Turn-of-month / January / Halloween / holiday | McConnell-Xu 2008; Bouman-Jacobsen 2002; Haug-Hirschey 2006; Tsiakas 2010 | constructible | TOM 0.15%/day VW 1926–2005; Halloween 36/37 markets 1970–98 | **contested**: Plastun et al. 2019 (https://doi.org/10.1016/j.qref.2019.04.008) find all calendar anomalies gone in DJIA since 1980s |
| Same-weekday momentum | Da & Zhang 2024 — https://academicweb.nd.edu/~zda/Same_Weekday_Momentum.pdf | constructible | = 20–60% of total momentum | survives size/weighting; institutional-driver |
| Overnight vs intraday | Lou, Polk & Skouras 2019, *JFE* — https://doi.org/10.1016/j.jfineco.2019.03.011 | constructible | night-minus-day 74/51 bps per news class | **LS killed by costs** (~25% of capital in fees over 20y per QuantConnect) |
| Weekend/Monday in speculative stocks | Birru 2018, *JFE* https://doi.org/10.1016/j.jfineco.2018.06.008 | constructible | Monday carries ≥100% of LR/STR returns of speculative names | index-level weekend effect dead (Plastun) |
| Put-call (buyer-initiated) | Pan & Poteshman 2006, *RFS* — https://web.mit.edu/~junpan/www/volume.pdf | needs order-level options volume | >40 bps/day, >1%/wk | large OOS erosion; capacity-limited |
| Short vol / straddle underperformance | Coval-Shumway 2001; Lai 2009 | options history | zero-beta ATM straddles ≈ −3%/wk | **not capturable after costs** (Lai) |
| VIX term-structure slope | Johnson 2017, *JFQA* — https://www.travislakejohnson.com/pdfs/Johnson%20VIXTS%202017%20%28JFQA%29.pdf | VIX futures/forward curve | SLOPE predicts var-swap/straddle returns, t≈−7 to −3 | gross Sharpe ~1.5; use as timing overlay |
| Short interest (informed shorts) | Boehmer-Jones-Zhang 2008 — https://doi.org/10.1111/j.1540-6261.2008.01324.x | our `short_interest` table (watchlist-only) | heavily-shorted −1.16%/20d (15.6%/yr); institutional shorts −1.43%/mo | short fees material; FINRA NMS feed still blocked |
| Opportunistic insider purchases | Cohen, Malloy & Pomorski 2012 — https://doi.org/10.1111/j.1540-6261.2012.01740.x | Alpha Vantage/finviz insider tables | 82 bps/mo VW abnormal, routine≈0 | aggregate raw dead; *opportunistic* aggregate alive (+0.57% next mo per 1-SD, Huang-Lin-Zheng 2022 SSRN 4294492) |
| Carry × TSMOM blend | Molyboga-Qian-He 2019 (SSRN 3470864); Baz et al 2015 CME (https://www.cmegroup.com/education/files/dissecting-investment-strategies-in-the-cross-section-and-time-series.pdf) | AQR data | +0.17 net Sharpe over TSMOM; lower max DD | practical futures blueprint |
| Currency momentum | Menkhoff et al 2012 category | currencyfactors.com | standard momentum factor in FX portfolios | robust, but FX data quality caps history |

---

## Tier 3 — Weak / dead / not investable (C)

- **Emoji/paid none** — see table above; category exemplars: size (dead), accruals (dead
  ~2000), short-term reversal (killed by costs), straddle-short (killed by costs),
  52-week-high (t≈2, overlapping), retail options flow (Eaton et al 2026, *JFE* — small-cap
  concentrated, data unavailable), RPIX (practitioner indicator, **no academic validation** —
  do not cite).
- General rule from HXZ: **trading-frictions anomalies (106 tested) fail replication
  96% of the time.** Anything whose edge is "microstructure/illiquidity" and net of our cost
  model is a low prior.

---

## Tier 1 — Data & code sources (feed these into the pipeline)

Ranked by usefulness for our own backtests. All free/no key unless noted.

| Source | URL | What you get | Automation | FATAL caveats |
|---|---|---|---|---|
| Open Source Asset Pricing (OSAP) | https://www.openassetpricing.com/data/ | ~212 replicated anomaly predictors + **monthly & daily long-short portfolio returns** + 209 firm characteristics; PIT-aware | `pip install openassetpricing`; Google-Drive CSV dumps | data through Dec 2024; option-IV predictors only through Dec 2022; microcap-heavy — apply price/liquidity filters |
| global-q.org (Hou-Xue-Zhang) | https://global-q.org/testingportfolios.html | q-factors + **201 replicated anomaly portfolios** (deciles, size-sorts, NYSE bps), 1967–2025 | predictable CSV zip URL pattern (`<cat>_<freq>_<year>.zip`) | returns in **percent**; July 2026 release switched to CRSP CIZ data (construction changed) — tag your vintage |
| JKP Global Factor Data | https://www.jkpfactors.com/ | 153 factors × 93 countries, monthly LS tercile returns, Parquet | site downloads; `github.com/bkelly-lab/jkp-data` (MIT) | the "factors survive" paper — don't cite as "many factors dead"; thin international early years |
| Kenneth French Data Library | https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html | FF3/5/6 factors, momentum, ST Rev, 25/100 BM-size ports, single sorts | stable `ftp/` CSV zip URLs; `pandas_datareader` "famafrench" | returns in **percent**; ASCII preamble row count varies; CRSP FIZ→CIZ re-issue at Jan 2025 changes vintages |
| AQR Data Sets | https://www.aqr.com/Insights/Datasets | TSMOM, BAB, QMJ, HML-Dev, Value&Momentum Everywhere, Century of Factor Premia, Commodities-for-the-Long-Run, Credit, ESG — xlsx | predictable media URLs; scraper in `stefan-jansen/machine-learning-for-trading` `data/factors/aqr_download.py` | **no separate Carry dataset** (bundled in Century); "Low risk" = BAB only; per-file header row offsets |
| Tidy Finance factor library | https://huggingface.co/datasets/tidy-finance/factor-library | 50 factors × **841k construction specs** (breakpoint/EW-VW/lag variants) Parquet, 1960–2024 US | `py-tidyfinance download_data()` | specification-sensitivity sweeps, not canonical set |
| Andrew Y. Chen data | https://sites.google.com/site/chenandrewy/Data | 29k mined accounting signals; 80k past-return strategies; 18k ratio portfolios | Drive/Dropbox dumps | all data-mined — use ONLY for deflated-Sharpe/Harvey-haircut sanity checks |
| 101 Formulaic Alphas | https://arxiv.org/abs/1601.00991 | all 101 formula + implementions (github.com/lvlh2/alpha101, OctopusTakopi/toraniko-alpha101) | trivially scriptable on our daily OHLCV+volume | OCR/typo variants across copies; NaN warm-up windows; ~day-level holdouts |
| Quantpedia | https://quantpedia.com/ | ~70 free strategy reviews + paper links; 900+ behind paywall | no API (scrape-only) | figures mostly author-reported; use as idea discovery |
| HLZ factor list | http://faculty.fuqua.duke.edu/~charvey/Factor-List.xlsx | 316 factors + published t-stats | download once, host locally (Duke blocks bots) | catalog, not returns |

**Recommended stack:** OSAP (primary anomaly engine) + global-q (replication baseline) + JKP
(global OOS) + French (baseline model) + AQR Century/TSMOM (cross-asset premia). Run IC via
`evaluation/ic.py`, significance through the 3-tier battery + deflated Sharpe, and anything
data-mined through the Harvey-Liu haircut before trusting.

---

## Backtest priority (what fits this pipeline, first)

Our data (see CLAUDE.md / docs/PIPELINE_CATALOG.md): 44 futures 1997-10+, 2,285 Russell-3000-ish
equities 1962+, deep `market_history` 1927+, forex 1999+, options 2023-12+, daily bars (some
intraday via schwab, short retention), fundamentals ~1990s+, short_interest/borrow fees.

1. **TSMOM on our 44 futures (1997+)** — A-grade, fully PIT-constructible, uses existing
   `futures` table + vol targeting + our cost model. DONE (2026-09-12): Sharpe 0.23
   roll-masked (0.15 raw), decade 2000s 0.27 / 2010s 0.38 / 2020s 0.66; monthly corr
   0.41–0.53 vs AQR official factor (2010s+ in line, 2000s capped by our 4 FX/4 rates
   universe; AQR trades 13/13). Writeup: `experiments/2026-09-12_tsmom-futures.md`.
   NEXT: full forward-optimization loop (walk-forward + CPCV) or hand off to the next
   priority factor — pending back-adjusted futures availability.
2. **Carry + Carry×TSMOM** — from AQR Century xlsx + futures/FX construction.
3. **Cross-sectional momentum / reversal / low-vol on `yfinance_universe_prices`** — OSAP
   patterns; intraday momentum if we accumulate schwab 1-min bars.
4. **OSAP / global-q long-short monthly portfolios** — pull the CSV dumps and run through
   `evaluation/ic.py` + deflated Sharpe as a replication sanity check before trading any.
5. **101 Alphas on the equities universe** — fast win, needs volume; audit α's daily turnover.
6. **Return seasonalities (KLN)** — constructible from price matrix; ~13%/yr gross.
7. **Short-interest / borrow-fee cross-section** — data exists but coverage is watchlist-only;
   FINRA NMS restore is the unblocker.
8. **VIX term-structure slope overlay** — needs VIX futures history (`[verify]` available
   data path); currently `synthetic_options`/`options_history` only go to 2023.
9. **PEAD** — blocked on historical earnings (see CLAUDE.md open work) until a real
   earnings-dates backfill lands.

Everything netted with: bps + spread + borrow-fee matrix + ADV participation (adv_participation
costs in `backtest.py`), vol-targeted, walk-forward OOS, deflated-Sharpe multiple-testing gate.