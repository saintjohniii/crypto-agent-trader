const money = (n, digits = 2) =>
  Number(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

function setPnL(el, value) {
  el.classList.remove("up", "down");
  if (value > 0) el.classList.add("up");
  if (value < 0) el.classList.add("down");
}

function priceFmt(v) {
  if (v >= 10000) return money(v, 0);
  if (v >= 100) return money(v, 2);
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 4 });
}

const CHART_W = 320, CHART_H = 120, CHART_PAD = 6;

function chartSvg(series, position) {
  const W = CHART_W, H = CHART_H, PAD = CHART_PAD;
  const closes = series.map((p) => p.c);
  let lo = Math.min(...closes);
  let hi = Math.max(...closes);
  // Widen scale so entry/SL/TP lines stay visible when nearby
  if (position) {
    lo = Math.min(lo, position.stop_loss);
    hi = Math.max(hi, position.take_profit);
  }
  if (hi === lo) { hi += 1; lo -= 1; }
  const x = (i) => PAD + (i / (closes.length - 1)) * (W - 2 * PAD);
  const y = (v) => PAD + (1 - (v - lo) / (hi - lo)) * (H - 2 * PAD);

  const coords = series.map((p, i) => ({
    x: Number(x(i).toFixed(1)),
    y: Number(y(p.c).toFixed(1)),
    c: p.c,
    t: p.t,
  }));
  const pts = coords.map((p) => `${p.x},${p.y}`).join(" ");
  const first = closes[0], last = closes[closes.length - 1];
  const up = last >= first;
  const color = up ? "var(--good)" : "var(--bad)";
  const areaPts = `${PAD},${H - PAD} ${pts} ${W - PAD},${H - PAD}`;

  const hline = (v, cls, label) => {
    if (v == null || v < lo || v > hi) return "";
    const yy = y(v).toFixed(1);
    return `<line x1="${PAD}" y1="${yy}" x2="${W - PAD}" y2="${yy}" class="chart-line ${cls}" />
            <text x="${W - PAD}" y="${Math.max(10, yy - 3)}" text-anchor="end" class="chart-label ${cls}">${label} ${priceFmt(v)}</text>`;
  };

  let overlays = "";
  if (position) {
    overlays =
      hline(position.entry, "entry", "entry") +
      hline(position.stop_loss, "sl", "SL") +
      hline(position.take_profit, "tp", "TP");
  }

  return `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-pts='${JSON.stringify(coords)}'>
      <polygon points="${areaPts}" fill="${color}" opacity="0.08"></polygon>
      <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6"></polyline>
      <circle cx="${x(closes.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="2.6" fill="${color}"></circle>
      ${overlays}
      <g class="xhair" style="display:none">
        <line class="xhair-line" y1="${PAD}" y2="${H - PAD}" x1="0" x2="0"></line>
        <circle class="xhair-dot" r="3" fill="${color}"></circle>
      </g>
    </svg>`;
}

function attachChartHover(card, series, position) {
  const svg = card.querySelector("svg");
  const tip = card.querySelector(".chart-tip");
  if (!svg || !tip) return;
  const coords = JSON.parse(svg.dataset.pts);
  const xhair = svg.querySelector(".xhair");
  const xline = svg.querySelector(".xhair-line");
  const xdot = svg.querySelector(".xhair-dot");
  const first = coords[0].c;

  const show = (clientX) => {
    const rect = svg.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    const i = Math.round(frac * (coords.length - 1));
    const p = coords[i];

    xhair.style.display = "";
    xline.setAttribute("x1", p.x);
    xline.setAttribute("x2", p.x);
    xdot.setAttribute("cx", p.x);
    xdot.setAttribute("cy", p.y);

    const when = new Date(p.t > 1e12 ? p.t : p.t * 1000);
    const timeStr = when.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    const chg = ((p.c - first) / first) * 100;
    let extra = "";
    if (position) {
      const pnl = (p.c - position.entry) * position.quantity * (position.leverage || 1);
      extra = `<div class="${pnl >= 0 ? "up" : "down"}">uPnL ${pnl >= 0 ? "+" : ""}R${money(pnl)}</div>`;
    }
    tip.innerHTML = `
      <div class="tip-time">${timeStr}</div>
      <div class="tip-price">R${priceFmt(p.c)}</div>
      <div class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "+" : ""}${money(chg)}% in window</div>
      ${extra}`;
    tip.hidden = false;

    // Position tooltip near the cursor, flipping side near the right edge
    const cardRect = card.getBoundingClientRect();
    const px = clientX - cardRect.left;
    tip.style.left = px > cardRect.width - 140 ? `${px - tip.offsetWidth - 12}px` : `${px + 12}px`;
    tip.style.top = `${svg.getBoundingClientRect().top - cardRect.top + 8}px`;
  };

  const hide = () => {
    xhair.style.display = "none";
    tip.hidden = true;
  };

  svg.addEventListener("mousemove", (e) => show(e.clientX));
  svg.addEventListener("mouseleave", hide);
  svg.addEventListener("touchstart", (e) => show(e.touches[0].clientX), { passive: true });
  svg.addEventListener("touchmove", (e) => show(e.touches[0].clientX), { passive: true });
  svg.addEventListener("touchend", hide);
}

