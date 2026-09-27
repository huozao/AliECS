/* Fixed-window market screen: one bounded request, then cursor increments. */
(async () => {
  "use strict";
  const status = document.querySelector("#status"), root = document.querySelector("#contracts"), login = document.querySelector("#login");
  const state = {cursor: null, timer: null, controller: null, inFlight: false, charts: new Map(), rows: new Map(), hidden: document.hidden, retryAttempt: 0};
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const priceText = (value, digits = 2) => {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : "—";
  };
  const stamp = (row) => row.observed_at || row.captured_at || row.source_time;
  const point = (row) => { const time = Date.parse(stamp(row)) / 1000, value = Number(row.last_price); return Number.isFinite(time) && Number.isFinite(value) ? {time, value} : null; };
  const ageText = (source) => {
    if (source?.age_seconds == null) return "年龄未知";
    const age = Number(source.age_seconds);
    return !Number.isFinite(age) ? "年龄未知" : age < 1 ? "<1秒前" : `${age.toFixed(1)} 秒前`;
  };
  const timeText = (value) => {
    const time = Date.parse(value || "");
    return Number.isFinite(time) ? new Date(time).toLocaleTimeString("zh-SG", {hour12: false}) : "时间未知";
  };
  function renderLayers(body, quotes) {
    const latest = quotes[quotes.length - 1] || {}, sources = latest.sources || {}, international = body.international || {};
    const xau = sources.xau || sources.xauusd || {}, fx = sources.fx || sources.usdcnh || {}, domestic = sources.domestic || {};
    const intlXau = international.xauusd || xau, intlFx = international.usdcnh || fx;
    const intlReady = international.strategy_allowed === true || (Number.isFinite(Number(latest.international_price)) && Number(intlXau.age_seconds) <= 30 && Number(intlFx.age_seconds) <= 30);
    const domesticReady = Number(domestic.age_seconds) <= 30;
    const intlStatus = body.international?.status || (intlReady ? "READY｜国际参考可用" : "REFERENCE_BLOCKED｜国际参考不可用");
    const overlap = body.overlap?.status || (domesticReady && intlReady ? "READY｜存在有效重叠" : domesticReady ? "DOMESTIC_ONLY｜仅国内行情可用" : intlReady ? "INTERNATIONAL_ONLY｜等待国内行情" : "BLOCKED｜两层行情均待确认");
    const strategy = body.strategy?.status || (domesticReady && intlReady ? "READY｜策略可运行" : "BLOCKED｜等待有效重叠行情");
    document.querySelector("#international-status").textContent = intlStatus;
    document.querySelector("#international-detail").textContent = `$${priceText(intlXau.price ?? latest.xauusd_usd_per_oz)} · ¥${priceText(international.international_cny_per_g ?? latest.international_price)}/克 · XAU ${ageText(intlXau)} · FX ${ageText(intlFx)} · 更新 ${timeText(international.updated_at)}`;
    document.querySelector("#domestic-status").textContent = body.domestic?.status || (domesticReady ? "FRESH｜国内行情新鲜" : "STALE｜国内行情待确认");
    document.querySelector("#domestic-detail").textContent = `${quotes.length} 个合约 · ${ageText(domestic)} · ${domesticReady ? "可参与策略计算" : "不参与新策略"}`;
    document.querySelector("#overlap-status").textContent = overlap;
    document.querySelector("#overlap-detail").textContent = body.overlap?.reason || (domesticReady && intlReady ? "国内与国际行情均有新鲜报价" : "国际参考不可用时阻断新策略");
    document.querySelector("#strategy-status").textContent = strategy;
    document.querySelector("#strategy-detail").textContent = body.strategy?.reason || (domesticReady && intlReady ? "新建策略信号允许" : "只读采集继续，等待有效参考");
  }
  function renderHedgeCandidates(rows) {
    const node = document.querySelector("#hedge-candidates");
    if (!rows?.length) { node.textContent = "当前没有带目标腿上下文的候选；排序模块已启用，等待策略候选快照。"; return; }
    const items = rows.map((row) => `<tr><td>${esc(row.target_contract || "通用")}</td><td>${esc(row.contract || row.symbol || "—")}</td><td>${esc(row.rank ?? "—")}</td><td>${esc(row.activity_count_300s ?? "—")}</td><td>${esc(row.expected_net_cny ?? row.expected_cost_cny ?? "—")}</td><td>${esc(row.reason || row.status || "—")}</td></tr>`).join("");
    node.innerHTML = `<div class="table-wrap"><table><thead><tr><th>目标腿</th><th>候选合约</th><th>排序</th><th>300秒成交次数</th><th>预期收益/成本</th><th>资格状态</th></tr></thead><tbody>${items}</tbody></table></div>`;
  }
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
    renderLayers(body, [...quotes.values()]);
    renderHedgeCandidates(body.hedge_ranking || []);
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
