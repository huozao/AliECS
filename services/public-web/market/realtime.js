/* Fixed-window market screen: one bounded request, then cursor increments. */
(async () => {
  "use strict";
  const status = document.querySelector("#status"), root = document.querySelector("#contracts"), login = document.querySelector("#login"), windowSelect = document.querySelector("#window-minutes");
  const state = {cursor: null, timer: null, controller: null, inFlight: false, charts: new Map(), rows: new Map(), hidden: document.hidden, retryAttempt: 0, windowMinutes: 5};
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const stamp = (row) => row.observed_at || row.captured_at || row.source_time;
  const point = (row) => { const time = Date.parse(stamp(row)) / 1000, value = Number(row.last_price); return Number.isFinite(time) && Number.isFinite(value) ? {time, value} : null; };
  function chartFor(contract) {
    let item = state.charts.get(contract); if (item) return item;
    const card = document.createElement("article"); card.className = "market-card realtime-contract-card"; card.dataset.contract = contract;
    card.innerHTML = `<div class="realtime-card-head"><h2>${esc(contract)}</h2><div class="last">—</div></div><div class="realtime-parallel"><div><div class="realtime-extremes"><span>秒内最高 <b data-extreme="high">—</b></span><span>秒内最低 <b data-extreme="low">—</b></span></div><div class="chart" aria-label="${esc(contract)} 成交与 I 价格带"></div></div><div class="five-price" aria-label="${esc(contract)} 模型价格带与盘口"><span class="sell"><small>上方卖挂单</small><b data-tag="sell">—</b></span><span class="upper"><small>I 价格带上边缘</small><b data-tag="upper">—</b></span><span class="current"><small>真实成交价</small><b data-tag="current">—</b></span><span class="lower"><small>I 价格带下边缘</small><b data-tag="lower">—</b></span><span class="buy"><small>下方买挂单</small><b data-tag="buy">—</b></span></div></div><div class="muted"></div><ol class="hedges"></ol>`;
    root.append(card); const node = card.querySelector(".chart");
    if (window.LightweightCharts) {
      const chart = LightweightCharts.createChart(node, {height: 160, autoSize: true, layout: {background:{color:"transparent"}, textColor:"#91a1af"}, grid:{vertLines:{visible:false},horzLines:{visible:false}}});
      item = {card, chart, price: chart.addSeries(LightweightCharts.LineSeries, {color:"#54d6bc"}), upper: chart.addSeries(LightweightCharts.LineSeries, {color:"#ef9a9a"}), center: chart.addSeries(LightweightCharts.LineSeries, {color:"#f6c85f"}), lower: chart.addSeries(LightweightCharts.LineSeries, {color:"#8cc7ff"})};
    } else item = {card};
    state.charts.set(contract, item); return item;
  }
  function updateChart(contract, data, quote, band, orders, ranking) {
    const item = chartFor(contract), record = state.rows.get(contract) || {quotes: new Map(), bands: new Map()};
    for (const row of data?.quotes || []) record.quotes.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    for (const row of data?.bands || []) record.bands.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    const left = Date.now() - 15 * 60 * 1000;
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
      return Number.isFinite(Number(candidate)) ? Number(candidate).toFixed(2) : "—";
    };
    const order = orders.find((row) => row.contract === contract) || {};
    item.card.querySelector(".last").textContent = value("last_price");
    item.card.querySelector('[data-extreme="high"]').textContent = value("high", latest?.ohlc);
    item.card.querySelector('[data-extreme="low"]').textContent = value("low", latest?.ohlc);
    const tags = {sell: order.sell?.price, upper: band?.upper, current: latest?.last_price, lower: band?.lower, buy: order.buy?.price};
    Object.entries(tags).forEach(([kind, price]) => { item.card.querySelector(`[data-tag="${kind}"]`).textContent = Number.isFinite(Number(price)) ? Number(price).toFixed(2) : "—"; });
    item.card.querySelector(".muted").textContent = `买 ${order.buy?.price ?? "—"}（${order.buy?.status ?? "—"}） · 卖 ${order.sell?.price ?? "—"}（${order.sell?.status ?? "—"}） · 源时刻 ${latest?.source_time || latest?.observed_at || "—"}`;
    item.card.querySelector(".hedges").innerHTML = (ranking || []).filter((row) => (row.target_contract || row.contract || row.symbol) === contract).slice(0,5).map((row) => `<li>${esc(row.contract || row.symbol || "无候选")}：${esc(row.rank ?? "—")} · ${esc(row.reason || row.status || "—")}</li>`).join("") || "<li>后端未给出该合约对冲候选</li>";
    if (!item.chart) return;
    item.price.setData(quotes.map(point).filter(Boolean));
    for (const [series, field] of [[item.upper,"upper"],[item.center,"center"],[item.lower,"lower"]]) series.setData(bands.map((row) => {const time=Date.parse(stamp(row))/1000,value=Number(row[field]);return Number.isFinite(time)&&Number.isFinite(value)?{time,value}:null;}).filter(Boolean));
  }
  function render(body) {
    const window_minutes = body.window_minutes || state.windowMinutes;
    // A new run stream, or a server-side reset, invalidates what we hold.
    if (body.reset || (body.run_id && body.run_id !== state.runId)) state.rows.clear();
    state.runId = body.run_id ?? state.runId;
    const quotes = new Map((body.quotes || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const bands = new Map((body.bands || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const contracts = new Set([...quotes.keys(), ...bands.keys(), ...Object.keys(body.series || {})]);
    [...contracts].sort().forEach((contract) => updateChart(contract, body.series?.[contract], quotes.get(contract), bands.get(contract), body.orders || [], body.hedge_ranking || []));
    // Watermark by parsed instant, not by string: an ISO stamp may or may not
    // carry microseconds, so lexicographic order is not reliable here.
    let watermark = state.since ? Date.parse(state.since) : -Infinity;
    for (const record of state.rows.values())
      for (const row of [...record.quotes.values(), ...record.bands.values()]) {
        const at = stamp(row), time = Date.parse(at);
        if (Number.isFinite(time) && time >= watermark) { watermark = time; state.since = at; }
      }
    status.textContent = `最近 ${window_minutes} 分钟：${body.window_start || "—"} 至 ${body.window_end || "—"}；合约 ${contracts.size} 个；${body.truncated ? "仅返回窗口内最新若干点" : "窗口内数据已全部返回"}；发布 ${body.freshness?.published_at || "—"}，接收 ${body.freshness?.received_at || "—"}`;
  }
  function retryDelay(error) {
    if (Number.isFinite(error?.retryAfterMs)) return error.retryAfterMs;
    const base = Math.min(30000, 2000 * (2 ** Math.min(state.retryAttempt, 4)));
    return Math.round(base * (0.75 + Math.random() * 0.5));
  }
  function schedule(delay = 2000) {
    window.clearTimeout(state.timer);
    if (!state.hidden) state.timer = window.setTimeout(() => load(), delay);
  }
  async function load() {
    if (state.inFlight || state.hidden) return; state.inFlight = true; state.controller?.abort(); state.controller = new AbortController();
    try {
      // No cursor: the server recomputes the window from `now` on every poll,
      // so a cursor minted against the previous window can only mismatch.
      // `since` is an absolute instant instead, so a steady poll carries only
      // the couple of seconds that are actually new.
      const query = new URLSearchParams({window_minutes: String(state.windowMinutes)});
      if (state.since) query.set("since", state.since);
      render(await MarketPage.request(`/api/v1/market/realtime?${query}`, {controller: state.controller}));
      login.hidden = true; state.retryAttempt = 0; schedule();
    }
    catch (error) {
      if (state.hidden) return;
      status.textContent = error.cause === "timeout" ? "实时请求超时；将退避重试。" : error.message;
      if (error.cause === "login" || error.cause === "forbidden") { login.hidden = false; return; }
      state.retryAttempt += 1; schedule(retryDelay(error));
    }
    finally { state.inFlight = false; }
  }
  document.addEventListener("visibilitychange", () => { state.hidden = document.hidden; if (state.hidden) {state.controller?.abort(); window.clearTimeout(state.timer);} else {load();} });
  window.addEventListener("pagehide", () => {window.clearTimeout(state.timer); state.controller?.abort(); state.charts.forEach((item) => item.chart?.remove());});
  login?.addEventListener("click", () => MarketPage.login());
  windowSelect?.addEventListener("change", () => {
    const next = Number(windowSelect.value);
    if (![5, 10, 15].includes(next)) return;
    state.windowMinutes = next; state.rows.clear(); state.since = null; state.retryAttempt = 0; load();
  });
  try { await MarketPage.absorbLoginHandoff(); } catch (error) { status.textContent = error.message; }
  await load();
})();