function renderCharts(signals, positions) {
  const root = document.getElementById("charts");
  const withData = (signals || []).filter((s) => s.series?.length > 1);
  if (!withData.length) {
    root.innerHTML = `<p class="empty">No chart data yet</p>`;
    return;
  }
  const posBySym = Object.fromEntries((positions || []).map((p) => [p.symbol, p]));
  root.innerHTML = withData
    .map((s) => {
      const closes = s.series.map((p) => p.c);
      const first = closes[0], last = closes[closes.length - 1];
      const chg = ((last - first) / first) * 100;
      const pos = posBySym[s.symbol];
      return `
      <div class="chart-card ${pos ? "held" : ""}">
        <div class="chart-head">
          <span class="sym">${s.symbol}${s.watch ? ` <span class="pill muted">watch</span>` : ""}</span>
          <span class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "+" : ""}${money(chg)}%</span>
        </div>
        <div class="chart-sub">R${priceFmt(last)}${pos ? ` · in position (entry R${priceFmt(pos.entry)})` : ""}</div>
        ${chartSvg(s.series, pos)}
        <div class="chart-tip" hidden></div>
      </div>`;
    })
    .join("");
  root.querySelectorAll(".chart-card").forEach((card, i) => {
    attachChartHover(card, withData[i].series, posBySym[withData[i].symbol]);
  });
}

function renderStats(stats, backtest) {
  const root = document.getElementById("stats");
  const badge = document.getElementById("stats-badge");
  if (!stats) {
    root.innerHTML = `<p class="empty">No stats yet</p>`;
    return;
  }
  badge.textContent = `${stats.closed_trades} closed`;
  badge.className = "pill muted";

  const pf = stats.profit_factor_display ?? "—";
  const pairRows = (stats.per_pair || [])
    .map(
      (p) => `<tr>
        <td>${p.symbol}</td>
        <td>${p.trades}</td>
        <td>${money(p.win_rate, 1)}%</td>
        <td>${p.profit_factor_display ?? "—"}</td>
        <td class="${p.net_pnl >= 0 ? "up" : "down"}">${p.net_pnl >= 0 ? "+" : ""}R${money(p.net_pnl)}</td>
      </tr>`
    )
    .join("");

  let btHtml = "";
  if (backtest) {
    const pass = backtest.pass_bar?.all;
    btHtml = `
      <div class="stats-backtest">
        <p class="sub">Last backtest · ${backtest.days_approx}d · ${backtest.closed_trades} trades ·
          PF ${backtest.profit_factor_display} · DD ${money(backtest.max_drawdown_pct, 1)}% ·
          PnL R${money(backtest.net_pnl)} ·
          <span class="${pass ? "up" : "down"}">${pass ? "PASS BAR" : "FAIL BAR"}</span>
        </p>
      </div>`;
  }

  root.innerHTML = `
    <div class="stats-grid">
      <div><p class="label">Win rate</p><p class="mega small">${money(stats.win_rate, 1)}%</p></div>
      <div><p class="label">Profit factor</p><p class="mega small">${pf}</p></div>
      <div><p class="label">Max DD</p><p class="mega small">${money(stats.max_drawdown_pct, 1)}%</p></div>
      <div><p class="label">Net PnL</p><p class="mega small ${stats.net_pnl >= 0 ? "up" : "down"}">${stats.net_pnl >= 0 ? "+" : ""}R${money(stats.net_pnl)}</p></div>
    </div>
    ${btHtml}
    ${
      pairRows
        ? `<table class="table" style="margin-top:0.75rem">
            <thead><tr><th>Pair</th><th>Trades</th><th>WR</th><th>PF</th><th>PnL</th></tr></thead>
            <tbody>${pairRows}</tbody>
          </table>`
        : `<p class="empty" style="margin-top:0.75rem">No closed paper trades yet — metrics fill after exits</p>`
    }`;
}

