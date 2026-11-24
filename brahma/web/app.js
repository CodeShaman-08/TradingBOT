/* BRAHMA ALARM dashboard — no build step, no framework. */

const $ = (id) => document.getElementById(id);
const IST = "Asia/Kolkata";

let chart = null;
let candleSeries = null;
let lineA = null;
let lineB = null;
let chartReady = false;
let refreshing = false;

/* ------------------------------------------------------------------ utils */
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function num(value, digits = 2) {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function money(value) {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return "—";
  return (n < 0 ? "-₹" : "₹") + Math.abs(n).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function fmtTime(ts) {
  if (!ts) return "—";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return String(ts).slice(11, 19);
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function tone(value) {
  const n = Number(value ?? 0);
  if (n > 0) return "green";
  if (n < 0) return "red";
  return "";
}

function showToast(message, kind = "ok") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.add("hidden"), 4200);
}

async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!response.ok) throw new Error(data.detail || response.statusText || "request failed");
  return data;
}

/* ------------------------------------------------------------------ chart */
function initChart() {
  const container = $("chart");
  if (!window.LightweightCharts) {
    container.classList.add("hidden");
    $("chart-fallback").classList.remove("hidden");
    return;
  }

  chart = LightweightCharts.createChart(container, {
    layout: {
      background: { color: "rgba(13, 20, 29, 1)" },
      textColor: "#8fa6bb",
      fontFamily: "ui-sans-serif, system-ui, sans-serif",
    },
    grid: {
      vertLines: { color: "rgba(27, 39, 51, 0.45)" },
      horzLines: { color: "rgba(27, 39, 51, 0.45)" },
    },
    rightPriceScale: { borderColor: "rgba(27, 39, 51, 0.8)" },
    timeScale: {
      borderColor: "rgba(27, 39, 51, 0.8)",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 4,
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { priceFormatter: (p) => Number(p).toFixed(2) },
  });

  candleSeries = chart.addCandlestickSeries({
    upColor: "#22c55e",
    downColor: "#ef4444",
    borderUpColor: "#22c55e",
    borderDownColor: "#ef4444",
    wickUpColor: "#22c55e",
    wickDownColor: "#ef4444",
  });
  lineA = chart.addLineSeries({
    color: "#38bdf8",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
    title: "A",
  });
  lineB = chart.addLineSeries({
    color: "#f59e0b",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
    title: "B",
  });

  const resize = () =>
    chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
  new ResizeObserver(resize).observe(container);
  resize();
}

function renderChart(payload) {
  if (!chart || !payload) return;
  const candles = payload.candles || [];
  if (!candles.length) return;

  candleSeries.setData(candles);
  lineA.setData(payload.line_a || []);
  lineB.setData(payload.line_b || []);

  if (!chartReady) {
    chart.timeScale().fitContent();
    chartReady = true;
  }
}

/* ----------------------------------------------------------------- status */
function setPill(id, text, klass = "") {
  const el = $(id);
  el.textContent = text;
  el.className = `pill ${klass}`.trim();
}

function renderStatus(payload) {
  const engine = payload.engine || {};
  const state = payload.state || {};
  const snap = payload.snapshot || {};
  const config = payload.config || {};
  const risk = payload.risk || {};
  const market = payload.market || {};
  const signal = payload.signal || {};

  const running = Boolean(engine.running);
  const halted = Boolean(state.kill_switch);
  if (halted) setPill("pill-run", "HALTED", "bad");
  else if (running) setPill("pill-run", "RUNNING", "ok");
  else if (engine.ready) setPill("pill-run", "STOPPED", "warn");
  else setPill("pill-run", "STARTING", "warn");

  setPill("pill-mode", config.mode || "—", config.mode === "LIVE" ? "live" : "");
  const live = config.mode === "LIVE";
  $("btn-live").disabled = live || config.broker !== "dhan";
  $("btn-paper").disabled = !live;
  const session = engine.session || "—";
  const forced = Boolean(config.session?.ignore_market_hours);
  setPill(
    "pill-session",
    forced && session !== "OPEN" ? `${session} · FORCED` : session,
    session === "OPEN" || forced ? "ok" : session === "PRE" ? "warn" : ""
  );
  setPill("pill-provider", config.data_provider || "—");
  setPill("pill-ready", engine.ready ? "ready" : "booting", engine.ready ? "ok" : "warn");

  // KPI: P&L
  const pnlEl = $("kpi-pnl");
  pnlEl.textContent = money(snap.day_pnl);
  pnlEl.className = tone(snap.day_pnl);
  $("kpi-pnl-sub").textContent = `realized ${money(snap.realized)} · unrealized ${money(snap.unrealized)}`;

  // KPI: position
  const qty = Number(snap.net_qty || 0);
  $("kpi-position").textContent = qty ? `${qty} @ ${num(snap.avg_price)}` : "FLAT";
  $("kpi-position").className = qty ? "amber" : "";
  $("kpi-position-sub").textContent = qty ? `option LTP ${num(snap.ltp)}` : "no open position";

  // KPI: LTP
  $("kpi-ltp").textContent = market.close ? num(market.close) : "—";
  const age = Number(market.age_sec || 0);
  const ageText = market.replay
    ? "simulated feed"
    : age >= 120
    ? `${num(age / 60, 1)} min old`
    : `${num(age, 0)}s old`;
  $("kpi-ltp-sub").textContent = market.error ? "data error" : `${market.bars || 0} bars · ${ageText}`;

  // KPI: signal
  const action = signal.action || "—";
  $("kpi-signal").textContent = action;
  $("kpi-signal").className = action === "BUY" ? "green" : action === "SELL" ? "red" : "";
  $("kpi-signal-sub").textContent = signal.reason || market.reason || "—";

  // KPI: entries
  const maxEntries = config.risk?.max_trades_per_day ?? "—";
  $("kpi-entries").textContent = `${state.entries ?? 0} / ${maxEntries}`;
  $("kpi-entries-sub").textContent = state.entries_blocked
    ? `blocked: ${state.entries_block_reason || "yes"}`
    : "entries allowed";

  // KPI: stop
  $("kpi-stop").textContent = risk.stop_amount ? money(risk.stop_amount) : "—";
  $("kpi-stop-sub").textContent = `max trade loss ${money(risk.max_trade_loss)}`;

  // Risk panel
  $("risk-kill").textContent = halted ? `ON · ${state.kill_reason || "manual"}` : "off";
  $("risk-kill").className = halted ? "red" : "green";
  $("risk-entries").textContent = state.entries_blocked
    ? `BLOCKED · ${state.entries_block_reason || "yes"}`
    : "allowed";
  $("risk-entries").className = state.entries_blocked ? "amber" : "green";

  const pending = state.pending_order;
  $("risk-pending").textContent = pending
    ? `${pending.kind || "?"} ${pending.side || ""} ${pending.qty || ""} · ${pending.status || "submitted"}`
    : "none";
  $("risk-pending").className = pending ? "amber" : "";

  const lastExit = state.last_exit;
  $("risk-last-exit").textContent = lastExit
    ? `${money(lastExit.pnl)} · ${lastExit.reason || ""}`
    : "—";
  $("risk-last-exit").className = lastExit ? tone(lastExit.pnl) : "";

  const instrument = engine.instrument;
  $("risk-instrument").textContent = instrument
    ? `${instrument.trading_symbol || instrument.security_id} · ${instrument.security_id}`
    : "unresolved";
  $("risk-lot").textContent = instrument
    ? `${instrument.lot_size ?? "—"} / ${instrument.freeze_qty || "—"}`
    : "—";
  $("risk-broker-sl").textContent = config.use_broker_sl
    ? state.sl_order_id
      ? `order ${state.sl_order_id}`
      : "enabled · none"
    : "disabled";

  // Error box
  const errors = [
    engine.data_error && `data: ${engine.data_error}`,
    engine.broker_error && `broker: ${engine.broker_error}`,
    engine.persist_error && `state: ${engine.persist_error}`,
    engine.last_error && `engine: ${engine.last_error}`,
    market.error && `market: ${market.error}`,
  ].filter(Boolean);
  const errorBox = $("error-box");
  if (errors.length) {
    errorBox.textContent = errors[0];
    errorBox.classList.remove("hidden");
  } else {
    errorBox.classList.add("hidden");
  }

  // Chart meta
  $("chart-title").textContent = `Underlying · ${config.underlying_symbol || ""} · 1m`;
  $("market-meta").textContent = market.error
    ? `market error: ${market.error}`
    : `${market.replay ? "replay · simulated session" : market.ready ? "ready" : "warming"} · ${market.reason || "—"} · A ${num(market.line_a)} · B ${num(market.line_b)} · candle ${market.last_candle || "—"}`;

  $("footer-instrument").textContent = instrument
    ? `${instrument.trading_symbol || ""} (${instrument.security_id}) · expiry ${instrument.expiry || "—"} · strike ${instrument.strike ?? "—"}`
    : "";
}

/* ----------------------------------------------------------------- tables */
function renderTrades(items) {
  $("trades-count").textContent = String(items.length);
  const body = $("trades-body");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">No trades yet</td></tr>';
    return;
  }
  body.innerHTML = items
    .map((item) => {
      const pnl = item.event === "CLOSE" ? money(item.pnl) : "—";
      const pnlClass = item.event === "CLOSE" ? tone(item.pnl) : "";
      const eventClass = item.event === "OPEN" ? "green" : "amber";
      return `<tr>
        <td>${esc(fmtTime(item.ts))}</td>
        <td class="${eventClass}">${esc(item.event || "")}</td>
        <td>${esc(item.side || "")}</td>
        <td>${esc(item.qty ?? "")}</td>
        <td>${item.price ? num(item.price) : "—"}</td>
        <td class="${pnlClass}">${pnl}</td>
        <td>${esc(item.reason || "")}</td>
      </tr>`;
    })
    .join("");
}

