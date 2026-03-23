/**
 * 22B Strategy Engine Dashboard — Real-time WebSocket client
 *
 * Responsibilities:
 *  - Connect to /ws/live and maintain connection with auto-reconnect
 *  - Handle incoming events: snapshot, ticker, regime, candle, funding, system_mode, etc.
 *  - Update DOM elements in-place without full page refresh
 *  - Show toast notifications for regime changes
 */

"use strict";

// ============================================================
// Config
// ============================================================
function getWsUrl() {
  const configured = window.__WS_URL__ || "";
  // If configured URL has localhost/127.0.0.1 but we're on a different host, use current host
  if (configured.includes("localhost") || configured.includes("127.0.0.1")) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws/live`;
  }
  return configured || `ws://${location.host}/ws/live`;
}
const WS_URL       = getWsUrl();
const RECONNECT_MS = 3000;
const API_INDICATOR_REFRESH_MS  = 30000;   // re-fetch indicators every 30s
const API_STRATEGY_REFRESH_MS   = 30000;   // re-fetch strategy board every 30s
const API_POSITIONS_REFRESH_MS  = 15000;   // re-fetch live positions every 15s
const API_TRADE_LOG_REFRESH_MS  = 60000;   // re-fetch trade log every 60s

// ============================================================
// State
// ============================================================
const state = {
  ws:              null,
  reconnectTimer:  null,
  connected:       false,
  lastRegime:      null,
  indicators:      {},    // symbol → { indicators, price, change_pct, funding_rate, open_interest }
  tickers:         {},    // symbol → ticker dict
  signals:         [],    // last 50 signals (newest first)
  openPositions:   [],    // open paper positions
  livePositions:   [],    // live positions from Binance
  tradeLog:        [],    // closed trades
  killSwitchActive: window.__KILL_SWITCH_ACTIVE__ || false,
  tradeLogMode:    '',    // '' | 'LIVE' | 'PAPER'
  reconcileStatus: null,
};

// ============================================================
// DOM helpers
// ============================================================
function $(id)      { return document.getElementById(id); }
function $q(sel)    { return document.querySelector(sel); }
function $qa(sel)   { return document.querySelectorAll(sel); }

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function setClass(id, cls) {
  const el = $(id);
  if (el) el.className = cls;
}

function addFlash(el, cls = 'flash') {
  if (!el) return;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 600);
}

// ============================================================
// Number formatting
// ============================================================
function fmtPrice(v, sym) {
  if (v == null) return '—';
  if (typeof v !== 'number') v = parseFloat(v);
  if (isNaN(v)) return '—';
  if (sym === 'BTCUSDT') return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (v >= 1000) return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (v >= 1)    return v.toFixed(4);
  return v.toFixed(6);
}

function fmtPct(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${n.toFixed(2)}%`;
}

function fmtNum(v, decimals = 2) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  return n.toFixed(decimals);
}

function fmtVol(v) {
  if (v == null) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (n >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(2)}`;
}

function pctClass(v) {
  if (v == null) return 'neutral';
  return parseFloat(v) >= 0 ? 'positive' : 'negative';
}

function colorClass(v) {
  if (v == null) return '';
  const n = parseFloat(v);
  if (n > 0) return 'highlight-green';
  if (n < 0) return 'highlight-red';
  return '';
}

// ============================================================
// WebSocket
// ============================================================
function connect() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) return;

  state.ws = new WebSocket(WS_URL);

  state.ws.onopen = () => {
    console.log('[WS] Connected');
    state.connected = true;
    updateWsStatus('connected');
    clearTimeout(state.reconnectTimer);
  };

  state.ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMessage(msg);
    } catch (e) {
      console.error('[WS] Parse error:', e);
    }
  };

  state.ws.onclose = (evt) => {
    console.warn('[WS] Closed', evt.code);
    state.connected = false;
    updateWsStatus('reconnecting');
    state.reconnectTimer = setTimeout(connect, RECONNECT_MS);
  };

  state.ws.onerror = (err) => {
    console.error('[WS] Error', err);
    state.connected = false;
    updateWsStatus('disconnected');
  };
}

function updateWsStatus(status) {
  const dot  = $q('.ws-dot');
  const text = $q('.ws-status-text');
  if (!dot) return;
  dot.className = `ws-dot ${status}`;
  if (text) {
    const labels = { connected: 'Live', disconnected: 'Disconnected', reconnecting: 'Reconnecting…' };
    text.textContent = labels[status] || status;
  }
}

// ============================================================
// Message dispatcher
// ============================================================
function handleMessage(msg) {
  switch (msg.type) {
    case 'snapshot':               handleSnapshot(msg.data);           break;
    case 'ticker':                 handleTicker(msg.data);              break;
    case 'regime':                 handleRegime(msg.data);              break;
    case 'candle':                 handleCandle(msg.data);              break;
    case 'funding':                handleFunding(msg.data);             break;
    case 'open_interest':          handleOI(msg.data);                  break;
    case 'system_mode':            handleSystemMode(msg.data);          break;
    case 'exchange_status':        handleExchangeStatus(msg.data);      break;
    case 'signal':                 handleSignal(msg.data);              break;
    case 'paper_position_opened':  handlePositionOpened(msg.data);      break;
    case 'paper_position_closed':  handlePositionClosed(msg.data);      break;
    // Phase 3 — Execution
    case 'kill_switch':            handleKillSwitchUpdate(msg.data);    break;
    case 'order_update':           handleOrderUpdate(msg.data);         break;
    case 'reconcile':              handleReconcileUpdate(msg.data);     break;
    case 'account_balance':        handleBalanceUpdate(msg.data);       break;
    // Phase 4 — Panel 6
    case 'regime_interpretation':  handleRegimeInterpretation(msg.data); break;
    case 'weekly_review':          handleWeeklyReview(msg.data);         break;
    case 'daily_review':           handleDailyReview(msg.data);          break;
    case 'daily_alert_count':      handleDailyAlertCount(msg.data);      break;
    case 'recommendation_decided': handleRecommendationDecided(msg.data); break;
    case 'ping':                   /* keepalive — ignore */              break;
    default:
      console.debug('[WS] Unknown message type:', msg.type);
  }
}

// ============================================================
// Snapshot (initial state)
// ============================================================
function handleSnapshot(data) {
  if (!data) return;

  if (data.system_mode)  updateSystemMode(data.system_mode);
  if (data.exchange_ok != null) updateExchangeStatus(data.exchange_ok);
  updatePnL(data.daily_pnl, data.daily_pnl_pct);
  updateExposure(data.exposure_pct);

  if (data.tickers) {
    Object.entries(data.tickers).forEach(([sym, t]) => {
      state.tickers[sym] = t;
    });
  }
  if (data.funding) {
    Object.entries(data.funding).forEach(([sym, rate]) => {
      state.indicators[sym] = state.indicators[sym] || {};
      state.indicators[sym].funding_rate = rate;
    });
  }
  if (data.regime) handleRegime(data.regime);

  // Re-render indicator cards
  renderAllSymbolCards();
}

// ============================================================
// Ticker update
// ============================================================
function handleTicker(data) {
  const { symbol, ticker } = data;
  if (!symbol || !ticker) return;
  state.tickers[symbol] = ticker;

  // Update card
  updateSymbolCard(symbol);
}

// ============================================================
// Regime
// ============================================================
function handleRegime(data) {
  if (!data) return;
  const regime = data.regime;

  const old = state.lastRegime;
  state.lastRegime = regime;

  updateRegimeDisplay(data);

  if (old && old !== regime) {
    showToast('regime-change', '⚡ Regime Changed', `${old} → ${regime}`);
  }
}

