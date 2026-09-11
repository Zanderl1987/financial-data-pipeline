/**
 * Holdings Download Link (Firestore-powered)
 * ----------------------------------------
 * Renders a link that downloads the latest holdings as CSV.
 */

(function () {
const CSV_ICON = '<i class="fas fa-file-csv"></i>';

  function waitForAmplifyFirestore(timeout = 10000) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const tick = () => {
        if (window.amplifyFirestore) return resolve(window.amplifyFirestore);
        if (Date.now() - start > timeout) return reject(new Error("Amplify Firestore not available"));
        setTimeout(tick, 50);
      };
      tick();
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initHoldingsDownloadLinks();
  });
  document.addEventListener("amplify:firestore:ready", () => {
    initHoldingsDownloadLinks();
  });

  async function initHoldingsDownloadLinks() {
    const widgets = document.querySelectorAll(".amplify-holdings-download");
    for (const el of widgets) {
      try {
        await renderHoldingsDownload(el);
      } catch (err) {
        console.error("❌ Holdings download init error:", err);
        el.innerHTML = "<p>Unable to load download link.</p>";
      }
    }
  }

  async function renderHoldingsDownload(container) {
    await waitForAmplifyFirestore();

    const ticker = sanitizeTicker(container.dataset.fund || "");
    if (!ticker) {
      container.innerHTML = "<p>Missing fund ticker.</p>";
      return;
    }

    const linkText = typeof container.dataset.linkText === "string" && container.dataset.linkText.trim() !== ""
      ? container.dataset.linkText.trim()
      : "Download Holdings (CSV)";

    const swapMode = container.dataset.swapMode === "true";

    container.innerHTML = `
      <a class="amplify-holdings-download__link" href="#" data-format="csv">
        <span class="amplify-holdings-download__icon" aria-hidden="true">${CSV_ICON}</span>
        <span class="amplify-holdings-download__text">${escapeHtml(linkText)}</span>
      </a>
    `;

    const link = container.querySelector(".amplify-holdings-download__link");
    if (!link) return;

    link.addEventListener("click", async (e) => {
      e.preventDefault();
      link.classList.add("is-loading");

      try {
        const latestAsOfId = await getLatestAsOfId(ticker, "holdings");
        if (!latestAsOfId) {
          throw new Error("No holdings data available");
        }

        // Swap mode reads swap_holdings — only that doc carries
        // Market_Value_Notional and UnrealizedGainLoss (Swap MTM).
        // The as-of date is still resolved from "holdings" (the hosted
        // Firestore script only supports known collection names).
        let doc = swapMode
          ? await window.amplifyFirestore.getNestedDoc(["funds", ticker, "swap_holdings", latestAsOfId])
          : null;
        if (!doc || !Array.isArray(doc.holdings) || doc.holdings.length === 0) {
          doc = await window.amplifyFirestore.getNestedDoc(["funds", ticker, "holdings", latestAsOfId]);
        }
        const holdings = doc?.holdings;
        if (!Array.isArray(holdings) || holdings.length === 0) {
          throw new Error("No holdings data available");
        }

        const fundMeta = await window.amplifyFirestore.getNestedDoc(["funds", ticker, "fund_metadata", "overview"]);
        const fundName = fundMeta?.DisplayName || "";

        const csvText = swapMode
          ? holdingsToSwapCsv({
              holdings,
              asOfId: latestAsOfId,
              fundName,
              fundTicker: ticker,
            })
          : holdingsToCsv({
              holdings,
              asOfId: latestAsOfId,
              fundName,
              fundTicker: ticker,
            });
        const filename = `Amplify_${ticker}_Holdings_${latestAsOfId}.csv`;
        triggerDownload(csvText, filename, "text/csv;charset=utf-8;");
      } catch (err) {
        console.error("❌ Holdings CSV download error:", err);
        alert("Unable to download holdings right now.");
      } finally {
        link.classList.remove("is-loading");
      }
    });
  }

  async function getLatestAsOfId(ticker, subcollection) {
    const t = sanitizeTicker(ticker);
    if (!t) return null;

    // Prefer getLatestAsOfId if available.
    if (window.amplifyFirestore && typeof window.amplifyFirestore.getLatestAsOfId === "function") {
      const id = await window.amplifyFirestore.getLatestAsOfId(t, subcollection);
      return normalizeAsOfId(id);
    }

    // Fallback to getLatestAsOfDate, normalized into YYYY-MM-DD.
    if (window.amplifyFirestore && typeof window.amplifyFirestore.getLatestAsOfDate === "function") {
      const dateVal = await window.amplifyFirestore.getLatestAsOfDate(t, subcollection);
      const normalized = normalizeAsOfId(dateVal);
      return normalized;
    }

    return null;
  }

  function normalizeAsOfId(val) {
    if (val == null) return null;
    const raw = String(val).trim();
    if (!raw) return null;

    // Already YYYY-MM-DD
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;

    // MM/DD/YYYY -> YYYY-MM-DD
    const mdy = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
    if (mdy) {
      const mm = String(mdy[1]).padStart(2, "0");
      const dd = String(mdy[2]).padStart(2, "0");
      const yyyy = mdy[3];
      return `${yyyy}-${mm}-${dd}`;
    }

    return raw;
  }

  function holdingsToCsv(holdings) {
    const asOfId = holdings?.asOfId || "";
    const fundName = holdings?.fundName || "";
    const fundTicker = holdings?.fundTicker || "";
    const rows = Array.isArray(holdings?.holdings) ? holdings.holdings : holdings;

    // Sort by weight descending
    const sortedRows = [...rows].sort((a, b) => {
      const weightA = toPercentValue(getHoldingWeightRaw(a)) ?? 0;
      const weightB = toPercentValue(getHoldingWeightRaw(b)) ?? 0;
      return weightB - weightA;
    });

    const headers = [
      "As of Date",
      "Fund Name",
      "Fund Ticker",
      "Holding Name",
      "Holding Ticker",
      "Market Value (%)",
      "CUSIP",
      "Shares",
      "Market Value ($)",
    ];

    const lines = [];
    lines.push(headers.map(csvEscape).join(","));

    for (const h of sortedRows) {
      const holdingName = h?.SecurityName || h?.StockTicker || h?.CUSIP || "";
      const holdingTicker = sanitizeTicker(h?.StockTicker || "");
      const cusip = h?.CUSIP || "";
      const shares = h?.Shares ?? "";
      const marketValue = h?.MarketValue ?? "";
      const weightPct = toPercentValue(getHoldingWeightRaw(h));
      const weightDisplay = weightPct == null ? "" : `${Number(weightPct).toFixed(2)}%`;

      const row = [
        asOfId,
        fundName,
        fundTicker,
        holdingName,
        holdingTicker,
        weightDisplay,
        cusip,
        shares,
        marketValue,
      ];

      lines.push(row.map(csvEscape).join(","));
    }

    // Add disclaimer footer
    lines.push(""); // Blank row
    lines.push(""); // Blank row
    lines.push(csvEscape("Fund holdings are subject to change at any time and should not be considered recommendations to buy or sell any security."));

    return lines.join("\r\n");
  }

  function holdingsToSwapCsv(holdings) {
    const rows = Array.isArray(holdings?.holdings) ? holdings.holdings : holdings;

    // Sort by weight descending
    const sortedRows = [...rows].sort((a, b) => {
      const weightA = toPercentValue(getHoldingWeightRaw(a)) ?? 0;
      const weightB = toPercentValue(getHoldingWeightRaw(b)) ?? 0;
      return weightB - weightA;
    });

    const headers = [
      "Name",
      "Ticker",
      "CUSIP",
      "Shares",
      "Price",
      "Market Value/Notional Exposure",
      "Swap MTM",
      "Weightings",
    ];

    const lines = [];
    lines.push(headers.map(csvEscape).join(","));

    for (const h of sortedRows) {
      const name = h?.SecurityName || "";
      const ticker = sanitizeTicker(h?.StockTicker || "");
      const cusip = h?.CUSIP || h?.Cusip || "";
      const shares = h?.Shares ?? "";
      
      // Prefer the ingested Price field; fall back to deriving it from
      // (Market_Value_Notional or MarketValue) / Shares.
      let price = "";
      if (h?.Price != null && Number.isFinite(Number(h.Price))) {
        price = Number(h.Price);
      } else {
        const marketVal = h?.Market_Value_Notional ?? h?.MarketValue;
        if (marketVal != null && h?.Shares != null && h.Shares !== 0) {
          const priceNum = Number(marketVal) / Number(h.Shares);
          if (Number.isFinite(priceNum)) {
            price = priceNum;
          }
        }
      }
      
      // Use Market_Value_Notional if available, fallback to MarketValue
      const marketValueNotional = h?.Market_Value_Notional ?? h?.MarketValue ?? "";
      
      // Use UnrealizedGainLoss for Swap MTM
      const swapMtm = h?.UnrealizedGainLoss ?? "";
      
      // Get weight percentage
      const weightPct = toPercentValue(getHoldingWeightRaw(h));
      const weightDisplay = weightPct == null ? "" : `${Number(weightPct).toFixed(2)}%`;

      const row = [
        name,
        ticker,
        cusip,
        shares,
        price,
        marketValueNotional,
        swapMtm,
        weightDisplay,
      ];

      lines.push(row.map(csvEscape).join(","));
    }

    // Add disclaimer footer
    lines.push(""); // Blank row
    lines.push(""); // Blank row
    lines.push(csvEscape("Fund holdings are subject to change at any time and should not be considered recommendations to buy or sell any security."));
    lines.push(csvEscape("Market Value / Notional Exposure: Market value is for non-swap instruments. Notional exposure represents a swap's underlying security exposure."));
    lines.push(csvEscape("Swap MTM: Represents a swap's mark-to-market (MTM) since last reset date."));
    lines.push(csvEscape("Cash & Other: Includes an adjustment ensuring the fund's net asset value accurately reflects investments. The fund's cash holding can be estimated by subtracting the value of its equity and swap positions from its total net assets."));

    return lines.join("\r\n");
  }

  function getHoldingWeightRaw(holding) {
    if (!holding || typeof holding !== "object") return null;
    const candidates = [
      holding.weight,
      holding.Weight,
      holding.Weighting,
      holding.Weightings,
      holding.weighting,
      holding.weightings,
    ];

    for (const c of candidates) {
      if (c === null || c === undefined) continue;
      if (typeof c === "string" && c.trim() === "") continue;
      return c;
    }
    return null;
  }

  function toPercentValue(raw) {
    if (raw === null || raw === undefined) return null;

    if (typeof raw === "string") {
      const trimmed = raw.trim();
      if (!trimmed) return null;

      const hasPercent = trimmed.endsWith("%");
      const withoutPercent = hasPercent ? trimmed.slice(0, -1).trim() : trimmed;
      const parsed = Number.parseFloat(withoutPercent);
      if (!Number.isFinite(parsed)) return null;

      if (hasPercent) return parsed;
      if (parsed > 0 && parsed < 1) return parsed * 100;
      return parsed;
    }

    const num = Number(raw);
    if (!Number.isFinite(num)) return null;
    if (num > 0 && num < 1) return num * 100;
    return num;
  }

  function csvEscape(value) {
    if (value === null || value === undefined) return "";
    
    // Handle NaN and Infinity
    if (typeof value === "number" && !Number.isFinite(value)) return "";

    let s;
    if (typeof value === "object") {
      // Preserve raw JSON for nested values
      try {
        s = JSON.stringify(value);
      } catch {
        s = String(value);
      }
    } else {
      s = String(value);
    }

    const needsQuotes = /[\r\n",]/.test(s);
    const escaped = s.replace(/"/g, '""');
    return needsQuotes ? `"${escaped}"` : escaped;
  }

  function triggerDownload(text, filename, mimeType) {
    const blob = new Blob([text], { type: mimeType || "text/plain;charset=utf-8;" });
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();

    // Cleanup
    setTimeout(() => URL.revokeObjectURL(url), 250);
  }

  function sanitizeTicker(ticker) {
    if (!ticker) return "";
    return String(ticker)
      .trim()
      .toUpperCase()
      .replace(/[^A-Z0-9_\-]/g, "");
  }

  function escapeHtml(input) {
    return String(input)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
})();
