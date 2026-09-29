/* Live market screen: a stream owns the hot path; bounded HTTP remains the explicit fallback. */
(async () => {
  "use strict";
  const status = document.querySelector("#status"), streamStatus = document.querySelector("#stream-status"), root = document.querySelector("#contracts"), login = document.querySelector("#login"), windowSelect = document.querySelector("#window-minutes");
  const state = {since: null, timer: null, headTimer: null, ageTimer: null, internationalAges: null, controller: null, inFlight: false, pendingReload: false, generation: 0, streamGeneration: 0, runId: null, charts: new Map(), rows: new Map(), hidden: document.hidden, retryAttempt: 0, windowMinutes: 5, streamReady: false, streamWaiting: false, streamStarting: false, streamTransport: null, streamStartPromise: null, streamUpdates: 0};
  const streamReducer = typeof GoldMarketRealtimeState !== "undefined" ? GoldMarketRealtimeState.create({
    expectedContracts: 8,
    onResync: ({reason}) => {
      clearStreamView();
      state.streamReady = false;
      state.streamWaiting = false;
      status.textContent = `实时流已暂停（${reason}），正在重新取得窗口…`;
      state.streamTransport?.bootstrap(`state:${reason}`);
    },
  }) : null;
  function setStreamStatus(text) { if (streamStatus) streamStatus.textContent = text; }
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const priceText = (value, digits = 2) => {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(digits) : "—";
  };
  const stamp = (row) => row.observed_at || row.captured_at || row.source_time;
  const point = (row) => { const time = Date.parse(stamp(row)) / 1000, value = Number(row.last_price); return Number.isFinite(time) && Number.isFinite(value) ? {time, value} : null; };
  const finite = (value) => value != null && value !== "" && Number.isFinite(Number(value));
  const seriesPoints = (rows, field) => [...new Map(rows.map((row) => {
    const time = row.bucket_start_ms != null ? Number(row.bucket_start_ms) / 1000 : Date.parse(stamp(row)) / 1000;
    return [time, Number.isFinite(time) && finite(row[field]) ? {time, value: Number(row[field])} : null];
  }).filter((entry) => entry[1])).values()].sort((a, b) => a.time - b.time);
  const extractOrderPrice = (row, side) => {
    if (!row) return null;
    const orders = Array.isArray(row.orders) ? row.orders : (row.order ? [row.order] : []);
    for (const order of orders) {
      const orderSide = String(order.side || order.direction || "").toLowerCase();
      if (orderSide.includes(side)) {
        const price = order.price ?? order.price_cny_per_g;
        if (finite(price)) return Number(price);
      }
    }
    return null;
  };
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
    if (!state.hidden) state.ageTimer = window.setTimeout(updateInternationalAges, 1000);
  }
  function renderLayers(body, quotes) {
    const latest = quotes[quotes.length - 1] || {}, sources = latest.sources || {}, international = body.international || {};
    const xau = sources.xau || sources.xauusd || {}, fx = sources.fx || sources.usdcnh || {};
    const intlXau = international.xauusd || xau, intlFx = international.usdcnh || fx;
    const intlReady = international.strategy_allowed === true || (Number.isFinite(Number(latest.international_price)) && Number(intlXau.age_seconds) <= 30 && Number(intlFx.age_seconds) <= 30);
    const domesticReady = body.domestic?.strategy_quotes_allowed ?? (quotes.length > 0);
    const domesticAge = quotes.length ? Math.min(...quotes.map((quote) => (Date.now() - Date.parse(quote.source_time || quote.observed_at || "")) / 1000).filter(Number.isFinite)) : null;
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
    document.querySelector("#domestic-detail").textContent = `${quotes.length}/${quotes.length} 个合约已接入 · ${Number.isFinite(domesticAge) ? Math.max(0, domesticAge).toFixed(2) + "s" : "—"} · ${domesticReady ? "可参与行情计算" : "不参与新策略"}`;
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
      const stepLine = (color, options = {}) => chart.addSeries(LightweightCharts.LineSeries, {
        color, lineWidth: 1, lineType: LightweightCharts.LineType?.WithSteps ?? 1,
        priceLineVisible: false, lastValueVisible: false, ...options
      });
      item = {
        card, chart,
        sellOrder: stepLine("#FFC772"),
        upper: line("#84b8ef"),
        price: line("#ffffff", {lineWidth:2}),
        center: line("#f6c85f", {lineStyle:2}),
        lower: line("#84b8ef"),
        buyOrder: stepLine("#F0AA70"),
        lastTimes: {sellOrder: null, upper: null, price: null, center: null, lower: null, buyOrder: null}
      };
    } else item = {card};
    state.charts.set(contract, item); return item;
  }
  function updateChart(contract, data, quote, band, orders, ranking) {
    const item = chartFor(contract), record = state.rows.get(contract) || {quotes: new Map(), bands: new Map()};
    const order = orders.find((row) => row.contract === contract) || {};
    for (const row of data?.quotes || []) {
      if (row.buy_order_price == null) row.buy_order_price = extractOrderPrice(row, "buy") ?? (finite(order.buy?.price) ? Number(order.buy.price) : null);
      if (row.sell_order_price == null) row.sell_order_price = extractOrderPrice(row, "sell") ?? (finite(order.sell?.price) ? Number(order.sell.price) : null);
      record.quotes.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    }
    for (const row of data?.bands || []) record.bands.set(`${stamp(row)}|${row.snapshot_sequence || ""}`, row);
    let maxDataTime = 0;
    for (const row of record.quotes.values()) {
      const t = Date.parse(stamp(row));
      if (Number.isFinite(t) && t > maxDataTime) maxDataTime = t;
    }
    for (const row of record.bands.values()) {
      const t = Date.parse(stamp(row));
      if (Number.isFinite(t) && t > maxDataTime) maxDataTime = t;
    }
    const windowMs = state.windowMinutes * 60 * 1000;
    const now = Date.now();
    const anchorTime = (maxDataTime > 0 && (now - maxDataTime > windowMs)) ? maxDataTime : now;
    const left = anchorTime - windowMs;
    for (const [key,row] of record.quotes) if (Date.parse(stamp(row)) < left) record.quotes.delete(key);
    for (const [key,row] of record.bands) if (Date.parse(stamp(row)) < left) record.bands.delete(key);
    state.rows.set(contract, record);
    const quotes = [...record.quotes.values()].sort((a,b) => Date.parse(stamp(a))-Date.parse(stamp(b)));
    const bands = [...record.bands.values()].sort((a,b) => Date.parse(stamp(a))-Date.parse(stamp(b)));
    const latest = quote || quotes.at(-1) || {};
    const value = (field, source = latest) => finite(source?.[field]) ? Number(source[field]).toFixed(2) : "—";
    item.card.querySelector(".last").textContent = value("last_price");
    item.card.querySelector('[data-extreme="high"]').textContent = value("high", latest.ohlc);
    item.card.querySelector('[data-extreme="low"]').textContent = value("low", latest.ohlc);
    const tags = {sell: order.sell?.price, upper: band?.upper, current: latest.last_price, center: band?.center, lower: band?.lower, buy: order.buy?.price};
    Object.entries(tags).forEach(([kind, price]) => { item.card.querySelector(`[data-tag="${kind}"]`).textContent = finite(price) ? Number(price).toFixed(2) : "—"; });
    item.card.querySelector(".muted").textContent = `买 ${order.buy?.price ?? "—"}（${order.buy?.status ?? "—"}） · 卖 ${order.sell?.price ?? "—"}（${order.sell?.status ?? "—"}） · 源时刻 ${quote?.source_time || "—"}`;
    if (!item.chart) return;
    const pricePoints = seriesPoints(quotes, "last_price");
    item.price.setData(pricePoints);
    item.lastTimes.price = pricePoints.at(-1)?.time ?? null;
    const sellPoints = seriesPoints(quotes, "sell_order_price");
    if (item.sellOrder) { item.sellOrder.setData(sellPoints); item.lastTimes.sellOrder = sellPoints.at(-1)?.time ?? null; }
    const buyPoints = seriesPoints(quotes, "buy_order_price");
    if (item.buyOrder) { item.buyOrder.setData(buyPoints); item.lastTimes.buyOrder = buyPoints.at(-1)?.time ?? null; }
    for (const [series, field, name] of [[item.upper,"upper","upper"],[item.center,"center","center"],[item.lower,"lower","lower"]]) {
      const points = seriesPoints(bands, field);
      series.setData(points);
      item.lastTimes[name] = points.at(-1)?.time ?? null;
    }
  }
  function clearStreamView() {
    state.rows.clear();
    state.charts.forEach((item) => {
      item.price?.setData([]);
      item.sellOrder?.setData([]);
      item.upper?.setData([]);
      item.center?.setData([]);
      item.lower?.setData([]);
      item.buyOrder?.setData([]);
      item.lastTimes = {sellOrder: null, upper: null, price: null, center: null, lower: null, buyOrder: null};
    });
  }
  function streamIso(value) {
    const time = Number(value);
    return Number.isFinite(time) ? new Date(time).toISOString() : "";
  }
  function streamOrderRow(event) {
    const result = {contract: event.contract, buy: null, sell: null};
    for (const order of Array.isArray(event.orders) ? event.orders : []) {
      const side = String(order.side || order.direction || "").toLowerCase();
      const item = {price: order.price ?? order.price_cny_per_g,
        status: order.status || order.state || "—"};
      if (side.includes("buy") || side.includes("买")) result.buy = item;
      if (side.includes("sell") || side.includes("卖")) result.sell = item;
    }
    return result;
  }
  function streamProjection(contract) {
    if (!streamReducer) return {quotes: [], bands: [], latest: {}, band: {}, orders: []};
    const parts = streamReducer.seriesFor(contract);
    const quotes = [], bands = [];
    for (const part of parts) {
      const event = part.event;
      const source = streamIso(event.source_time_ms);
      const observed = streamIso(event.observed_at_ms);
      const ohlc = event.ohlc && typeof event.ohlc === "object" ? event.ohlc : {};
      const buyPrice = extractOrderPrice(event, "buy");
      const sellPrice = extractOrderPrice(event, "sell");
      quotes.push({contract, source_time: source, observed_at: observed, captured_at: observed,
        bucket_start_ms: event.bucket_start_ms, snapshot_sequence: event.source_sequence,
        last_price: event.price, ohlc: {high: ohlc.high, low: ohlc.low, close: ohlc.close,
          bucket_start: streamIso(event.bucket_start_ms)}, orders: event.orders || [],
        buy_order_price: buyPrice, sell_order_price: sellPrice});
      bands.push({contract, source_time: source, observed_at: observed,
        bucket_start_ms: event.bucket_start_ms, snapshot_sequence: event.source_sequence,
        upper: event.band?.upper, center: event.band?.center, lower: event.band?.lower});
    }
    const latest = quotes.at(-1) || {};
    const band = bands.at(-1) || {};
    const event = parts.at(-1)?.event;
    return {quotes, bands, latest, band, orders: event ? [streamOrderRow(event)] : []};
  }
  function renderStreamSnapshot(snapshot) {
    const result = streamReducer?.install(snapshot);
    if (!result || result.action !== "bootstrap") return false;
    clearStreamView();
    const contracts = new Set(streamReducer.eventsFor().map((event) => event.contract));
    for (const contract of [...contracts].sort()) {
      const projection = streamProjection(contract);
      updateChart(contract, {quotes: projection.quotes, bands: projection.bands},
        projection.latest, projection.band, projection.orders, []);
    }
    const meta = streamReducer.metadata();
    state.runId = meta.run_id || state.runId;
    state.streamReady = meta.continuous;
    state.streamWaiting = meta.gap_reason === "WAITING_FOR_SOURCE";
    if (state.streamWaiting) {
      setStreamStatus("频道已订阅；等待首条行情");
      window.clearTimeout(state.timer);
    }
    status.textContent = `消息流已连接；窗口 ${meta.window_minutes} 分钟；源序号 ${meta.source_sequence}；${meta.window_complete ? "完整窗口" : "窗口预热中，暂不宣称连续"}`;
    return true;
  }
  function renderStreamEvent(event) {
    const result = streamReducer?.receive(event);
    if (!result || result.action !== "applied") return result;
    const projection = streamProjection(event.contract);
    const item = chartFor(event.contract);
    const latest = projection.latest, band = projection.band, order = projection.orders[0] || {};
    item.card.querySelector(".last").textContent = priceText(latest.last_price);
    item.card.querySelector('[data-extreme="high"]').textContent = priceText(latest.ohlc?.high);
    item.card.querySelector('[data-extreme="low"]').textContent = priceText(latest.ohlc?.low);
    const tags = {sell: order.sell?.price, upper: band.upper, current: latest.last_price,
      center: band.center, lower: band.lower, buy: order.buy?.price};
    Object.entries(tags).forEach(([kind, price]) => {
      item.card.querySelector(`[data-tag="${kind}"]`).textContent = finite(price) ? Number(price).toFixed(2) : "—";
    });
    item.card.querySelector(".muted").textContent = `买 ${order.buy?.price ?? "—"}（${order.buy?.status ?? "—"}） · 卖 ${order.sell?.price ?? "—"}（${order.sell?.status ?? "—"}） · 源时刻 ${latest.source_time || "—"}`;
    if (!item.chart) return result;
    const time = Number(event.bucket_start_ms) / 1000;
    const points = [
      [item.sellOrder, "sellOrder", order.sell?.price],
      [item.upper, "upper", band.upper],
      [item.price, "price", event.price],
      [item.center, "center", band.center],
      [item.lower, "lower", band.lower],
      [item.buyOrder, "buyOrder", order.buy?.price]
    ];
    for (const [series, name, value] of points) {
      if (!series || !finite(value)) continue;
      const oldTime = item.lastTimes[name];
      if (oldTime == null || time >= oldTime) {
        series.update({time, value: Number(value)});
        item.lastTimes[name] = time;
      } else {
        const fields = name === "price" ? "last_price" : (name === "sellOrder" ? "sell_order_price" : (name === "buyOrder" ? "buy_order_price" : name));
        series.setData(seriesPoints(name === "price" || name.includes("Order") ? projection.quotes : projection.bands, fields));
        item.lastTimes[name] = time;
      }
    }
    state.streamUpdates = (state.streamUpdates || 0) + 1;
    if (state.streamUpdates % 120 === 0) {
      updateChart(event.contract, {quotes: projection.quotes, bands: projection.bands},
        latest, band, projection.orders, []);
    }
    const meta = streamReducer.metadata();
    state.streamReady = meta.continuous;
    status.textContent = `消息流已连接；窗口 ${meta.window_minutes} 分钟；源序号 ${meta.source_sequence}；${meta.window_complete ? "完整窗口" : "窗口预热中，暂不宣称连续"}`;
    return result;
  }
  function renderHead(body) {
    if (!body || state.hidden) return;
    const quotes = (body.quotes || []).filter((row) => row.contract);
    renderLayers(body, quotes);
    renderHedgeCandidates(body.hedge_ranking || []);
  }
  function render(body) {
    const window_minutes = body.window_minutes || state.windowMinutes;
    const runChanged = Boolean(state.runId && body.run_id && state.runId !== body.run_id);
    if (body.reset || runChanged) { state.rows.clear(); state.since = null; }
    state.runId = body.run_id || state.runId;
    const quotes = new Map((body.quotes || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const bands = new Map((body.bands || []).filter((row) => row.contract).map((row) => [row.contract,row]));
    const contracts = new Set([...quotes.keys(), ...bands.keys(), ...Object.keys(body.series || {})]);
    renderLayers(body, [...quotes.values()]);
    renderHedgeCandidates(body.hedge_ranking || []);
    if (state.streamReady) {
      state.since = null;
      status.textContent = `消息流已连接；状态快照 ${body.freshness?.received_at || body.server_time || "—"}；${contracts.size} 个合约由消息流绘制`;
      return;
    }
    [...contracts].sort().forEach((contract) => updateChart(contract, body.series?.[contract], quotes.get(contract), bands.get(contract), body.orders || [], body.hedge_ranking || []));
    state.since = runChanged || body.truncated || body.orders_truncated ? null : body.next_since || state.since;
    const windowEnd = body.window_end || body.server_time;
    const windowStart = Number.isFinite(Date.parse(windowEnd)) ? new Date(Date.parse(windowEnd) - window_minutes * 60000).toISOString() : "—";
    status.textContent = `最近 ${window_minutes} 分钟：${windowStart} 至 ${windowEnd || "—"}；合约 ${contracts.size} 个；${body.truncated ? "已采样/截断" : "完整返回窗口内上限"}；发布 ${body.freshness?.published_at || "—"}，接收 ${body.freshness?.received_at || "—"}`;
  }
  function stopStream(reason = "stopped") {
    state.streamGeneration += 1;
    state.streamTransport?.stop();
    state.streamTransport = null;
    state.streamStartPromise = null;
    state.streamStarting = false;
    state.streamReady = false;
    state.streamWaiting = false;
    streamReducer?.reset();
    clearStreamView();
    setStreamStatus(`消息流${reason}；使用 HTTP 窗口回退`);
  }
  function streamStatusText(update) {
    const labels = {
      token: "正在取得消息流凭据…", connecting: "消息流连接中…", connected: "消息流已连接，等待频道…",
      subscribed: "频道已订阅，正在取得一次完整窗口…", bootstrapping: "消息流窗口同步中…",
      disconnected: "消息流断开；等待重连或 HTTP 回退", gap: `消息流有缺口（${update.reason || "UNKNOWN"}）`,
      error: `消息流错误：${update.error?.message || "UNKNOWN"}`,
    };
    return labels[update.state] || `消息流：${update.state || "UNKNOWN"}`;
  }
  function startStream() {
    if (state.hidden || typeof GoldMarketRealtimeStream === "undefined"
        || typeof GoldMarketRealtimeStream.create !== "function") {
      setStreamStatus("消息流未加载；使用 HTTP 窗口回退");
      return Promise.resolve(false);
    }
    if (state.streamStartPromise) return state.streamStartPromise;
    if (state.streamTransport) state.streamTransport.stop();
    const generation = ++state.streamGeneration;
    const transport = GoldMarketRealtimeStream.create({
      channel: "gold:market",
      fetchToken: () => MarketPage.request("/api/v1/market/stream-token"),
      fetchBootstrap: () => MarketPage.request(`/api/v1/market/bootstrap?window_minutes=${state.windowMinutes}`),
      onSnapshot: (snapshot) => { if (generation === state.streamGeneration) renderStreamSnapshot(snapshot); },
      onEvent: (event) => { if (generation === state.streamGeneration) renderStreamEvent(event); },
      onStatus: (update) => {
        if (generation !== state.streamGeneration) return;
        setStreamStatus(streamStatusText(update));
        if (update.state === "error" && (update.error?.cause === "login" || update.error?.cause === "forbidden")) login.hidden = false;
      },
      resync: () => { if (generation === state.streamGeneration) { state.streamReady = false; load(true); } },
    });
    state.streamTransport = transport;
    state.streamStarting = true;
    const promise = transport.start().then(() => {
      if (generation !== state.streamGeneration) return false;
      state.streamStarting = false;
      login.hidden = true;
      return state.streamReady || state.streamWaiting;
    }).catch((error) => {
      if (generation !== state.streamGeneration) return false;
      state.streamStarting = false;
      state.streamReady = false;
      state.streamWaiting = false;
      setStreamStatus(`消息流不可用：${error.message || "UNKNOWN"}；使用 HTTP 窗口回退`);
      load(true);
      return false;
    }).finally(() => {
      if (generation === state.streamGeneration) state.streamStartPromise = null;
    });
    state.streamStartPromise = promise;
    return promise;
  }
  async function refreshHead() {
    if (state.hidden) return;
    try {
      const head = await MarketPage.request("/api/v1/market/latest");
      if (!state.hidden) { renderHead(head); login.hidden = true; }
    } catch (error) {
      if (error.cause === "login" || error.cause === "forbidden") login.hidden = false;
    } finally {
      window.clearTimeout(state.headTimer);
      if (!state.hidden) state.headTimer = window.setTimeout(refreshHead, 5000);
    }
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
    if (state.streamWaiting) return;
    if (state.streamReady) { void refreshHead(); return; }
    if (state.inFlight) { if (force) { state.pendingReload = true; state.controller?.abort(); } return; }
    if (state.hidden) return;
    state.inFlight = true; state.controller = new AbortController(); const generation = state.generation;
    try {
      const query = new URLSearchParams({window_minutes: String(state.windowMinutes)});
      if (!force && state.since) query.set("since", state.since);
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
  document.addEventListener("visibilitychange", () => {
    state.hidden = document.hidden;
    if (state.hidden) {
      state.controller?.abort(); window.clearTimeout(state.timer); window.clearTimeout(state.headTimer);
      window.clearTimeout(state.ageTimer); state.ageTimer = null; stopStream("已暂停");
    } else {
      updateInternationalAges(); void startStream().then((available) => { if (!available) load(true); }); void refreshHead();
    }
  });
  window.addEventListener("pagehide", () => {window.clearTimeout(state.timer); window.clearTimeout(state.headTimer); window.clearTimeout(state.ageTimer); state.controller?.abort(); stopStream("已关闭"); state.charts.forEach((item) => item.chart?.remove());});
  login?.addEventListener("click", () => MarketPage.login());
  windowSelect?.addEventListener("change", () => {
    const next = Number(windowSelect.value);
    if (![5, 10, 15].includes(next)) return;
    state.windowMinutes = next; state.generation += 1; state.since = null; state.rows.clear(); state.retryAttempt = 0;
    if (state.streamTransport) stopStream("窗口切换");
    void startStream().then((available) => { if (!available) load(true); });
  });
  try { await MarketPage.absorbLoginHandoff(); } catch (error) { status.textContent = error.message; }
  if (!state.hidden) state.ageTimer = window.setTimeout(updateInternationalAges, 1000);
  if (!state.hidden) {
    void refreshHead();
    const streamAvailable = await startStream();
    if (!streamAvailable && !state.hidden) await load(true);
  }
})();