function updateRegimeDisplay(data) {
  const regime = data.regime || 'UNKNOWN';

  // Header regime
  const headerRegime = $('header-regime');
  if (headerRegime) {
    headerRegime.innerHTML = `<span class="regime-badge regime-${regime}">${regime}</span>`;
  }

  // Header entry allowed
  const entryEl = $('header-entry');
  if (entryEl) {
    const allowed = data.new_entry_allowed;
    entryEl.innerHTML = allowed
      ? `<span class="text-green">✓ Allowed</span>`
      : `<span class="text-red">✗ Blocked</span>`;
  }

  // Regime updated time
  const ts = data.ts ? new Date(data.ts).toLocaleTimeString() : '—';
  setText('regime-updated', ts);

  // Regime panel
  const regimeName = $('regime-name');
  if (regimeName) {
    regimeName.textContent = regime;
    regimeName.className = `regime-name regime-${regime}`;
  }

  // Allowed strategies
  const allowedEl = $('regime-allowed');
  if (allowedEl && data.allowed_strategies) {
    allowedEl.innerHTML = data.allowed_strategies.length
      ? data.allowed_strategies.map(s => `<span>${s}</span>`).join('')
      : '<span style="color:var(--red)">NONE</span>';
  }

  // Regime indicators
  renderRegimeIndicators(data);
}

function renderRegimeIndicators(data) {
  const container = $('regime-indicators');
  if (!container) return;

  const fields = [
    { key: 'btc_price',    label: 'BTC Price',   fmt: v => fmtPrice(v, 'BTCUSDT') },
    { key: 'btc_ema50',    label: 'EMA 50 (4H)', fmt: v => fmtPrice(v, 'BTCUSDT') },
    { key: 'btc_atr_pct',  label: 'ATR %',        fmt: v => fmtNum(v, 2) + '%' },
    { key: 'btc_ret_24h',  label: 'BTC 24H Ret', fmt: v => fmtPct(v), color: true },
    { key: 'btc_ret_1h',   label: 'BTC 1H Ret',  fmt: v => fmtPct(v), color: true },
    { key: 'btc_rsi',      label: 'RSI (4H)',     fmt: v => fmtNum(v, 1) },
    { key: 'funding',      label: 'Funding Rate', fmt: v => fmtNum(v, 4) + '%' },
    { key: 'btc_bb_bw',    label: 'BB Bandwidth', fmt: v => fmtNum(v, 4) },
  ];

  container.innerHTML = fields.map(f => {
    const val = data[f.key];
    const fmtd = val != null ? f.fmt(val) : '—';
    const extra = f.color ? colorClass(val) : '';
    return `
      <div class="regime-indicator-item">
        <div class="metric-label">${f.label}</div>
        <div class="metric-value ${extra}">${fmtd}</div>
      </div>`;
  }).join('');
}

// ============================================================
// Candle (no-op for now — used for future charting)
// ============================================================
function handleCandle(data) {
  // Phase 1: just log
  // Phase 2: update mini chart
}

// ============================================================
// Panel 2 — Signals
// ============================================================

/**
 * Called when the WebSocket pushes a new "signal" event.
 * Prepends to the in-memory list and re-renders the table.
 */
function handleSignal(data) {
  if (!data) return;
  // Prepend new signal (newest first)
  state.signals.unshift(data);
  if (state.signals.length > 50) state.signals.length = 50;

  renderSignalsTable();

  // Flash the live indicator dot
  flashLiveDot('signals-live-dot');

  // Toast for BUY/SELL
  if (data.action === 'BUY' || data.action === 'SELL') {
    const actionLabel = data.action === 'BUY' ? '🟢 BUY' : '🔴 SELL';
    showToast(
      data.action === 'BUY' ? 'success' : 'error',
      `${actionLabel} Signal — ${data.symbol}`,
      `${data.strategy} | conf ${(data.confidence * 100).toFixed(0)}% | ${data.regime}`,
      4000,
    );
  }
}

function renderSignalsTable() {
  const tbody = $('signals-tbody');
  if (!tbody) return;

  const countEl = $('signals-count');
  if (countEl) countEl.textContent = `${state.signals.length} signal${state.signals.length !== 1 ? 's' : ''}`;

  if (state.signals.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="8">Waiting for signals…</td></tr>';
    return;
  }

  tbody.innerHTML = state.signals.map(sig => {
    const time = sig.ts ? new Date(sig.ts).toLocaleTimeString() : '—';
    const actionCls = sig.action === 'BUY' ? 'action-buy' : sig.action === 'SELL' ? 'action-sell' : 'action-skip';
    const confPct = sig.confidence != null ? Math.round(sig.confidence * 100) : 0;
    const confBar = `
      <div class="conf-bar-wrap" title="${confPct}%">
        <div class="conf-bar-fill" style="width:${confPct}%;background:${confBarColor(confPct)}"></div>
        <span class="conf-bar-label">${confPct}%</span>
      </div>`;
    const reason = sig.reason
      ? `<span class="signal-reason" title="${escapeHtml(sig.reason)}">${escapeHtml(truncate(sig.reason, 48))}</span>`
      : '—';
    return `
      <tr class="signal-row ${actionCls}-row">
        <td class="text-mono" style="white-space:nowrap;font-size:11px">${time}</td>
        <td class="text-mono fw-bold">${sig.symbol || '—'}</td>
        <td><span class="signal-action-badge ${actionCls}">${sig.action || '—'}</span></td>
        <td class="text-mono" style="font-size:11px">${sig.strategy || '—'}</td>
        <td><span class="mode-badge mode-paper">PAPER</span></td>
        <td>${confBar}</td>
        <td><span class="regime-badge regime-${sig.regime || 'UNKNOWN'}" style="font-size:10px">${sig.regime || '—'}</span></td>
        <td>${reason}</td>
      </tr>`;
  }).join('');
}

function confBarColor(pct) {
  if (pct >= 80) return 'var(--green)';
  if (pct >= 60) return 'var(--accent-cyan)';
  if (pct >= 40) return 'var(--yellow)';
  return 'var(--text-muted)';
}

// ============================================================
// Panel 3 — Open Paper Positions
// ============================================================

function handlePositionOpened(data) {
  if (!data) return;
  state.openPositions.unshift(data);
  renderPositionsTable();
  showToast('success', `Position Opened — ${data.symbol}`,
    `${data.strategy} | ${data.side} @ ${fmtPrice(data.entry_price, data.symbol)}`, 3500);
}

function handlePositionClosed(data) {
  if (!data) return;
  // Remove from open list
  state.openPositions = state.openPositions.filter(p => p.id !== data.id);
  renderPositionsTable();

  const pnl = data.pnl_pct != null ? data.pnl_pct.toFixed(2) : '?';
  const win = data.pnl_pct != null && data.pnl_pct > 0;
  showToast(
    win ? 'success' : 'error',
    `Position Closed — ${data.symbol} (${win ? 'WIN' : 'LOSS'})`,
    `${data.strategy} | PnL ${win ? '+' : ''}${pnl}% | ${data.close_reason || ''}`,
    4000,
  );
}

function renderPositionsTable() {
  const tbody = $('positions-tbody');
  if (!tbody) return;

  const countEl = $('positions-count');
  if (countEl) countEl.textContent = `${state.openPositions.length} open`;

  if (state.openPositions.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="8">No open positions</td></tr>';
    return;
  }

  tbody.innerHTML = state.openPositions.map(pos => {
    const opened = pos.opened_at ? new Date(pos.opened_at).toLocaleTimeString() : '—';
    const sideCls = pos.side === 'LONG' ? 'action-buy' : 'action-sell';
    return `
      <tr>
        <td class="text-mono fw-bold">${pos.symbol || '—'}</td>
        <td class="text-mono" style="font-size:11px">${pos.strategy || '—'}</td>
        <td><span class="signal-action-badge ${sideCls}">${pos.side || '—'}</span></td>
        <td class="text-mono">${fmtPrice(pos.entry_price, pos.symbol)}</td>
        <td class="text-mono text-green">${pos.tp != null ? fmtPrice(pos.tp, pos.symbol) : '—'}</td>
        <td class="text-mono text-red">${pos.sl != null ? fmtPrice(pos.sl, pos.symbol) : '—'}</td>
        <td><span class="regime-badge regime-${pos.regime || 'UNKNOWN'}" style="font-size:10px">${pos.regime || '—'}</span></td>
        <td class="text-mono" style="font-size:11px">${opened}</td>
      </tr>`;
  }).join('');
}