function orderFields(item) {
  const source = item.result || {};
  const intent = item.intent || {};
  return {
    kind: item.kind || intent.kind || "",
    side: item.side || intent.side || source.side || "",
    qty: item.qty ?? intent.qty ?? source.filled_qty ?? "",
    price: item.price ?? item.avg_fill_price ?? source.avg_fill_price ?? item.trigger ?? "",
    detail:
      item.status ||
      source.status ||
      item.message ||
      source.message ||
      item.error ||
      (item.ok === false ? "failed" : ""),
  };
}

function renderOrders(items) {
  $("orders-count").textContent = String(items.length);
  const body = $("orders-body");
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">No orders yet</td></tr>';
    return;
  }
  body.innerHTML = items
    .map((item) => {
      const fields = orderFields(item);
      const event = item.event || "";
      const bad = /fail|exception|reject/i.test(event + " " + fields.detail);
      return `<tr>
        <td>${esc(fmtTime(item.ts || item.created_at))}</td>
        <td class="${bad ? "red" : ""}">${esc(event)}</td>
        <td>${esc(fields.kind)}</td>
        <td>${esc(fields.side)}</td>
        <td>${esc(fields.qty)}</td>
        <td>${fields.price !== "" ? num(fields.price) : "—"}</td>
        <td>${esc(fields.detail)}</td>
      </tr>`;
    })
    .join("");
}

