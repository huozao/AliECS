/* Fixed-window market screen: one bounded request, then cursor increments. */
(async () => {
  "use strict";
  const status = document.querySelector("#status"), root = document.querySelector("#contracts"), login = document.querySelector("#login"), windowSelect = document.querySelector("#window-minutes");
  const state = {cursor: null, since: null, timer: null, ageTimer: null, internationalAges: null, controller: null, inFlight: false, pendingReload: false, generation: 0, runId: null, charts: new Map(), rows: new Map(), hidden: document.hidden, retryAttempt: 0, windowMinutes: 5};
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const priceText = (value, digits = 2) => {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : "—";
  };
  const stamp = (row) => row.observed_at || row.captured_at || row.source_time;
  const point = (row) => { const time = Date.parse(stamp(row)) / 1000, value = Number(row.last_price); return Number.isFinite(time) && Number.isFinite(value) ? {time, value} : null; };
  const finite = (value) => value != null && value !== "" && Number.isFinite(Number(value));
  const seriesPoints = (rows, field) => [...new Map(rows.map((row) => {
    const time = Math.floor(Date.parse(stamp(row)) / 1000);
    return [time, Number.isFinite(time) && finite(row[field]) ? {time, value: Number(row[field])} : null];
  }).filter((entry) => entry[1])).values()].sort((a, b) => a.time - b.time);
  const ageValue = (source) => source?.age_seconds == null ? null : Number(source.age_seconds);
  const ageText = (source) => Number.isFinite(ageValue(source)) ? `${Math.max(0, ageValue(source)).toFixed(2)}s` : "—";
  const timeText = (value) => {
    const time = Date.parse(value || "");
    return Number.isFinite(time) ? new Date(time).toLocaleTimeString("zh-SG", {hour12: false, timeZone: "Asia/Singapore"}) : "—";
  };
  function updateInternationalAges() {
    if (!state.internationalAges) return;
    const elapsed = Math.max(0, (performance.now() - state.internationalAges.receivedAt) / 1000);
    for (const name of ["xau", "fx"]) {
      const node = document.querySelector(`#international-${name}-age`);
      const initial = state.internationalAges[name];
      const age = initial == null ? null : Math.max(0, initial + elapsed);
      node.textContent = age == null ? "—" : `${age.toFixed(2)}s`;
      node.classList.toggle("quote-age-stale", age != null && age > 1);
    }
    if (!state.hidden) state.ageTimer = window.setTimeout(updateInternationalAges, 100);
  }
  function renderLayers(body, quotes) {
    const latest = quotes[quotes.length - 1] || {}, sources = latest.sources || {}, international = body.international || {};
    const xau = sources.xau || sources.xauusd || {}, fx = sources.fx || sources.usdcnh || {};
    const intlXau = international.xauusd || xau, intlFx = international.usdcnh || fx;
    const intlReady = international.strategy_allowed === true || (Number.isFinite(Number(latest.international_price)) && Number(intlXau.age_seconds) <= 30 && Number(intlFx.age_seconds) <= 30);
    const freshQuotes = quotes.filter((quote) => {
      const sourceTime = Date.parse(quote.source_time || "");
      const age = (Date.now() - sourceTime) / 1000;
      return Number.isFinite(age) && age >= -5 && age <= 30;
    });
    const domesticReady = quotes.length > 0 && freshQuotes.length === quotes.length;
    const domesticAge = quotes.length ? Math.max(...quotes.map((quote) => (Date.now() - Date.parse(quote.source_time || "")) / 1000)) : null;
    const intlStatus = body.international?.status || (intlReady ? "READY｜国际参考可用" : "REFERENCE_BLOCKED｜国际参考不可用");
    const overlap = body.overlap?.status || (domesticReady && intlReady ? "READY｜存在有效重叠" : domesticReady ? "DOMESTIC_ONLY｜仅国内行情可用" : intlReady ? "INTERNATIONAL_ONLY｜等待国内行情" : "BLOCKED｜两层行情均待确认");
    const strategy = body.strategy?.status || (domesticReady && intlReady ? "MARKET_READY｜行情重叠可用，策略状态待核对" : "BLOCKED｜等待有效重叠行情");
    document.querySelector("#international-status").textContent = intlStatus;
    document.querySelector("#international-detail").innerHTML = `$${priceText(intlXau.price ?? latest.xauusd_usd_per_oz)}/oz · ¥${priceText(international.international_cny_per_g ?? latest.international_price)}/g <span class="quote-divider">|</span> XAU <span id="international-xau-age" class="quote-age">${ageText(intlXau)}</span> · FX <span id="international-fx-age" class="quote-age">${ageText(intlFx)}</span> <span class="quote-divider">|</span> ↻${esc(timeText(international.updated_at))}`;
    state.internationalAges = {xau: Number.isFinite(ageValue(intlXau)) ? Math.max(0, ageValue(intlXau)) : null,
                               fx: Number.isFinite(ageValue(intlFx)) ? Math.max(0, ageValue(intlFx)) : null,
                               receivedAt: performance.now()};
    updateInternationalAges();
    document.querySelector("#domestic-status").textContent = body.domestic?.status || (domesticReady ? "FRESH｜国内行情新鲜" : "STALE｜国内行情待确认");
    document.querySelector("#domestic-detail").textContent = `${freshQuotes.length}/${quotes.length} 个合约新鲜 · ${Number.isFinite(domesticAge) ? Math.max(0, domesticAge).toFixed(2) + "s" : "—"} · ${domesticReady ? "可参与行情计算" : "不参与新策略"}`;
    document.querySelector("#overlap-status").textContent = overlap;
    document.querySelector("#overlap-detail").textContent = body.overlap?.reason || (domesticReady && intlReady ? "国内与国际行情均有新鲜报价" : domesticReady ? "等待国际参考" : "等待国内行情新鲜报价");
    document.querySelector("#strategy-status").textContent = strategy;
    document.querySelector("#strategy-detail").textContent = body.strategy?.reason || (domesticReady && intlReady ? "行情已重叠；账户执行仍以服务端风控为准" : "只读采集继续，等待有效参考");
  }
  function renderHedgeCandidates(rows) {
    const node = document.querySelector("#hedge-candidates");
    if (!rows?.length) { node.textContent = "当前没有带目标腿上下文的候选；排序模块已启用，等待策略候选快照。"; return; }
    const items = rows.map((row) => `<tr><td>${esc(row.target_contract || "通用")}</td><td>${esc(row.contract || row.symbol || "—")}</td><td>${esc(row.rank ?? "—")}</td><td>${esc(row.activity_count_300s ?? "—")}</td><td>${esc(row.expected_net_cny ?? row.expected_cost_cny ?? "—")}</td><td>${esc(row.reason || row.status || "—")}</td></tr>`).join("");
    node.innerHTML = `<div class="table-wrap"><table><thead><tr><th>目标腿</th><th>候选合约</th><th>排序</th><th>300秒成交次数</th><th>预期收益/成本</th><th>资格状态</th></tr></thead><tbody>${items}</tbody></table></div>`;
  }
  function chartFor(contract) {
    let item = state.charts.get(contract); if (item) return item;
    const card = document.createElement("article"); card.className = "market-card realtime-contract-card"; card.dataset.contract = contract;
    card.innerHTML = `<div class="realtime-card-head"><h2>${esc(contract)}</h2><div class="last">—</div></div><div class="realtime-parallel"><div><div class="realtime-extremes"><span>秒内最高 <b data-extreme="high">—</b></span><span>秒内最低 <b data-extreme="low">—</b></span></div><div class="chart" aria-label="${esc(contract)} 成交与 I 价格带"></div></div><div class="five-price" aria-label="${esc(contract)} 模型价格带与盘口"><span class="sell"><small>上方卖挂单</small><b data-tag="sell">—</b></span><span class="upper"><small>I 价格带上边缘</small><b data-tag="upper">—</b></span><span class="current"><small>真实成交价</small><b data-tag="current">—</b></span><span class="center"><small>I 价格带中心</small><b data-tag="center">—</b></span><span class="lower"><small>I 价格带下边缘</small><b data-tag="lower">—</b></span><span class="buy"><small>下方买挂单</small><b data-tag="buy">—</b></span></div></div><div class="muted"></div>`;
    root.append(card); const node = card.querySelector(".chart");
    if (window.LightweightCharts) {
      const chart = LightweightCharts.createChart(node, {autoSize: true, layout: {background:{color:"transparent"}, textColor:"#91a1af", attributionLogo:false}, timeScale:{timeVisible:true,secondsVisible:true}, grid:{vertLines:{visible:false},horzLines:{color:"#243244"}}});
      const line = (color, options = {}) => chart.addSeries(LightweightCharts.LineSeries, {color, lineWidth:1, priceLineVisible:false, lastValueVisible:false, ...options});
      item = {card, chart, price:line("#ffffff", {lineWidth:2}), upper:line("#84b8ef"), center:line("#f6c85f", {lineStyle:2}), lower:line("#84b8ef")};
    } else item = {card};
    state.charts.set(contract, item); return item;
  }
  function updateChart(contract, data, quote, band, orders, ranking) {
    const item = chartFor(contract), record = state.rows.get(contract) || {quotes: new Map(), bands: new Map()};
    for (const row of data?.quotes || []) record.quotes.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    for (const row of data?.bands || []) record.bands.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    const left = Date.now() - state.windowMinutes * 60 * 1000;
    for (const [key,row] of record.quotes) if (Date.parse(stamp(row)) < left) record.quotes.delete(key);
    for (const [key,row] of record.bands) if (Date.parse(stamp(row)) < left) record.bands.delete(key);
    state.rows.set(contract, record);
    const quotes = [...record.quotes.values()].sort((a,b) => Date.parse(stamp(a))-Date.parse(stamp(b)));
    const bands = [...record.bands.values()].sort((a,b) => Date.parse(stamp(a))-Date.parse(stamp(b)));
    const latest = quote || quotes.at(-1) || {};
    const value = (field, source = latest) => finite(source?.[field]) ? Number(source[field]).toFixed(2) : "—";
    const order = orders.find((row) => row.contract === contract) || {};
    item.card.querySelector(".last").textContent = value("last_price");
    item.card.querySelector('[data-extreme="high"]').textContent = value("high", latest.ohlc);
    item.card.querySelector('[data-extreme="low"]').textContent = value("low", latest.ohlc);
    const tags = {sell: order.sell?.price, upper: band?.upper, current: latest.last_price, center: band?.center, lower: band?.lower, buy: order.buy?.price};
    Object.entries(tags).forEach(([kind, price]) => { item.card.querySelector(`[data-tag="${kind}"]`).textContent = finite(price) ? Number(price).toFixed(2) : "—"; });
    item.card.querySelector(".muted").textContent = `买 ${order.buy?.price ?? "—"}（${order.buy?.status ?? "—"}） · 卖 ${order.sell?.price ?? "—"}（${order.sell?.status ?? "—"}） · 源时刻 ${quote?.source_time || "—"}`;
    if (!item.chart) return;
    item.price.setData(seriesPoints(quotes, "last_price"));
    for (const [series, field] of [[item.upper,"upper"],[item.center,"center"],[item.lower,"lower"]]) series.setData(seriesPoints(bands, field));
  }
  function render(body) {
    const window_minutes = body.window_minutes || state.windowMinutes;
    const runChanged = Boolean(state.runId && body.run_id && state.runId !== body.run_id);
    if (body.reset || runChanged) { state.rows.clear(); state.cursor = null; state.since = null; }
    state.runId = body.run_id || state.runId;
    const quotes = new Map((body.quotes || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const bands = new Map((body.bands || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const contracts = new Set([...quotes.keys(), ...bands.keys(), ...Object.keys(body.series || {})]);
    renderLayers(body, [...quotes.values()]);
    renderHedgeCandidates(body.hedge_ranking || []);
    [...contracts].sort().forEach((contract) => updateChart(contract, body.series?.[contract], quotes.get(contract), bands.get(contract), body.orders || [], body.hedge_ranking || []));
    state.cursor = body.next_cursor || state.cursor;
    state.since = runChanged || body.truncated || body.orders_truncated ? null : body.next_since || state.since;
    const windowEnd = body.window_end || body.server_time;
    const windowStart = Number.isFinite(Date.parse(windowEnd)) ? new Date(Date.parse(windowEnd) - window_minutes * 60000).toISOString() : "—";
    status.textContent = `最近 ${window_minutes} 分钟：${windowStart} 至 ${windowEnd || "—"}；合约 ${contracts.size} 个；${body.truncated ? "已采样/截断" : "完整返回窗口内上限"}；发布 ${body.freshness?.published_at || "—"}，接收 ${body.freshness?.received_at || "—"}`;
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
    if (state.inFlight) { if (force) { state.pendingReload = true; state.controller?.abort(); } return; }
    if (state.hidden) return;
    state.inFlight = true; state.controller = new AbortController(); const generation = state.generation;
    try {
      const query = new URLSearchParams({window_minutes: String(state.windowMinutes)});
      if (!force && state.since) {
        query.set("since", state.since);
      } else if (!force && state.cursor) query.set("after", state.cursor);
      const body = await MarketPage.request(`/api/v1/market/realtime?${query}`, {controller: state.controller});
      if (generation !== state.generation || state.hidden) return;
      render(body);
      login.hidden = true; state.retryAttempt = 0; schedule();
    }
    catch (error) {
      if (state.hidden || generation !== state.generation) return;
      status.textContent = error.cause === "timeout" ? "实时请求超时；将退避重试。" : error.message;
      if (error.cause === "login" || error.cause === "forbidden") { login.hidden = false; return; }
      state.retryAttempt += 1; schedule(retryDelay(error));
    }
    finally { state.inFlight = false; if (state.pendingReload && !state.hidden) { state.pendingReload = false; load(true); } }
  }
  document.addEventListener("visibilitychange", () => { state.hidden = document.hidden; if (state.hidden) {state.controller?.abort(); window.clearTimeout(state.timer); window.clearTimeout(state.ageTimer); state.ageTimer = null;} else {updateInternationalAges(); load(true);} });
  window.addEventListener("pagehide", () => {window.clearTimeout(state.timer); window.clearTimeout(state.ageTimer); state.controller?.abort(); state.charts.forEach((item) => item.chart?.remove());});
  login?.addEventListener("click", () => MarketPage.login());
  windowSelect?.addEventListener("change", () => {
    const next = Number(windowSelect.value);
    if (![5, 10, 15].includes(next)) return;
    state.windowMinutes = next; state.generation += 1; state.cursor = null; state.since = null; state.rows.clear(); state.retryAttempt = 0; load(true);
  });
  try { await MarketPage.absorbLoginHandoff(); } catch (error) { status.textContent = error.message; }
  if (!state.hidden) state.ageTimer = window.setTimeout(updateInternationalAges, 100);
  await load(true);
})();