// ============================================================
// Panel 4 — Strategy Board
// ============================================================

async function refreshStrategyBoard() {
  try {
    const resp = await fetch('/api/strategies');
    if (!resp.ok) return;
    const strategies = await resp.json();
    renderStrategyBoard(strategies);

    const updEl = $('strategy-board-updated');
    if (updEl) updEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    console.debug('Strategy board refresh failed:', e);
  }
}

function renderStrategyBoard(strategies) {
  const grid = $('strategy-board-grid');
  if (!grid) return;

  if (!strategies || strategies.length === 0) {
    grid.innerHTML = '<div class="text-muted" style="padding:20px;font-size:13px">No strategies loaded</div>';
    return;
  }

  grid.innerHTML = strategies.map(s => {
    const st = s.stats || {};
    const mode = s.mode || 'PAPER';
    const modeCls = mode === 'PAUSED' ? 'mode-badge mode-paused' : 'mode-badge mode-paper';

    const wr = st.win_rate != null ? (st.win_rate * 100).toFixed(1) + '%' : '—';
    const pf = st.profit_factor != null ? st.profit_factor.toFixed(2) : '—';
    const tc = st.trade_count != null ? st.trade_count : 0;
    const exp = st.expectancy != null ? (st.expectancy >= 0 ? '+' : '') + st.expectancy.toFixed(2) + '%' : '—';
    const mdd = st.mdd != null ? st.mdd.toFixed(2) + '%' : '—';
    const openCnt = st.open_count || 0;

    const lastTs = s.last_signal_ts
      ? new Date(s.last_signal_ts).toLocaleTimeString()
      : '—';

    const wrColor = st.win_rate != null
      ? (st.win_rate >= 0.55 ? 'text-green' : st.win_rate >= 0.45 ? 'text-yellow' : 'text-red')
      : '';

    const regimeTags = (s.regime_filter || [])
      .map(r => `<span class="regime-badge regime-${r}" style="font-size:9px;padding:1px 5px">${r}</span>`)
      .join(' ');

    return `
      <div class="strategy-card">
        <div class="strategy-card-header">
          <div>
            <div class="strategy-name">${s.name}</div>
            <div class="strategy-category">${s.category || ''}</div>
          </div>
          <span class="${modeCls}">${mode}</span>
        </div>

        <div class="strategy-regime-tags">${regimeTags || '<span class="text-muted" style="font-size:10px">No regime filter</span>'}</div>

        <div class="strategy-stats-grid">
          <div class="strategy-stat">
            <div class="strategy-stat-label">Win Rate</div>
            <div class="strategy-stat-value ${wrColor}">${wr}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Profit Factor</div>
            <div class="strategy-stat-value">${pf}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Trades</div>
            <div class="strategy-stat-value">${tc}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Open</div>
            <div class="strategy-stat-value">${openCnt}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Expectancy</div>
            <div class="strategy-stat-value">${exp}</div>
          </div>
          <div class="strategy-stat">
            <div class="strategy-stat-label">Max DD</div>
            <div class="strategy-stat-value text-red">${mdd}</div>
          </div>
        </div>

        <div class="strategy-card-footer">
          <span class="strategy-last-signal">Last signal: ${lastTs}</span>
        </div>
      </div>`;
  }).join('');
}

// ============================================================
// Initial data fetch for Phase 2 panels
// ============================================================

async function refreshSignalsPanel() {
  try {
    const resp = await fetch('/api/signals?limit=50');
    if (!resp.ok) return;
    const signals = await resp.json();
    state.signals = signals;
    renderSignalsTable();
  } catch (e) {
    console.debug('Signals panel refresh failed:', e);
  }
}

async function refreshOpenPositions() {
  try {
    const resp = await fetch('/api/open-positions');
    if (!resp.ok) return;
    const positions = await resp.json();
    state.openPositions = positions;
    renderPositionsTable();
  } catch (e) {
    console.debug('Open positions refresh failed:', e);
  }
}

// ============================================================
// Funding rate
// ============================================================
function handleFunding(data) {
  const { symbol, rate } = data;
  if (!symbol) return;
  state.indicators[symbol] = state.indicators[symbol] || {};
  state.indicators[symbol].funding_rate = rate;
  updateFundingInCard(symbol, rate);
}

function updateFundingInCard(symbol, rate) {
  const el = $(`funding-${symbol}`);
  if (el) {
    el.textContent = rate != null ? fmtNum(rate, 4) + '%' : '—';
    el.className = `metric-value ${parseFloat(rate) > 0 ? 'highlight-yellow' : ''}`;
  }
}

// ============================================================
// Open Interest
// ============================================================
function handleOI(data) {
  const { symbol, oi } = data;
  if (!symbol) return;
  state.indicators[symbol] = state.indicators[symbol] || {};
  state.indicators[symbol].open_interest = oi;
  const el = $(`oi-${symbol}`);
  if (el) el.textContent = fmtVol(oi);
}

// ============================================================
// System mode
// ============================================================
function handleSystemMode(data) {
  updateSystemMode(data.mode);
}

function updateSystemMode(mode) {
  const el = $('header-mode');
  if (!el) return;
  el.innerHTML = `<span class="badge mode-${mode}"><span class="badge-dot" style="background:currentColor"></span>${mode}</span>`;
}

// ============================================================
// Exchange status
// ============================================================
function handleExchangeStatus(data) {
  updateExchangeStatus(data.ok);
}

function updateExchangeStatus(ok) {
  const el = $('header-exchange');
  if (!el) return;
  el.innerHTML = ok
    ? `<span class="conn-ok">● Binance OK</span>`
    : `<span class="conn-fail">● Binance FAIL</span>`;
}

// ============================================================
// P&L + Exposure
// ============================================================
function updatePnL(pnl, pct) {
  const el = $('header-pnl');
  if (!el) return;
  if (pnl == null) { el.textContent = '—'; return; }
  const sign = pnl >= 0 ? '+' : '';
  const cls  = pnl >= 0 ? 'pnl-positive' : 'pnl-negative';
  el.innerHTML = `<span class="${cls}">${sign}$${parseFloat(pnl).toFixed(2)} (${sign}${parseFloat(pct || 0).toFixed(2)}%)</span>`;
}

function updateExposure(pct) {
  const el = $('header-exposure');
  if (!el) return;
  el.textContent = pct != null ? `${parseFloat(pct).toFixed(1)}%` : '—';
}

// ============================================================
// Symbol cards
// ============================================================
function renderAllSymbolCards() {
  const grid = $('indicators-grid');
  if (!grid) return;

  const symbols = Array.from($qa('.symbol-card')).map(c => c.dataset.symbol);
  symbols.forEach(sym => updateSymbolCard(sym));
}