function renderLogs(lines) {
  const el = $("logs");
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  el.textContent = lines.length ? lines.join("\n") : "no log lines yet";
  if (atBottom) el.scrollTop = el.scrollHeight;
}

/* --------------------------------------------------------------- backtest */
function renderBacktest(result) {
  const verdict = $("backtest-verdict");
  verdict.textContent = `${result.verdict || "—"} · ${result.bars} bars · ${result.from} → ${result.to}`;
  verdict.className = `verdict ${
    (result.verdict || "").includes("kalman looks better")
      ? "good"
      : (result.verdict || "").includes("does not add edge")
      ? "bad"
      : "muted"
  }`;

  const rows = [
    ["Total return %", "total_return_pct", 2],
    ["Buy & hold %", "buy_hold_pct", 2],
    ["Max drawdown %", "max_drawdown_pct", 2],
    ["Profit factor", "profit_factor", 2],
    ["Win rate %", "win_rate", 2],
    ["Sharpe", "sharpe", 2],
    ["Trades", "trades", 0],
    ["Exposure %", "exposure_pct", 1],
  ];

  const body = $("backtest-body");
  body.innerHTML = rows
    .map(([label, key, digits]) => {
      const withK = result.kalman?.[key];
      const withoutK = result.no_kalman?.[key];
      const scale = key === "win_rate" ? 100 : 1;
      const delta = result.delta?.[key];
      const deltaText = delta === undefined || delta === null
        ? "—"
        : `${delta > 0 ? "+" : ""}${num(delta * scale, digits)}`;
      const deltaClass = delta === undefined || delta === null ? "" : delta > 0 ? "green" : delta < 0 ? "red" : "";
      return `<tr>
        <td>${esc(label)}</td>
        <td>${num((withK ?? 0) * scale, digits)}</td>
        <td>${num((withoutK ?? 0) * scale, digits)}</td>
        <td class="${deltaClass}">${deltaText}</td>
      </tr>`;
    })
    .join("");
}

