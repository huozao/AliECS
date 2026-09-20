/* Fixed-window market screen: one bounded request, then cursor increments. */
(async () => {
  "use strict";
  const status = document.querySelector("#status"), root = document.querySelector("#contracts"), login = document.querySelector("#login"), windowSelect = document.querySelector("#window-minutes");
  const state = {cursor: null, timer: null, controller: null, inFlight: false, pendingReload: false, generation: 0, sourceRevision: null, charts: new Map(), rows: new Map(), hidden: document.hidden, retryAttempt: 0, windowMinutes: 5};
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const stamp = (row) => row.observed_at || row.captured_at || row.source_time;
  // A source may revise an order state at the same observed instant.  Keep the
  // revision/sequence in the cache key; collapsing to the timestamp erases a
  // cancel/reprice transition before the chart code can decide how to render it.
  const orderKey = row => `${stamp(row)}|${row.revision ?? row.snapshot_sequence ?? row.sequence ?? ""}|${row.ordinal ?? row.event_sequence ?? ""}|${JSON.stringify(row.orders || row.order_history || [])}`;
  const finite = value => value != null && value !== "" && Number.isFinite(Number(value));
  const colors = {upper:"#84b8ef", center:"#f6c85f", lower:"#84b8ef", buy:"#56d6b0", sell:"#ef9a6a"};
  const sgt = new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Singapore", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false});
  const timeLabel = value => Number.isFinite(Date.parse(value)) ? `${sgt.format(new Date(value))} SGT` : "—";
  const point = (row) => { const time = Date.parse(stamp(row)) / 1000, value = Number(row.last_price); return Number.isFinite(time) && finite(row.last_price) ? {time:Math.floor(time), value} : null; };
  function chartFor(contract) {
    let item = state.charts.get(contract); if (item) return item;
    const card = document.createElement("article"); card.className = "market-card realtime-contract-card"; card.dataset.contract = contract;
    card.innerHTML = `<div class="realtime-card-head"><h2>${esc(contract)}</h2><div class="last">—</div></div><div class="realtime-parallel"><div><div class="realtime-extremes"><span>秒内最高 <b data-extreme="high">—</b></span><span>秒内最低 <b data-extreme="low">—</b></span></div><div class="chart" aria-label="${esc(contract)} 成交与 I 价格带"></div></div><div class="five-price" aria-label="${esc(contract)} 模型价格带与盘口"><span class="sell"><small>上方卖挂单</small><b data-tag="sell">—</b></span><span class="upper"><small>I 价格带上边缘</small><b data-tag="upper">—</b></span><span class="current"><small>真实成交价</small><b data-tag="current">—</b></span><span class="center"><small>I 价格带中心</small><b data-tag="center">—</b></span><span class="lower"><small>I 价格带下边缘</small><b data-tag="lower">—</b></span><span class="buy"><small>下方买挂单</small><b data-tag="buy">—</b></span></div></div><div class="muted"></div><ol class="hedges"></ol>`;
    root.append(card); const node = card.querySelector(".chart");
    if (window.LightweightCharts) {
      const chart = LightweightCharts.createChart(node, {autoSize: true,
        layout: {background:{color:"transparent"}, textColor:"#91a1af", attributionLogo:false},
        localization:{timeFormatter:time=>`${sgt.format(new Date(time*1000))} SGT`},
        timeScale:{timeVisible:true, secondsVisible:true, tickMarkFormatter:time=>sgt.format(new Date(time*1000))},
        grid:{vertLines:{visible:false},horzLines:{color:"#243244"}}});
      const price = chart.addSeries(LightweightCharts.LineSeries, {color:"#ffffff",lineWidth:2,priceLineVisible:false,lastValueVisible:false});
      item = {card, chart, price, series:price, colors};
    } else item = {card};
    state.charts.set(contract, item); return item;
  }
  function updateChart(contract, data, quote, band, orders, ranking, orderData, ordersTruncated) {
    const item = chartFor(contract), record = state.rows.get(contract) || {quotes: new Map(), bands: new Map(), orders: new Map()};
    for (const row of data?.quotes || []) record.quotes.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    for (const row of data?.bands || []) record.bands.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    for (const row of orderData || []) record.orders.set(orderKey(row), row);
    const left = Date.now() - state.windowMinutes * 60 * 1000;
    const previous = [...record.orders.values()].filter(row => Date.parse(stamp(row)) < left).at(-1);
    for (const [key,row] of record.orders) if (Date.parse(stamp(row)) < left) record.orders.delete(key);
    // Preserve the state entering the window, without retaining the whole session.
    if (previous) {
      // This is a visual state-anchor, not a new market observation.  Preserve
      // the source timestamp for audit/revisions and keep the derived display
      // coordinate separately so the page never rewrites market history.
      const renderAnchorAt = new Date(left).toISOString();
      record.orders.set(`render-anchor|${renderAnchorAt}`, {
        ...previous, render_anchor_at: renderAnchorAt, order_history: [],
      });
    }
    for (const [key,row] of record.quotes) if (Date.parse(stamp(row)) < left) record.quotes.delete(key);
    for (const [key,row] of record.bands) if (Date.parse(stamp(row)) < left) record.bands.delete(key);
    state.rows.set(contract, record);
    const quotes = [...record.quotes.values()].sort((a,b) => Date.parse(stamp(a))-Date.parse(stamp(b)));
    const bands = [...record.bands.values()].sort((a,b) => Date.parse(stamp(a))-Date.parse(stamp(b)));
    const latest = quote || quotes.at(-1) || {};
    // 秒内最高/最低 live in the snapshot's one-second OHLC bucket; the quote
    // root has no highest/lowest field, so reading it always rendered "—".
    const value = (field, source = latest) => {
      const candidate = source?.[field];
      return finite(candidate) ? Number(candidate).toFixed(2) : "—";
    };
    const rawOrder = orders.find((row) => row.contract === contract) || {};
    const order = Object.fromEntries(["buy", "sell"].map(side => [side, MarketObservation.order({orders:[rawOrder.buy,rawOrder.sell].filter(Boolean)}, side)]));
    item.card.querySelector(".last").textContent = value("last_price");
    item.card.querySelector('[data-extreme="high"]').textContent = value("high", latest?.ohlc);
    item.card.querySelector('[data-extreme="low"]').textContent = value("low", latest?.ohlc);
    const tags = {sell: order.sell?.price, upper: band?.upper, current: latest?.last_price, center: band?.center, lower: band?.lower, buy: order.buy?.price};
    Object.entries(tags).forEach(([kind, price]) => { item.card.querySelector(`[data-tag="${kind}"]`).textContent = finite(price) ? Number(price).toFixed(2) : "—"; });
    item.card.querySelector(".muted").textContent = `买 ${order.buy?.price ?? "—"}（${order.buy?.status ?? "—"}） · 卖 ${order.sell?.price ?? "—"}（${order.sell?.status ?? "—"}） · 源时刻 ${timeLabel(latest?.source_time || latest?.observed_at)}${orderData == null ? " · 挂单历史未提供" : ordersTruncated ? " · 挂单历史已截断" : ""}`;
    item.card.querySelector(".hedges").innerHTML = (ranking || []).filter((row) => (row.target_contract || row.contract || row.symbol) === contract).slice(0,5).map((row) => `<li>${esc(row.contract || row.symbol || "无候选")}：${esc(row.rank ?? "—")} · ${esc(row.reason || row.status || "—")}</li>`).join("") || "<li>后端未给出该合约对冲候选</li>";
    if (!item.chart) return;
    const unique = rows => [...new Map(rows.map(row => [row.time,row])).values()].sort((a,b)=>a.time-b.time);
    item.price.setData(unique(quotes.map(point).filter(Boolean)));
    const timeline = new Map();
    for (const row of quotes) timeline.set(stamp(row), {time:stamp(row), quote:row});
    for (const row of bands) timeline.set(stamp(row), {...timeline.get(stamp(row)),time:stamp(row),band:row});
    const rows = [...timeline.values()].sort((a,b)=>Date.parse(a.time)-Date.parse(b.time));
    item.orderSourceRows = [...record.orders.values()]
      .sort((a,b)=>Date.parse(a.render_anchor_at || stamp(a))-Date.parse(b.render_anchor_at || stamp(b)))
      .map(row=>({time:row.render_anchor_at || stamp(row), source_time:stamp(row), quote:row}));
    MarketObservation.layers(item, rows, false);

  }
  function clearCharts() {
    state.rows.clear();
    for (const item of state.charts.values()) { item.chart?.remove(); item.card.remove(); }
    state.charts.clear(); state.since = null;
  }
  function render(body) {
    const window_minutes = body.window_minutes || state.windowMinutes;
    // A new run stream, or a server-side reset, invalidates what we hold.
    if (body.reset || body.revision_reset || (body.run_id && body.run_id !== state.runId)) {
      clearCharts();
    }
    state.runId = body.run_id ?? state.runId;
    state.sourceRevision = body.source_revision ?? state.sourceRevision;
    const quotes = new Map((body.quotes || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const bands = new Map((body.bands || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const contracts = new Set([...quotes.keys(), ...bands.keys(), ...Object.keys(body.series || {})]);
    [...contracts].sort().forEach((contract) => updateChart(contract, body.series?.[contract], quotes.get(contract), bands.get(contract), body.orders || [], body.hedge_ranking || [], body.order_series?.[contract], body.orders_truncated));
    // The server states how far it actually covered. Deriving the watermark
    // from the price rows we happened to draw assumes order state never lands
    // after the last quote, and it keeps incrementing past a window the server
    // had to truncate — the dropped left edge would then never be re-requested.
    // `next_since: null` means exactly that: ask for the whole window again.
    if ("next_since" in body) state.since = body.next_since || null;
    else {
      // Older source without the field: fall back to the drawn rows. Parse the
      // instant rather than comparing strings, since an ISO stamp may or may
      // not carry microseconds.
      let watermark = state.since ? Date.parse(state.since) : -Infinity;
      for (const record of state.rows.values())
        for (const row of [...record.quotes.values(), ...record.bands.values()]) {
          const at = stamp(row), time = Date.parse(at);
          if (Number.isFinite(time) && time >= watermark) { watermark = time; state.since = at; }
        }
    }
    const gaps = Array.isArray(body.sequence_gap_alerts) ? body.sequence_gap_alerts : [];
    const resolvedGaps = Array.isArray(body.sequence_gap_resolutions) ? body.sequence_gap_resolutions : [];
    const gapNotice = gaps.length
      ? `；⚠ 源序号缺口 ${gaps.map(gap => `${gap.first_missing_sequence}-${gap.last_missing_sequence}`).join(",")}`
      : "";
    const resolvedNotice = resolvedGaps.length && !gaps.length
      ? `；✓ 源序号缺口已补齐 ${resolvedGaps.map(gap => `${gap.first_missing_sequence}-${gap.last_missing_sequence}`).join(",")}`
      : "";
    status.textContent = `最近 ${window_minutes} 分钟：${timeLabel(body.window_start)} 至 ${timeLabel(body.window_end)}；合约 ${contracts.size} 个；${body.truncated ? "仅返回窗口内最新若干点" : "窗口内数据已全部返回"}${gapNotice}${resolvedNotice}；发布 ${timeLabel(body.freshness?.published_at)}，接收 ${timeLabel(body.freshness?.received_at)}`;
  }
  function retryDelay(error) {
    if (Number.isFinite(error?.retryAfterMs)) return error.retryAfterMs;
    const base = Math.min(30000, 2000 * (2 ** Math.min(state.retryAttempt, 4)));
    return Math.round(base * (0.75 + Math.random() * 0.5));
  }
  // The snapshot stream publishes every 500ms and a steady poll now carries
  // ~21KB, so matching that cadence costs little and removes what had become
  // the largest term in the on-screen delay.
  function schedule(delay = 500) {
    window.clearTimeout(state.timer);
    if (!state.hidden) state.timer = window.setTimeout(() => load(), delay);
  }
  async function load() {
    if (state.hidden) return;
    if (state.inFlight) { state.pendingReload = true; state.controller?.abort(); return; }
    state.inFlight = true; state.controller?.abort(); state.controller = new AbortController();
    const generation = state.generation;
    try {
      // No cursor: the server recomputes the window from `now` on every poll,
      // so a cursor minted against the previous window can only mismatch.
      // `since` is an absolute instant instead, so a steady poll carries only
      // the couple of seconds that are actually new.
      const query = new URLSearchParams({window_minutes: String(state.windowMinutes)});
      if (state.since) query.set("since", state.since);
      if (state.sourceRevision) query.set("revision", state.sourceRevision);
      const body = await MarketPage.request(`/api/v1/market/realtime?${query}`, {controller: state.controller});
      // A changed window/run may have aborted this request, but an intermediary
      // can still resolve it.  Its cursor and rows must not overwrite the newer
      // generation after the user selected another window.
      if (generation !== state.generation || state.hidden) return;
      render(body);
      login.hidden = true; state.retryAttempt = 0; schedule();
    }
    catch (error) {
      if (state.hidden) return;
      status.textContent = error.cause === "timeout" ? "实时请求超时；将退避重试。" : error.message;
      if (error.cause === "login" || error.cause === "forbidden") { login.hidden = false; return; }
      state.retryAttempt += 1; schedule(retryDelay(error));
    }
    finally {
      state.inFlight = false;
      if (state.pendingReload && !state.hidden) { state.pendingReload = false; load(); }
    }
  }
  document.addEventListener("visibilitychange", () => { state.hidden = document.hidden; if (state.hidden) {state.controller?.abort(); window.clearTimeout(state.timer);} else {load();} });
  window.addEventListener("pagehide", () => {window.clearTimeout(state.timer); state.controller?.abort(); state.charts.forEach((item) => item.chart?.remove());});
  login?.addEventListener("click", () => MarketPage.login());
  windowSelect?.addEventListener("change", () => {
    const next = Number(windowSelect.value);
    if (![5, 10, 15].includes(next)) return;
    // Retain card/chart instances while the replacement request is in flight:
    // removing them makes an already rendered eight-contract screen flash to
    // zero cards and lets stale vendor probes race the new window.  Clear only
    // cached rows/cursor; the accepted generation replaces every series.
    state.windowMinutes = next; state.generation += 1; state.rows.clear(); state.since = null;
    state.retryAttempt = 0; load();
  });
  try { await MarketPage.absorbLoginHandoff(); } catch (error) { status.textContent = error.message; }
  await load();
})();