function updateSymbolCard(symbol) {
  const card = $(`card-${symbol}`);
  if (!card) return;

  const ticker = state.tickers[symbol] || {};
  const ind    = state.indicators[symbol] || {};

  const price     = ticker.price;
  const changePct = ticker.change_pct;
  const vol24h    = ticker.volume_24h;
  const funding   = ind.funding_rate;
  const oi        = ind.open_interest;
  const indicators = ind.indicators || {};

  // Price
  const priceEl = $(`price-${symbol}`);
  if (priceEl) {
    const newText = fmtPrice(price, symbol);
    if (priceEl.textContent !== newText) {
      priceEl.textContent = newText;
      addFlash(priceEl);
    }
  }

  // Change pct
  const changeEl = $(`change-${symbol}`);
  if (changeEl) {
    changeEl.textContent = fmtPct(changePct);
    changeEl.className = `symbol-change ${pctClass(changePct)}`;
  }

  // Card accent
  if (changePct != null) {
    card.className = `symbol-card ${parseFloat(changePct) >= 0 ? 'positive' : 'negative'}`;
  }

  // Volume
  const volEl = $(`vol-${symbol}`);
  if (volEl) volEl.textContent = fmtVol(vol24h);

  // Funding
  const fundEl = $(`funding-${symbol}`);
  if (fundEl) {
    fundEl.textContent = funding != null ? fmtNum(funding, 4) + '%' : '—';
    fundEl.className = `metric-value ${parseFloat(funding) > 0 ? 'highlight-yellow' : ''}`;
  }

  // OI
  const oiEl = $(`oi-${symbol}`);
  if (oiEl) oiEl.textContent = fmtVol(oi);

  // EMA / RSI / ATR / VWAP — prefer 4h then 1h
  const indData = indicators['4h'] || indicators['1h'] || {};

  const setMetric = (id, val, extra = '') => {
    const el = $(id);
    if (el) {
      el.textContent = val;
      if (extra) el.className = `metric-value ${extra}`;
    }
  };

  setMetric(`ema50-${symbol}`,  fmtPrice(indData.ema50, symbol));
  setMetric(`rsi-${symbol}`,    indData.rsi != null ? fmtNum(indData.rsi, 1) : '—',
    indData.rsi > 70 ? 'highlight-red' : indData.rsi < 30 ? 'highlight-green' : '');
  setMetric(`atr-${symbol}`,    indData.atr_pct != null ? fmtNum(indData.atr_pct, 2) + '%' : '—');
  setMetric(`vwap-${symbol}`,   fmtPrice(indData.vwap, symbol));
}

// ============================================================
// Periodic indicator refresh from REST
// ============================================================
async function refreshIndicators() {
  try {
    const resp = await fetch('/api/indicators');
    if (!resp.ok) return;
    const data = await resp.json();
    Object.entries(data).forEach(([sym, info]) => {
      state.indicators[sym] = info;
    });
    renderAllSymbolCards();
  } catch (e) {
    console.debug('Indicator refresh failed:', e);
  }
}

// ============================================================
// Toast notifications
// ============================================================
function showToast(type, title, msg, duration = 5000) {
  const container = $q('.toast-container');
  if (!container) return;

  const icons = {
    'regime-change': '⚡',
    'error':         '❌',
    'success':       '✅',
  };

  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <div class="toast-icon">${icons[type] || 'ℹ️'}</div>
    <div class="toast-body">
      <div class="toast-title">${title}</div>
      <div class="toast-msg">${msg}</div>
    </div>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(120%)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 350);
  }, duration);
}

// ============================================================
// Flash animation (CSS class added/removed)
// ============================================================
(function injectFlashStyle() {
  const style = document.createElement('style');
  style.textContent = `
    .flash {
      animation: flashBg 0.6s ease;
    }
    @keyframes flashBg {
      0%   { background-color: rgba(59,130,246,0.3); }
      100% { background-color: transparent; }
    }
  `;
  document.head.appendChild(style);
})();

// ============================================================
// Clock (header)
// ============================================================
function startClock() {
  function tick() {
    const el = $('header-clock');
    if (el) {
      const now = new Date();
      el.textContent = now.toUTCString().slice(17, 25) + ' UTC';
    }
  }
  tick();
  setInterval(tick, 1000);
}

// ============================================================
// Live dot flash helper
// ============================================================
function flashLiveDot(id) {
  const el = $(id);
  if (!el) return;
  el.classList.add('live-dot-flash');
  setTimeout(() => el.classList.remove('live-dot-flash'), 600);
}

// ============================================================
// Phase 3 — Kill Switch UI
// ============================================================

function handleKillSwitch() {
  if (state.killSwitchActive) {
    // Already active — show reset dialog
    const modal = $('kill-switch-modal');
    if (!modal) return;
    $('ks-modal-title').textContent = 'Reset Kill Switch';
    $('ks-modal-body').innerHTML =
      'The kill switch is currently <strong>ACTIVE</strong>.<br/><br/>' +
      'Reset will allow new entries again. System mode will return to OBSERVE.<br/><br/>' +
      'Are you sure you want to reset?';
    const confirmBtn = $('ks-modal-confirm-btn');
    if (confirmBtn) {
      confirmBtn.textContent = 'Reset Kill Switch';
      confirmBtn.className = 'modal-btn modal-btn-reset';
      confirmBtn.onclick = confirmKillSwitchReset;
    }
    modal.style.display = 'flex';
  } else {
    // Not active — show activation dialog
    const modal = $('kill-switch-modal');
    if (!modal) return;
    $('ks-modal-title').textContent = 'Confirm Kill Switch';
    $('ks-modal-body').innerHTML =
      'This will <strong>immediately block all new entries</strong> ' +
      'and cancel all open orders.<br/><br/>' +
      'Existing positions will be kept and protected by their SL/TP orders.<br/><br/>' +
      'Are you sure?';
    const confirmBtn = $('ks-modal-confirm-btn');
    if (confirmBtn) {
      confirmBtn.textContent = 'Confirm Kill Switch';
      confirmBtn.className = 'modal-btn modal-btn-confirm';
      confirmBtn.onclick = confirmKillSwitch;
    }
    modal.style.display = 'flex';
  }
}

function closeKillSwitchModal() {
  const modal = $('kill-switch-modal');
  if (modal) modal.style.display = 'none';
}

async function confirmKillSwitch() {
  closeKillSwitchModal();
  try {
    const resp = await fetch('/api/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        reason: 'Manual trigger from dashboard',
        authorized_by: 'dashboard_operator',
      }),
    });
    if (resp.ok) {
      showToast('error', '⚠ Kill Switch Activated', 'All new entries blocked. Orders cancelled.', 6000);
      updateKillSwitchButton(true);
    } else {
      const err = await resp.json();
      showToast('error', 'Kill Switch Failed', err.detail || 'Unknown error', 5000);
    }
  } catch (e) {
    showToast('error', 'Kill Switch Error', String(e), 5000);
  }
}

async function confirmKillSwitchReset() {
  closeKillSwitchModal();
  try {
    const resp = await fetch('/api/kill-switch/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ authorized_by: 'dashboard_operator' }),
    });
    if (resp.ok) {
      showToast('success', 'Kill Switch Reset', 'System mode → OBSERVE. New entries now allowed.', 5000);
      updateKillSwitchButton(false);
    } else {
      const err = await resp.json();
      showToast('error', 'Reset Failed', err.detail || 'Unknown error', 5000);
    }
  } catch (e) {
    showToast('error', 'Reset Error', String(e), 5000);
  }
}

function updateKillSwitchButton(isActive) {
  state.killSwitchActive = isActive;
  const btn = $('btn-kill-switch');
  const label = $('kill-switch-label');
  if (!btn) return;
  if (isActive) {
    btn.classList.add('kill-switch-btn-active');
    if (label) label.textContent = 'KILL SWITCH ON';
  } else {
    btn.classList.remove('kill-switch-btn-active');
    if (label) label.textContent = 'KILL SWITCH';
  }
}

function handleKillSwitchUpdate(data) {
  if (!data) return;
  const isActive = !!data.active;
  updateKillSwitchButton(isActive);

  if (isActive) {
    showToast(
      'error',
      '⚠ Kill Switch ACTIVE',
      `Reason: ${data.reason || '?'} | By: ${data.triggered_by || '?'}`,
      8000,
    );
  } else {
    showToast(
      'success',
      'Kill Switch Reset',
      `Reset by: ${data.reset_by || '?'}`,
      5000,
    );
  }
}

// ============================================================
// Phase 3 — Live Positions (Panel 3)
// ============================================================

async function refreshLivePositions() {
  try {
    const resp = await fetch('/api/live-positions');
    if (!resp.ok) return;
    const data = await resp.json();

    state.livePositions = data.live || [];
    state.openPositions = data.paper || [];

    renderLivePositionsTable();
    renderPositionsTable();
  } catch (e) {
    console.debug('Live positions refresh failed:', e);
  }
}