async function runBacktest() {
  const button = $("btn-backtest");
  button.disabled = true;
  button.textContent = "Running…";
  try {
    const result = await fetchJSON("/api/backtest");
    renderBacktest(result);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Run comparison";
  }
}

/* --------------------------------------------------------------- controls */
async function control(path, word, reason) {
  const typed = window.prompt(`Type ${word} to confirm:`);
  if (typed === null) return;
  if (typed.trim().toUpperCase() !== word) {
    showToast(`Confirmation did not match "${word}"`, "error");
    return;
  }
  try {
    const result = await fetchJSON(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: typed.trim(), reason }),
    });
    showToast(result.message || "Done", "ok");
    await refresh();
  } catch (error) {
    showToast(error.message, "error");
  }
}

function bindControls() {
  $("btn-halt").addEventListener("click", () => control("/api/control/halt", "HALT", "manual halt"));
  $("btn-resume").addEventListener("click", () => control("/api/control/resume", "RESUME"));
  $("btn-block").addEventListener("click", () => control("/api/control/block", "BLOCK", "manual block"));
  $("btn-allow").addEventListener("click", () => control("/api/control/allow", "ALLOW"));
  $("btn-flatten").addEventListener("click", () => control("/api/control/flatten", "FLATTEN", "MANUAL_FLATTEN"));
  $("btn-reset").addEventListener("click", () => control("/api/control/reset-paper", "RESET"));
  $("btn-live").addEventListener("click", () => control("/api/control/go-live", "LIVE"));
  $("btn-paper").addEventListener("click", () => control("/api/control/go-paper", "PAPER"));
  $("btn-backtest").addEventListener("click", runBacktest);
}

/* ------------------------------------------------------------------ clock */
function tickClock() {
  const text = new Intl.DateTimeFormat("en-IN", {
    timeZone: IST,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
  $("clock").textContent = `${text} IST`;
}

/* ------------------------------------------------------------------- poll */
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const status = await fetchJSON("/api/status");
    renderStatus(status);

    const candles = await fetchJSON("/api/candles?limit=300");
    renderChart(candles);

    const trades = await fetchJSON("/api/trades?limit=60");
    renderTrades(trades.items || []);

    const orders = await fetchJSON("/api/orders?limit=60");
    renderOrders(orders.items || []);

    const logs = await fetchJSON("/api/logs?limit=150");
    renderLogs(logs.lines || []);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    refreshing = false;
  }
}

/* ------------------------------------------------------------------- boot */
document.addEventListener("DOMContentLoaded", () => {
  initChart();
  bindControls();
  tickClock();
  setInterval(tickClock, 1000);
  refresh();
  setInterval(refresh, 3000);
});
