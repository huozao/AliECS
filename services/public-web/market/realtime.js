/* Fixed-window market screen: one bounded request, then cursor increments. */
(async () => {
  "use strict";
  const status = document.querySelector("#status"), root = document.querySelector("#contracts"), login = document.querySelector("#login");
  const state = {cursor: null, timer: null, controller: null, inFlight: false, charts: new Map(), rows: new Map(), hidden: document.hidden, retryAttempt: 0};
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const stamp = (row) => row.observed_at || row.captured_at || row.source_time;
  const point = (row) => { const time = Date.parse(stamp(row)) / 1000, value = Number(row.last_price); return Number.isFinite(time) && Number.isFinite(value) ? {time, value} : null; };
  function chartFor(contract) {
    let item = state.charts.get(contract); if (item) return item;
    const card = document.createElement("article"); card.className = "card"; card.dataset.contract = contract;
    card.innerHTML = `<h2>${esc(contract)}</h2><div class="value">—</div><div class="chart" aria-label="${esc(contract)} 最近十五分钟成交与 I 价格带"></div><div class="muted"></div><ol class="hedges"></ol>`;
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
    item.card.querySelector(".value").textContent = Number.isFinite(Number(quote?.last_price)) ? Number(quote.last_price).toFixed(2) : "—";
    const order = orders.find((row) => row.contract === contract) || {};
    item.card.querySelector(".muted").textContent = `买 ${order.buy?.price ?? "—"}（${order.buy?.status ?? "—"}） · 卖 ${order.sell?.price ?? "—"}（${order.sell?.status ?? "—"}） · 源时刻 ${quote?.source_time || "—"}`;
    item.card.querySelector(".hedges").innerHTML = (ranking || []).filter((row) => (row.contract || row.symbol) === contract).slice(0,5).map((row) => `<li>${esc(row.contract || row.symbol)}：${esc(row.rank ?? "—")} · ${esc(row.reason || row.status || "—")}</li>`).join("") || "<li>后端未给出该合约对冲候选</li>";
    if (!item.chart) return;
    item.price.setData(quotes.map(point).filter(Boolean));
    for (const [series, field] of [[item.upper,"upper"],[item.center,"center"],[item.lower,"lower"]]) series.setData(bands.map((row) => {const time=Date.parse(stamp(row))/1000,value=Number(row[field]);return Number.isFinite(time)&&Number.isFinite(value)?{time,value}:null;}).filter(Boolean));
  }
  function render(body) {
    const window_minutes = body.window_minutes;
    const quotes = new Map((body.quotes || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const bands = new Map((body.bands || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const contracts = new Set([...quotes.keys(), ...bands.keys(), ...Object.keys(body.series || {})]);
    [...contracts].sort().forEach((contract) => updateChart(contract, body.series?.[contract], quotes.get(contract), bands.get(contract), body.orders || [], body.hedge_ranking || []));
    state.cursor = body.next_cursor || state.cursor;
    status.textContent = `最近 ${window_minutes} 分钟：${body.window_start || "—"} 至 ${body.window_end || "—"}；合约 ${contracts.size} 个；${body.truncated ? "已采样/截断" : "完整返回窗口内上限"}；发布 ${body.freshness?.published_at || "—"}，接收 ${body.freshness?.received_at || "—"}`;
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
  async function load(force = false) {
    if (state.inFlight || state.hidden) return; state.inFlight = true; state.controller?.abort(); state.controller = new AbortController();
    try {
      render(await MarketPage.request(`/api/v1/market/realtime${!force && state.cursor ? `?after=${encodeURIComponent(state.cursor)}` : ""}`, {controller: state.controller}));
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
  document.addEventListener("visibilitychange", () => { state.hidden = document.hidden; if (state.hidden) {state.controller?.abort(); window.clearTimeout(state.timer);} else {load(true);} });
  window.addEventListener("pagehide", () => {window.clearTimeout(state.timer); state.controller?.abort(); state.charts.forEach((item) => item.chart?.remove());});
  login?.addEventListener("click", () => MarketPage.login());
  try { await MarketPage.absorbLoginHandoff(); } catch (error) { status.textContent = error.message; }
  await load(true);
})();