function renderLivePositionsTable() {
  const tbody = $('live-positions-tbody');
  if (!tbody) return;

  const countEl = $('live-positions-count');
  const totalEl = $('positions-count');
  const total   = state.livePositions.length + state.openPositions.length;

  if (countEl) countEl.textContent = `${state.livePositions.length} position${state.livePositions.length !== 1 ? 's' : ''}`;
  if (totalEl) totalEl.textContent = `${total} open`;

  if (state.livePositions.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="9">No live positions</td></tr>';
    return;
  }

  tbody.innerHTML = state.livePositions.map(pos => {
    const side     = pos.side || 'LONG';
    const sideCls  = side === 'LONG' ? 'action-buy' : 'action-sell';
    const pnl      = pos.pnl_pct != null ? parseFloat(pos.pnl_pct) : null;
    const pnlCls   = pnl == null ? '' : pnl >= 0 ? 'highlight-green' : 'highlight-red';
    const pnlStr   = pnl != null ? `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}% (${pos.unrealised_pnl != null ? (parseFloat(pos.unrealised_pnl) >= 0 ? '+' : '') + parseFloat(pos.unrealised_pnl).toFixed(2) : '?'} USDT)` : '—';
    const posId    = pos.id || '';

    return `
      <tr class="live-position-row">
        <td class="text-mono fw-bold">${pos.symbol || '—'}</td>
        <td><span class="signal-action-badge ${sideCls}">${side}</span></td>
        <td class="text-mono">${fmtNum(pos.qty, 4)}</td>
        <td class="text-mono">${fmtPrice(pos.entry_price, pos.symbol)}</td>
        <td class="text-mono">${fmtPrice(pos.current_price, pos.symbol)}</td>
        <td class="text-mono ${pnlCls}">${pnlStr}</td>
        <td class="text-mono text-red">${pos.sl != null ? fmtPrice(pos.sl, pos.symbol) : '—'}</td>
        <td class="text-mono text-green">${pos.tp != null ? fmtPrice(pos.tp, pos.symbol) : '—'}</td>
        <td>
          ${posId ? `<button class="btn-sm btn-danger" onclick="closePosition('${escapeHtml(posId)}', '${escapeHtml(pos.symbol || '')}')">Close</button>` : '—'}
        </td>
      </tr>`;
  }).join('');
}

async function closePosition(positionId, symbol) {
  if (!confirm(`Close position ${symbol}? This will submit a reduce-only MARKET order.`)) return;
  try {
    const resp = await fetch(`/api/order/close/${positionId}`, { method: 'POST' });
    if (resp.ok) {
      showToast('success', `Close order submitted for ${symbol}`, 'Reduce-only MARKET order placed.', 4000);
      setTimeout(refreshLivePositions, 2000);
    } else {
      const err = await resp.json();
      showToast('error', `Close failed: ${symbol}`, err.detail || 'Unknown error', 5000);
    }
  } catch (e) {
    showToast('error', 'Close position error', String(e), 5000);
  }
}

function togglePaperPositions() {
  const container = $('paper-positions-container');
  const icon = $('paper-collapse-icon');
  if (!container) return;
  const isHidden = container.style.display === 'none';
  container.style.display = isHidden ? 'block' : 'none';
  if (icon) icon.textContent = isHidden ? '▲ hide' : '▼ show';
}

function handleOrderUpdate(data) {
  if (!data) return;
  // Refresh positions after any order update
  refreshLivePositions();
}

// ============================================================
// Phase 3 — Reconcile status
// ============================================================

function handleReconcileUpdate(data) {
  if (!data) return;
  state.reconcileStatus = data;
  updateReconcileStatusBadge(data);

  if (data.has_discrepancies) {
    showToast(
      'error',
      '⚠ Reconciliation Discrepancy',
      `DB-only: ${data.in_db_not_exchange?.length || 0} | Exchange-only: ${data.in_exchange_not_db?.length || 0}`,
      7000,
    );
  }
}

function updateReconcileStatusBadge(data) {
  const dot   = $('reconcile-dot');
  const label = $('reconcile-label');
  if (!label) return;

  if (!data) {
    label.textContent = 'Reconcile: never';
    if (dot) dot.className = 'reconcile-dot reconcile-unknown';
    return;
  }

  const ageSec = data.age_sec != null ? data.age_sec : null;
  const ageStr = ageSec != null ? `${Math.round(ageSec / 60)}min ago` : '—';

  if (data.has_discrepancies) {
    label.textContent = `Reconcile: DISCREPANCY (${ageStr})`;
    if (dot) dot.className = 'reconcile-dot reconcile-error';
  } else {
    label.textContent = `Last reconciled: ${ageStr}`;
    if (dot) dot.className = 'reconcile-dot reconcile-ok';
  }
}

async function refreshReconcileStatus() {
  try {
    const resp = await fetch('/api/reconcile-status');
    if (!resp.ok) return;
    const data = await resp.json();
    state.reconcileStatus = data;
    updateReconcileStatusBadge(data);
  } catch (e) {
    console.debug('Reconcile status refresh failed:', e);
  }
}

// ============================================================
// Phase 3 — Account balance
// ============================================================

function handleBalanceUpdate(data) {
  if (!data) return;
  const el = $('header-balance');
  if (el) {
    el.textContent = data.balance != null ? `$${parseFloat(data.balance).toFixed(2)}` : '—';
    addFlash(el);
  }
}

// ============================================================
// Phase 3 — Trade Log (Panel 5)
// ============================================================

async function refreshTradeLog() {
  const strategy = ($('tl-filter-strategy')?.value || '').trim();
  const period   = $('tl-filter-period')?.value || '';
  const mode     = state.tradeLogMode;

  const params = new URLSearchParams({ limit: '100' });
  if (strategy) params.set('strategy', strategy);
  if (period)   params.set('period', period);
  if (mode)     params.set('mode', mode);

  try {
    const resp = await fetch(`/api/trade-log?${params}`);
    if (!resp.ok) return;
    const trades = await resp.json();
    state.tradeLog = trades;
    renderTradeLogTable();

    // Populate strategy dropdown from trade data
    populateStrategyFilter(trades);
  } catch (e) {
    console.debug('Trade log refresh failed:', e);
  }
}

function setTradeLogMode(mode) {
  state.tradeLogMode = mode;
  // Update button states
  ['', 'LIVE', 'PAPER'].forEach(m => {
    const btn = $(`tl-mode-${m === '' ? 'all' : m.toLowerCase()}`);
    if (btn) btn.className = 'mode-toggle-btn' + (m === mode ? ' active' : '');
  });
  refreshTradeLog();
}

function populateStrategyFilter(trades) {
  const select = $('tl-filter-strategy');
  if (!select) return;
  const strategies = [...new Set(trades.map(t => t.strategy).filter(Boolean))];
  const currentVal = select.value;
  select.innerHTML = '<option value="">All Strategies</option>' +
    strategies.map(s => `<option value="${escapeHtml(s)}" ${s === currentVal ? 'selected' : ''}>${escapeHtml(s)}</option>`).join('');
}