function renderSignals(signals) {
  const root = document.getElementById("signals");
  if (!signals?.length) {
    root.innerHTML = `<p class="empty">No signals</p>`;
    return;
  }
  root.innerHTML = signals
    .map((s) => {
      const tag = (s.signal || "HOLD").toLowerCase();
      const regime = (s.regime || "—").replace("TREND_", "");
      return `
      <article class="signal">
        <div>
          <div class="sym">${s.symbol}${s.watch ? ` <span class="pill muted">watch</span>` : ""}</div>
          <div class="price">${s.price == null ? "—" : "R" + money(s.price)}</div>
          <div class="price regime">${regime}</div>
        </div>
        <div>
          <div class="reason">${s.reason || ""}</div>
          <div class="price" style="margin-top:0.45rem">RSI ${s.rsi ?? "—"} · spread ${s.spread_bps ?? "—"} bps · news ${s.news == null ? "—" : (s.news >= 0 ? "+" : "") + s.news} · EMA ${s.ema_fast ?? "—"} / ${s.ema_slow ?? "—"}</div>
        </div>
        <span class="tag ${tag}">${s.signal}</span>
      </article>`;
    })
    .join("");
}

function renderPositions(positions) {
  const root = document.getElementById("positions");
  if (!positions?.length) {
    root.innerHTML = `<p class="empty">No open positions</p>`;
    return;
  }
  root.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>Symbol</th><th>Entry</th><th>Exit fill</th><th>Lev</th><th>Net uPnL</th><th>SL / TP</th>
        </tr>
      </thead>
      <tbody>
        ${positions
          .map(
            (p) => `
          <tr>
            <td>${p.symbol}<br/><span class="price">${p.quantity}</span></td>
            <td>R${money(p.entry)}</td>
            <td>R${money(p.exit_fill)}<br/><span class="price">mark R${money(p.mark)}</span></td>
            <td>${p.leverage}x</td>
            <td class="${p.unrealized_pnl >= 0 ? "up" : "down"}">${p.unrealized_pnl >= 0 ? "+" : ""}${money(p.unrealized_pnl)}</td>
            <td>${money(p.stop_loss)} / ${money(p.take_profit)}</td>
          </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderTrades(trades) {
  const root = document.getElementById("trades");
  if (!trades?.length) {
    root.innerHTML = `<p class="empty">No trades yet</p>`;
    return;
  }
  root.innerHTML = `
    <table class="table">
      <thead>
        <tr><th>When</th><th>Action</th><th>Symbol</th><th>Fill</th><th>Costs</th><th>PnL</th><th>Reason</th></tr>
      </thead>
      <tbody>
        ${trades
          .map((t) => {
            const when = t.ts ? new Date(t.ts).toLocaleString() : "—";
            const pnl = t.pnl == null ? "—" : `${t.pnl >= 0 ? "+" : ""}${money(t.pnl)}`;
            const costs = t.total_cost ?? t.fee;
            return `<tr>
              <td>${when}</td>
              <td>${t.action || "—"}</td>
              <td>${t.symbol || "—"}</td>
              <td>${t.price == null ? "—" : "R" + money(t.price)}</td>
              <td>${costs == null ? "—" : "R" + money(costs)}${t.fee == null ? "" : `<br/><span class="price">fee R${money(t.fee)}</span>`}</td>
              <td class="${t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : ""}">${pnl}</td>
              <td>${t.reason || "—"}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

function fmtQty(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  if (Math.abs(x) >= 1) return money(x, 4);
  return x.toLocaleString(undefined, { maximumFractionDigits: 8 });
}

function renderWallet(wallet) {
  const root = document.getElementById("wallet");
  const badge = document.getElementById("wallet-status");
  if (!wallet) {
    root.innerHTML = `<p class="empty">Wallet unavailable</p>`;
    badge.textContent = "offline";
    badge.className = "pill muted";
    return;
  }
  if (!wallet.connected) {
    root.innerHTML = `<p class="empty">${wallet.error || "Not connected"}</p>`;
    badge.textContent = "no key";
    badge.className = "pill bad";
    return;
  }
  badge.textContent = "live";
  badge.className = "pill good";

  if (!wallet.holdings?.length) {
    root.innerHTML = `<p class="empty">No balances</p>`;
    return;
  }

  const totalNote = wallet.zar_complete
    ? `Est. total R${money(wallet.total_zar)}`
    : `Partial ZAR total R${money(wallet.total_zar)} (some assets lack ZAR pair)`;

  root.innerHTML = `
    <p class="sub" style="margin:0 0 0.75rem">${totalNote}</p>
    <table class="table">
      <thead>
        <tr><th>Asset</th><th>Balance</th><th>Reserved</th><th>Value</th><th>Pair</th></tr>
      </thead>
      <tbody>
        ${wallet.holdings
          .map((h) => {
            let value = "—";
            if (h.value_zar != null) value = `R${money(h.value_zar)}`;
            else if (h.value_note) value = h.value_note;
            return `<tr>
              <td><strong>${h.asset}</strong></td>
              <td>${fmtQty(h.balance)}</td>
              <td>${fmtQty(h.reserved)}</td>
              <td>${value}</td>
              <td>${h.pair || "—"}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

function render(data) {
  if (data.error) {
    document.getElementById("status").textContent = "error";
    document.getElementById("status").className = "pill bad";
    return;
  }

  document.getElementById("mode").textContent = data.mode || "PAPER";
  const eyebrow = document.querySelector(".eyebrow");
  if (eyebrow && data.exchange) {
    eyebrow.textContent = `Paper · ${String(data.exchange).toUpperCase()} · Hybrid`;
  }
  const status = document.getElementById("status");
  if (data.halted) {
    status.textContent = "halted";
    status.className = "pill bad";
  } else {
    status.textContent = "active";
    status.className = "pill good";
  }

  document.getElementById("clock").textContent = new Date(data.updated_at).toLocaleString();
  document.getElementById("equity").textContent = `R${money(data.equity_zar)}`;
  document.getElementById("equity-sub").textContent = `paper cash R${money(data.cash_zar)}`;

  const pnl = document.getElementById("pnl");
  const pnlZar = data.pnl_zar ?? 0;
  pnl.textContent = `${pnlZar >= 0 ? "+" : ""}R${money(pnlZar)}`;
  setPnL(pnl, pnlZar);
  document.getElementById("pnl-sub").textContent = `${data.pnl_pct >= 0 ? "+" : ""}${money(data.pnl_pct)}% · day R${money(data.daily_pnl_zar ?? 0)}`;

  const news = document.getElementById("news");
  news.textContent = `${data.news_score >= 0 ? "+" : ""}${money(data.news_score)}`;
  setPnL(news, data.news_score);

  document.getElementById("risk").textContent = `${data.max_leverage}x`;
  document.getElementById("risk-sub").textContent =
    `${data.trade_count} fills · fee ${money(data.taker_fee_pct)}% · slippage ${data.slippage_bps} bps`;

  const headlines = document.getElementById("headlines");
  if (data.news_headlines?.length) {
    headlines.innerHTML = data.news_headlines.map((h) => `<li>${h}</li>`).join("");
  } else {
    headlines.innerHTML = `<li>No headlines loaded</li>`;
  }

  const newsStatus = document.getElementById("news-status");
  if (newsStatus) {
    if (data.news_source === "scout") {
      const age = data.news_age_min == null ? "" : ` · ${money(data.news_age_min, 0)}m old`;
      newsStatus.textContent = `scout · ${data.news_count} headlines${age}`;
      newsStatus.className = "pill good";
    } else {
      newsStatus.textContent = `live fetch · ${data.news_count ?? 0} headlines`;
      newsStatus.className = "pill muted";
    }
  }
  const newsCoins = document.getElementById("news-coins");
  if (newsCoins) {
    const pc = data.news_per_coin || {};
    const parts = Object.entries(pc)
      .filter(([, v]) => v && v.score != null)
      .map(([coin, v]) => {
        const cls = v.score > 0.05 ? "up" : v.score < -0.05 ? "down" : "";
        return `<span class="${cls}">${coin} ${v.score >= 0 ? "+" : ""}${money(v.score)} (${v.headlines})</span>`;
      });
    newsCoins.innerHTML = parts.length
      ? `Per-coin sentiment: ${parts.join(" · ")}`
      : "Per-coin sentiment: no coin-specific headlines yet";
  }

  renderStats(data.stats, data.backtest);
  renderWallet(data.wallet);
  renderCharts(data.signals, data.positions);
  renderSignals(data.signals);
  renderPositions(data.positions);
  renderTrades(data.trades);
}

async function load() {
  const status = document.getElementById("status");
  status.textContent = "updating";
  status.className = "pill muted";
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45000);
    const res = await fetch("/api/snapshot", { signal: controller.signal });
    clearTimeout(timer);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "request failed");
    render(data);
  } catch (err) {
    status.textContent = err.name === "AbortError" ? "timeout" : "offline";
    status.className = "pill bad";
  }
}

document.getElementById("refresh").addEventListener("click", load);
load();
setInterval(load, 20000);