function renderTradeLogTable() {
  const tbody = $('trade-log-tbody');
  if (!tbody) return;

  if (!state.tradeLog || state.tradeLog.length === 0) {
    tbody.innerHTML = '<tr class="signals-empty-row"><td colspan="10">No closed trades yet</td></tr>';
    return;
  }

  tbody.innerHTML = state.tradeLog.map(trade => {
    const openTime   = trade.opened_at ? new Date(trade.opened_at).toLocaleDateString() + ' ' + new Date(trade.opened_at).toLocaleTimeString() : '—';
    const side       = trade.side || '—';
    const sideCls    = side === 'LONG' ? 'action-buy' : (side === 'SHORT' ? 'action-sell' : 'action-skip');
    const pnl        = trade.pnl_pct != null ? parseFloat(trade.pnl_pct) : null;
    const pnlCls     = pnl == null ? '' : pnl > 0 ? 'highlight-green' : (pnl < 0 ? 'highlight-red' : '');
    const pnlStr     = pnl != null ? `${pnl > 0 ? '+' : ''}${pnl.toFixed(2)}%` : '—';
    const mode       = trade.mode || 'PAPER';
    const modeCls    = mode === 'LIVE' ? 'mode-live' : 'mode-paper';
    const tradeId    = trade.id || trade.signal_id || '';

    // Duration
    let duration = '—';
    if (trade.opened_at && trade.closed_at) {
      const diffMs = trade.closed_at - trade.opened_at;
      const mins   = Math.floor(diffMs / 60000);
      if (mins < 60) duration = `${mins}m`;
      else if (mins < 1440) duration = `${Math.floor(mins / 60)}h ${mins % 60}m`;
      else duration = `${Math.floor(mins / 1440)}d`;
    }

    const clickAttr = tradeId ? `onclick="showAuditTrail('${escapeHtml(tradeId)}')" style="cursor:pointer"` : '';

    return `
      <tr class="trade-log-row" ${clickAttr}>
        <td class="text-mono" style="font-size:11px;white-space:nowrap">${openTime}</td>
        <td class="text-mono fw-bold">${trade.symbol || '—'}</td>
        <td><span class="signal-action-badge ${sideCls}">${side}</span></td>
        <td class="text-mono" style="font-size:11px">${trade.strategy || '—'}</td>
        <td><span class="mode-badge ${modeCls}">${mode}</span></td>
        <td class="text-mono">${fmtPrice(trade.entry_price, trade.symbol)}</td>
        <td class="text-mono">${fmtPrice(trade.exit_price, trade.symbol)}</td>
        <td class="text-mono ${pnlCls}">${pnlStr}</td>
        <td class="text-mono" style="font-size:11px">${duration}</td>
        <td style="font-size:11px;color:var(--text-muted)">${escapeHtml(truncate(trade.close_reason || '—', 20))}</td>
      </tr>`;
  }).join('');
}

// ============================================================
// Phase 3 — Audit Trail Modal
// ============================================================

async function showAuditTrail(tradeId) {
  const modal = $('audit-trail-modal');
  const content = $('audit-trail-content');
  if (!modal || !content) return;

  content.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted)">Loading…</div>';
  modal.style.display = 'flex';

  try {
    const resp = await fetch(`/api/trade-log/${tradeId}/audit`);
    if (!resp.ok) {
      content.innerHTML = '<div style="color:var(--red);padding:20px">Failed to load audit trail.</div>';
      return;
    }
    const trail = await resp.json();

    if (!trail || trail.length === 0) {
      content.innerHTML = '<div style="color:var(--text-muted);padding:20px;font-style:italic">No audit trail records found.</div>';
      return;
    }

    content.innerHTML = trail.map((entry, idx) => {
      const ts = entry.ts ? new Date(entry.ts).toLocaleString() : '—';
      let riskCheck = {};
      try { riskCheck = JSON.parse(entry.risk_check || '{}'); } catch(e) {}
      const fromStatus = riskCheck.from_status || '';
      const toStatus   = riskCheck.to_status   || '';
      const transArrow = fromStatus ? `${fromStatus} → ${toStatus}` : toStatus;

      return `
        <div class="audit-entry">
          <div class="audit-entry-header">
            <span class="audit-step">#${idx + 1}</span>
            <span class="audit-transition">${escapeHtml(transArrow)}</span>
            <span class="audit-ts">${ts}</span>
          </div>
          <div class="audit-entry-body">
            <div class="audit-reason">${escapeHtml(entry.decision_reason || '—')}</div>
            ${entry.strategy ? `<div class="audit-meta">Strategy: <code>${escapeHtml(entry.strategy)}</code></div>` : ''}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    content.innerHTML = `<div style="color:var(--red);padding:20px">Error: ${escapeHtml(String(e))}</div>`;
  }
}

function closeAuditModal() {
  const modal = $('audit-trail-modal');
  if (modal) modal.style.display = 'none';
}

// Close modals on overlay click
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) {
    e.target.style.display = 'none';
  }
});

// ============================================================
// String helpers
// ============================================================
function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function truncate(str, maxLen) {
  if (!str) return '';
  return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
}

// ============================================================
// Panel 6 — Regime Interpretation
// ============================================================

function handleRegimeInterpretation(data) {
  if (!data) return;
  renderRegimeInterpretation(data);
  const updEl = $('ai-panel-updated');
  if (updEl) updEl.textContent = `AI updated ${new Date().toLocaleTimeString()}`;
  showToast('regime-change', '🤖 AI Regime Analysis',
    `Regime ${data.regime || ''} interpreted`, 4000);
}

function renderRegimeInterpretation(data) {
  const nameEl = $('interp-regime-name');
  if (nameEl) {
    nameEl.innerHTML = `<span class="regime-badge regime-${data.regime || 'UNKNOWN'}">${data.regime || 'UNKNOWN'}</span>`;
  }
  const factorsEl = $('interp-why-factors');
  if (factorsEl) {
    const factors = data.why_factors || [];
    factorsEl.innerHTML = factors.length
      ? factors.map(f => `<li class="interp-factor-item">${escapeHtml(String(f))}</li>`).join('')
      : '<li class="text-muted" style="font-size:12px">No factors available</li>';
  }
  const transEl = $('interp-transition');
  if (transEl) transEl.textContent = data.transition_signals || '—';
  const stratEl = $('interp-strategy-rec');
  if (stratEl) stratEl.textContent = data.strategy_recommendations || '—';
  const tsEl = $('interp-ts');
  if (tsEl) {
    const ts = data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : '—';
    const aiOk = data.ai_available !== false;
    tsEl.textContent = `${aiOk ? 'AI' : 'Offline'} · ${ts}`;
    tsEl.style.color = aiOk ? 'var(--accent-cyan)' : 'var(--text-muted)';
  }
  const badge = $('ai-badge');
  if (badge) {
    const aiOk = data.ai_available !== false;
    badge.className = aiOk ? 'panel-badge-ai' : 'panel-badge-ai ai-offline';
    badge.textContent = aiOk ? 'AI' : 'AI OFFLINE';
  }
}

async function refreshRegimeInterpretation() {
  try {
    const resp = await fetch('/api/regime-interpretation');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.available) renderRegimeInterpretation(data);
  } catch (e) {
    console.debug('Regime interpretation refresh failed:', e);
  }
}

// ============================================================
// Panel 6 — Recommendations
// ============================================================

function handleWeeklyReview(data) {
  if (!data) return;
  refreshRecommendations();
  renderWeeklyReviewSummary(data);
  showToast('success', '📈 Weekly Review Complete',
    `${(data.recommendations || []).length} recommendation(s) generated`, 5000);
}

function handleDailyReview(data) {
  if (!data) return;
  if ((data.alert_count || 0) > 0) {
    showToast('error', `📊 Daily Review — ${data.date || ''}`,
      `${data.alert_count} warning(s) detected`, 6000);
  } else {
    showToast('success', `📊 Daily Review — ${data.date || ''}`, 'No warnings', 4000);
  }
  handleDailyAlertCount({ count: data.alert_count || 0 });
}

function handleDailyAlertCount(data) {
  const badge = $('daily-alert-badge');
  if (!badge) return;
  const count = data.count || 0;
  badge.textContent = count;
  badge.style.display = count > 0 ? 'inline-flex' : 'none';
}

function handleRecommendationDecided(data) {
  if (!data) return;
  refreshRecommendations();
}

async function refreshRecommendations() {
  try {
    const [pendResp, histResp] = await Promise.all([
      fetch('/api/recommendations'),
      fetch('/api/recommendations/history?limit=10'),
    ]);
    if (pendResp.ok) renderRecommendations(await pendResp.json());
    if (histResp.ok) renderRecommendationHistory(await histResp.json());
  } catch (e) {
    console.debug('Recommendations refresh failed:', e);
  }
}

function renderRecommendations(recommendations) {
  const list = $('recommendations-list');
  if (!list) return;
  const countEl = $('rec-pending-count');
  if (countEl) countEl.textContent = `${recommendations.length} pending`;
  if (!recommendations || recommendations.length === 0) {
    list.innerHTML = `<div class="text-muted" style="font-size:12px;padding:16px;text-align:center;font-style:italic">No pending recommendations</div>`;
    return;
  }
  list.innerHTML = recommendations.map(rec => buildRecommendationCard(rec, true)).join('');
}

function buildRecommendationCard(rec, showActions) {
  const typeColors = { PROMOTE: 'var(--green)', DEMOTE: 'var(--red)', MODIFY: 'var(--yellow)', RETIRE: 'var(--text-muted)' };
  const color = typeColors[rec.type] || 'var(--text-primary)';
  const sd = rec.supporting_data || {};
  const wr  = sd.win_rate  != null ? (sd.win_rate  * 100).toFixed(1) + '%' : '—';
  const pf  = sd.profit_factor != null ? sd.profit_factor.toFixed(2) : '—';
  const tc  = sd.trade_count != null ? sd.trade_count : '—';
  const exp = sd.expectancy  != null ? (sd.expectancy >= 0 ? '+' : '') + sd.expectancy.toFixed(2) + '%' : '—';
  const counterArgs = (rec.counter_arguments || [])
    .map(a => `<li style="font-size:11px;color:var(--text-secondary)">${escapeHtml(a)}</li>`)
    .join('');
  const actionsHtml = showActions && rec.status === 'PENDING' ? `
    <div class="rec-actions">
      <input type="text" class="rec-reason-input" placeholder="Decision reason (required)…" id="reason-${escapeHtml(rec.id)}" />
      <div class="rec-action-buttons">
        <button class="rec-btn rec-btn-approve" onclick="decideRecommendation('${escapeHtml(rec.id)}','APPROVED')">✓ Approve</button>
        <button class="rec-btn rec-btn-reject"  onclick="decideRecommendation('${escapeHtml(rec.id)}','REJECTED')">✗ Reject</button>
        <button class="rec-btn rec-btn-defer"   onclick="decideRecommendation('${escapeHtml(rec.id)}','DEFERRED')">⏸ Defer</button>
      </div>
    </div>` : '';
  const decisionInfo = rec.status !== 'PENDING' && rec.decided_by ? `
    <div class="rec-decision-info">
      ${buildStatusBadge(rec.status)} by <span style="color:var(--text-primary)">${escapeHtml(rec.decided_by)}</span>
      ${rec.decision_reason ? `— "${escapeHtml(truncate(rec.decision_reason, 80))}"` : ''}
    </div>` : '';
  return `
    <div class="rec-card${rec.status !== 'PENDING' ? ' rec-card-decided' : ''}">
      <div class="rec-card-header">
        <div class="rec-type-badge" style="color:${color}">${rec.type}</div>
        <div class="rec-strategy-name">${escapeHtml(rec.strategy)}</div>
        <div class="rec-mode-arrow">
          <span class="mode-badge mode-paper">${rec.current_mode || '—'}</span>
          <span style="color:var(--text-muted);font-size:11px;margin:0 4px">→</span>
          <span class="mode-badge mode-paper">${rec.proposed_mode || '—'}</span>
        </div>
        ${rec.status !== 'PENDING' ? buildStatusBadge(rec.status) : ''}
      </div>
      <div class="rec-metrics">
        <div class="rec-metric"><div class="rec-metric-label">Win Rate</div><div class="rec-metric-value">${wr}</div></div>
        <div class="rec-metric"><div class="rec-metric-label">Profit Factor</div><div class="rec-metric-value">${pf}</div></div>
        <div class="rec-metric"><div class="rec-metric-label">Trades</div><div class="rec-metric-value">${tc}</div></div>
        <div class="rec-metric"><div class="rec-metric-label">Expectancy</div><div class="rec-metric-value">${exp}</div></div>
      </div>
      ${counterArgs ? `<div style="margin-top:8px"><div class="rec-label">Counter-arguments:</div><ul style="margin:4px 0 0 16px">${counterArgs}</ul></div>` : ''}
      ${rec.rollback_condition ? `<div class="rec-rollback">Rollback: ${escapeHtml(rec.rollback_condition)}</div>` : ''}
      ${decisionInfo}
      ${actionsHtml}
    </div>`;
}

function buildStatusBadge(status) {
  const map   = { PENDING: 'status-pending', APPROVED: 'status-approved', REJECTED: 'status-rejected', DEFERRED: 'status-deferred' };
  const icons = { PENDING: '⏳', APPROVED: '✓', REJECTED: '✗', DEFERRED: '⏸' };
  return `<span class="rec-status-badge ${map[status] || ''}">${icons[status] || ''} ${status}</span>`;
}

function renderRecommendationHistory(history) {
  const list = $('rec-history-list');
  if (!list) return;
  if (!history || history.length === 0) {
    list.innerHTML = '<div class="text-muted" style="font-size:11px;padding:8px;font-style:italic">No past decisions yet</div>';
    return;
  }
  list.innerHTML = history.map(rec => buildRecommendationCard(rec, false)).join('');
}

async function decideRecommendation(recId, decision) {
  const reasonInput = $(`reason-${recId}`);
  const reason = reasonInput ? reasonInput.value.trim() : '';
  if (!reason) {
    if (reasonInput) reasonInput.focus();
    showToast('error', 'Reason Required', 'Provide a decision reason before submitting', 3000);
    return;
  }
  try {
    const resp = await fetch(`/api/recommendations/${recId}/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, reason, decided_by: '22B_Operator' }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      showToast('error', 'Decision Failed', err.detail || 'Unknown error', 4000);
      return;
    }
    const labels = { APPROVED: '✓ Approved', REJECTED: '✗ Rejected', DEFERRED: '⏸ Deferred' };
    showToast(decision === 'APPROVED' ? 'success' : 'error', labels[decision] || decision, 'Recommendation decided', 3500);
    await refreshRecommendations();
  } catch (e) {
    showToast('error', 'Network Error', String(e), 4000);
  }
}

// ============================================================
// Panel 6 — Weekly Review Summary
// ============================================================

function renderWeeklyReviewSummary(data) {
  const container = $('ai-weekly-summary');
  const content   = $('weekly-review-content');
  if (!container || !content) return;

  const stats = data.strategy_stats || {};
  const statRows = Object.entries(stats).map(([name, s]) => {
    const pf = s.profit_factor != null ? s.profit_factor.toFixed(2) : '—';
    const wr = s.win_rate != null ? (s.win_rate * 100).toFixed(1) + '%' : '—';
    return `<tr>
      <td class="text-mono" style="font-size:11px">${escapeHtml(name)}</td>
      <td class="text-mono">${pf}</td>
      <td class="text-mono">${wr}</td>
      <td class="text-mono">${s.trade_count || 0}</td>
      <td><span class="mode-badge mode-paper">${s.mode || 'PAPER'}</span></td>
    </tr>`;
  }).join('');

  const aiHtml = data.ai_analysis ? `
    <div style="margin-top:14px">
      <div class="ai-section-subtitle">AI Analysis</div>
      <div class="ai-analysis-text">${escapeHtml(data.ai_analysis)}</div>
    </div>` : '';

  content.innerHTML = `
    <div style="font-size:11px;color:var(--text-muted);font-family:var(--font-mono);margin-bottom:10px">
      ${escapeHtml(data.week_label || '')} · ${escapeHtml(data.period_start || '')} → ${escapeHtml(data.period_end || '')}
    </div>
    ${statRows ? `<div class="signals-table-wrapper" style="margin-bottom:12px"><table class="signals-table">
      <thead><tr><th>Strategy</th><th>PF</th><th>WR</th><th>Trades</th><th>Mode</th></tr></thead>
      <tbody>${statRows}</tbody></table></div>` : ''}
    ${aiHtml}`;

  container.style.display = 'block';
}

async function refreshWeeklyReview() {
  try {
    const resp = await fetch('/api/weekly-review');
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.available) {
      renderWeeklyReviewSummary(data);
      if (data.recommendations) {
        renderRecommendations((data.recommendations || []).filter(r => r.status === 'PENDING'));
      }
    }
  } catch (e) {
    console.debug('Weekly review refresh failed:', e);
  }
}

async function refreshDailyAlertCount() {
  try {
    const resp = await fetch('/api/daily-alert-count');
    if (!resp.ok) return;
    handleDailyAlertCount(await resp.json());
  } catch (e) {
    console.debug('Daily alert count refresh failed:', e);
  }
}

// ============================================================
// Init
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
  connect();
  startClock();

  // Panel 1: Indicators
  refreshIndicators();
  setInterval(refreshIndicators, API_INDICATOR_REFRESH_MS);

  // Panel 2: Signals (initial load from REST, then live via WS)
  refreshSignalsPanel();

  // Panel 3: Live + Paper positions
  refreshLivePositions();
  setInterval(refreshLivePositions, API_POSITIONS_REFRESH_MS);

  // Panel 4: Strategy board
  refreshStrategyBoard();
  setInterval(refreshStrategyBoard, API_STRATEGY_REFRESH_MS);

  // Panel 5: Trade log
  refreshTradeLog();
  setInterval(refreshTradeLog, API_TRADE_LOG_REFRESH_MS);

  // Reconcile status
  refreshReconcileStatus();
  setInterval(refreshReconcileStatus, 60000);

  // Kill switch button initial state
  updateKillSwitchButton(state.killSwitchActive);

  // Panel 6: AI analysis (initial load)
  refreshRegimeInterpretation();
  refreshRecommendations();
  refreshWeeklyReview();
  refreshDailyAlertCount();

  // Refresh Panel 6 every 2 minutes
  setInterval(() => {
    refreshRegimeInterpretation();
    refreshRecommendations();
    refreshDailyAlertCount();
  }, 120000);

  // Handle manual refresh button if present
  const refreshBtn = $('btn-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      refreshIndicators();
      refreshBtn.textContent = '↻ Refreshing…';
      setTimeout(() => { refreshBtn.textContent = '↻ Refresh'; }, 1500);
    });
  }

  // System Health panel
  loadSystemHealth();
  setInterval(loadSystemHealth, 10000);
});

// ============================================================
// System Health Panel
// ============================================================
function fmtUptime(sec) {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60) % 60;
  const h = Math.floor(sec / 3600);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function setHealthItem(iconId, valId, ok, label) {
  const icon = $(iconId);
  const val  = $(valId);
  if (!icon || !val) return;
  icon.className = 'health-icon ' + (ok ? 'ok' : 'fail');
  icon.textContent = ok ? '●' : '●';
  val.textContent = label;
  val.style.color = ok ? 'var(--green)' : 'var(--red)';
}

async function loadSystemHealth() {
  try {
    const r = await fetch('/api/system-health');
    if (!r.ok) return;
    const d = await r.json();

    // Uptime
    const up = $('health-uptime');
    if (up) up.textContent = `Uptime: ${fmtUptime(d.uptime_sec || 0)}`;

    // Bot
    setHealthItem('hicon-bot', 'hval-bot', true, 'RUNNING');

    // Binance WS
    setHealthItem('hicon-ws', 'hval-ws', d.binance_ws, d.binance_ws ? 'Connected' : 'Disconnected');

    // OpenClaw
    setHealthItem('hicon-ai', 'hval-ai', d.openclaw, d.openclaw ? 'Connected' : 'Offline');

    // Telegram
    setHealthItem('hicon-tg', 'hval-tg', d.telegram, d.telegram ? 'Active' : 'Disabled');

    // Mode
    const modeEl = $('hval-mode');
    if (modeEl) {
      modeEl.textContent = d.system_mode || '—';
      modeEl.style.color = d.kill_switch ? 'var(--red)' : 'var(--text-primary)';
    }

    // Network
    const netEl = $('hval-net');
    if (netEl) {
      netEl.textContent = d.testnet ? 'TESTNET' : 'MAINNET';
      netEl.style.color = d.testnet ? 'var(--yellow)' : 'var(--green)';
    }

  } catch (e) {
    // Dashboard temporarily unreachable — don't spam errors
  }
}

// ============================================================
// Settings Panel
// ============================================================

let _currentSymbols = [];

async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    if (!r.ok) return;
    const d = await r.json();

    // Mode buttons
    ['OBSERVE','LIMITED','ACTIVE','BLOCKED'].forEach(m => {
      const btn = document.getElementById('mode-btn-' + m);
      if (!btn) return;
      btn.classList.toggle('mode-btn-active', d.system_mode === m);
    });

    // Kill switch status
    const ksLine = document.getElementById('ks-status-line');
    if (ksLine) {
      ksLine.textContent = d.kill_switch ? '🔴 현재 활성 — 신규 진입 차단 중' : '🟢 해제 상태';
      ksLine.style.color = d.kill_switch ? 'var(--red)' : 'var(--green)';
    }

    // AI toggle
    const aiBox = document.getElementById('toggle-ai');
    const aiTxt = document.getElementById('toggle-ai-text');
    if (aiBox) aiBox.checked = d.ai_enabled;
    if (aiTxt) aiTxt.textContent = d.ai_enabled ? 'ON' : 'OFF';

    // Symbols
    _currentSymbols = d.tracked_symbols || [];
    renderSymbolTags();
  } catch (e) {}
}

function renderSymbolTags() {
  const wrap = document.getElementById('symbol-tags');
  if (!wrap) return;
  wrap.innerHTML = '';
  _currentSymbols.forEach(sym => {
    const tag = document.createElement('span');
    tag.className = 'symbol-tag';
    tag.textContent = sym;
    tag.title = '클릭하여 제거';
    tag.onclick = () => {
      _currentSymbols = _currentSymbols.filter(s => s !== sym);
      renderSymbolTags();
    };
    wrap.appendChild(tag);
  });
}

function addSymbol() {
  const inp = document.getElementById('symbol-input');
  if (!inp) return;
  const sym = inp.value.trim().toUpperCase();
  if (!sym) return;
  if (!_currentSymbols.includes(sym)) {
    _currentSymbols.push(sym);
    renderSymbolTags();
  }
  inp.value = '';
}

async function saveSymbols() {
  await applySetting({ tracked_symbols: _currentSymbols }, '심볼 저장됨');
}

async function setMode(mode) {
  await applySetting({ system_mode: mode }, `모드 변경: ${mode}`);
  await loadSettings();
}

async function triggerKillSwitch(active) {
  const msg = active ? '🚨 Kill Switch를 활성화합니까?' : '✅ Kill Switch를 해제합니까?';
  if (!confirm(msg)) return;
  await applySetting({ kill_switch: active }, active ? 'Kill Switch 활성화' : 'Kill Switch 해제');
  await loadSettings();
}

async function setAI(enabled) {
  const aiTxt = document.getElementById('toggle-ai-text');
  if (aiTxt) aiTxt.textContent = enabled ? 'ON' : 'OFF';
  await applySetting({ ai_enabled: enabled }, `AI 분석 ${enabled ? '활성화' : '비활성화'}`);
}

async function applySetting(body, successMsg) {
  const statusEl = document.getElementById('settings-save-status');
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      showToast('success', '설정 저장', successMsg, 2500);
      if (statusEl) { statusEl.textContent = '✓ 저장됨'; statusEl.style.color = 'var(--green)'; }
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 3000);
    } else {
      const err = await r.json().catch(() => ({}));
      showToast('error', '설정 오류', err.detail || '저장 실패', 3000);
    }
  } catch (e) {
    showToast('error', '네트워크 오류', e.message, 3000);
  }
}

// Settings 초기 로드 및 주기적 갱신
loadSettings();
setInterval(loadSettings, 15000);

async function confirmRestart() {
  document.getElementById('restart-modal').style.display = 'none';
  showToast('success', 'Restarting', 'Bot process will restart in ~2s…', 3000);
  try {
    await fetch('/api/restart', { method: 'POST' });
  } catch (e) {
    // connection dropped is expected on restart
  }
  showToast('success', 'Dashboard Reloading', 'Reconnecting in 6 seconds…', 6000);
  setTimeout(() => location.reload(), 6000);
}
