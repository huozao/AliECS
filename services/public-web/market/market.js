/* V6 market review client: bounded, read-only evidence browsing. */
(function () {
  "use strict";

  const API = "/api/v1/market";
  const ANNOTATIONS_API = "/api/v1/market/annotations";
  const observation = window.MarketObservation;
  const state = {
    generation: 0,
    seriesLoading: false,
    seriesSymbol: null,
    events: [],
    quotes: [],
    bands: [],
    snapshotQuotes: [],
    snapshotBands: [],
    snapshot: null,
    position: null,
    positionDirty: null,
    selected: null,
    follow: true,
    timer: null,
    timerCount: 0,
    chartCount: 0,
    chartData: [],
    eventAfter: 0,
    eventHasMore: false,
    seriesAfter: null,
    seriesHasMore: false,
    historyWindow: null,
    runId: null,
    annotations: [],
    alertBaseline: null,
    unreadAlertIds: new Set(),
    cursor: null,
    miniCharts: new Map(),
    focusCharts: new Map(),
    observedCandidateSymbol: null,
    volumeEnabled: true,
    alertEvents: [], alertAfter:0, alertHasMore:false,alertTruncated:false,alertLoading:false,playing: false, candidateFrozen: null, positions: new Map(),
  };

  const $ = (id) => document.getElementById(id);
  const text = (value) => String(value ?? "—");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const json = (value) => JSON.stringify(value ?? {}, null, 2);
  const num = (value, digits = 3) => value == null || value === "" || !Number.isFinite(Number(value))
    ? "—" : Number(value).toFixed(digits);
  const names={TARGET_FILL_CONFIRMED:"目标腿成交确认",ACCOUNT_TRADE:"账户成交",MODEL_RESULT:"模型收益结果",CANDIDATE_SNAPSHOT:"对冲候选快照",HEDGE_CANDIDATE_SNAPSHOT:"对冲候选快照",HEDGE_FILL:"对冲腿成交",TARGET_ORDER:"目标挂单",ORDER_SUBMITTED:"订单已发送",ORDER_EFFECTIVE:"订单已生效",ORDER_CANCELLED:"订单已撤销",POSITION_CLOSED:"持仓已关闭",CLOSED:"已平仓",UNRESOLVED:"未解决敞口",both:"双账对照",model:"模型账",account:"账户账",uncertain:"存疑",confirmed:"确认毛刺",excluded:"证据排除",BUY:"买入",SELL:"卖出",OPEN:"开仓",CLOSE:"平仓"};
  const codeName=value=>String(value || "—").includes("｜") ? String(value) : `${value || "UNKNOWN"}｜${names[value] || "未定义中文说明"}`;

  function authHeaders() {
    const token = window.AliECSAuth && window.AliECSAuth.getToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function get(path, init = {}) {
    const generation = state.generation;
    const response = await fetch(`${API}${path}`, { ...init, headers: { ...authHeaders(), ...(init.headers || {}) }, cache: "no-store" });
    const body = await response.json().catch(() => ({}));
    if (generation !== state.generation) throw new Error("请求已失效");
    if (response.status === 401 || response.status === 403) {
      clear();
      throw new Error("需要登录并取得市场查看权限");
    }
    if (!response.ok) throw new Error(body.detail || `接口返回 HTTP ${response.status}`);
    return body;
  }

  function resetRun() {
    state.miniCharts.forEach(item => item.chart.remove());
    state.miniCharts.clear();
    $("market-overview").innerHTML = "";
    state.focusCharts.forEach(item => item.chart.remove());
    state.focusCharts.clear();
    ["international-chart","main-chart","linked-chart","volume-chart"].forEach(id => {
      const node = $(id); if (node.__reviewChart) node.__reviewChart.chart.remove();
      delete node.__reviewChart;
    });
    state.generation += 1;
    Object.assign(state, { events: [], quotes: [], bands: [], snapshotQuotes: [], snapshotBands: [],
      snapshot: null, selected: null, position: null, positionDirty:null, annotations: [], chartData: [],
      eventAfter: 0, eventHasMore: false, seriesAfter: null, seriesHasMore: false,
      seriesSymbol: null, historyWindow: null, runId: null, alertBaseline: null, unreadAlertIds: new Set(), cursor: null, observedCandidateSymbol: null,
      alertEvents: [],alertAfter:0,alertHasMore:false,alertTruncated:false,alertLoading:false,playing:false,candidateFrozen:null,positions:new Map() });
    ["raw-event", "annotations", "selection", "event-gaps", "unresolved", "comparison", "annotation-status"].forEach((id) => { $(id).textContent = ""; });
    $("raw-evidence").querySelector("tbody").innerHTML = "";
    $("reason").value = "";
    $("contract").innerHTML = "";
    $("history-more").classList.add("hidden");
    $("events-more").classList.add("hidden");
    renderCandidates(null); renderPosition(null); renderEvents(); renderAlerts(); drawCharts(); renderInternational(null);
    ["evidence-summary","position-list","lifecycle"].forEach(id => $(id).textContent="");
  }

  function clear() {
    resetRun();
    state.follow = false;
    $("follow").textContent = "恢复跟随";
    $("demo-banner").classList.add("hidden");
    $("source-health").textContent = "—";
    $("review-status").textContent = "需要登录并取得市场查看权限";
    window.render?.({ rows: [], contract_count: 0, comparison: { available: false } });
  }

  function mergeRows(previous, incoming) {
    const rows = new Map();
    [...previous, ...incoming].forEach((row) => rows.set(`${row.contract}|${observation.stamp(row)}`, row));
    const sorted = [...rows.values()].sort((a, b) => Date.parse(observation.stamp(a)) - Date.parse(observation.stamp(b)));
    if (sorted.length>10000) $("event-gaps").textContent="展示缓存上限 10000 条观察记录；较早记录请读取历史窗口";
    return sorted.slice(-10000);
  }

  function sourceHealth(snapshot) {
    const rows = Array.isArray(snapshot && snapshot.quotes) ? snapshot.quotes : [];
    const ages = [];
    rows.forEach((row) => Object.values(row.sources || {}).forEach((source) => {
      if (source?.age_seconds != null && Number.isFinite(Number(source.age_seconds))) ages.push(Number(source.age_seconds));
    }));
    $("source-health").textContent = ages.length
      ? `源时钟年龄：${Math.max(...ages).toFixed(0)} 秒；保留源时刻与服务器接收时刻`
      : "源时钟年龄：—；缺少可用源时刻";
  }

  function latestRows(snapshot) {
    const quotes = observation.latest(observation.rowsAt(mergeRows(state.quotes,snapshot?.quotes || []),state));
    const bands = observation.latest(observation.rowsAt(mergeRows(state.bands,snapshot?.bands || []),state));
    return [...quotes.keys()].sort().map((contract) => ({ contract, quote: quotes.get(contract), band: validBand(bands.get(contract)) }));
  }

  function validBand(band) {
    return band?.fit_available===false ? {...band,center:null,upper:null,lower:null} : band || {};
  }

  function orderFor(row, side) {
    return observation.order(row,side);
  }

  function renderInternational(snapshot) {
    const sourceRows = observation.rowsAt(mergeRows(state.quotes,snapshot?.quotes || []),state);
    const byTime = new Map();
    sourceRows.forEach((row) => {
      const source = row.sources && row.sources.xau;
      if (!source || source.price == null || !source.source_time) {
        const time=Date.parse(observation.stamp(row))/1000;
        if(Number.isFinite(time)) byTime.set(time,{time});
        return;
      }
      const time = Date.parse(source.source_time) / 1000;
      const value = Number(source.price);
      if (Number.isFinite(time) && Number.isFinite(value)) byTime.set(time, { time, value, age: source.age_seconds });
    });
    const points = [...byTime.values()].sort((left, right) => left.time - right.time);
    const latest = points[points.length - 1];
    $("international-value").textContent = latest ? num(latest.value, 2) : "—";
    const values = points.map((point) => point.value).filter(value=>value!=null && Number.isFinite(value));
    const range = values.length ? ` · 窗口高/低 ${num(Math.max(...values), 2)} / ${num(Math.min(...values), 2)}` : "";
    $("international-quality").textContent = latest?.value!=null ? `源时刻 ${instant(new Date(latest.time * 1000).toISOString())} · 年龄 ${num(latest.age, 0)} 秒${range}` : "XAUUSD 源缺失；图中保留缺失边界";
    const target = $("international-chart");
    try {
      const library = window.LightweightCharts;
      if (library && !target.__reviewChart) {
        const chart = library.createChart(target, { layout: { background: { color: "transparent" }, textColor: "#91a1af" }, grid: { vertLines: { visible: false }, horzLines: { visible: false } }, autoSize:true, height: 68, rightPriceScale: { visible: false }, timeScale: { visible: false } });
        target.__reviewChart = { chart, series: chart.addSeries(library.LineSeries, { color: "#54D6BC", lineWidth: 2, priceLineVisible: false, lastValueVisible: false }) };
      }
      if (target.__reviewChart) {
        const data=points.map(({time,value})=>value==null?{time}:{time,value});
        observation.segmentedLine(target.__reviewChart,data,"#54D6BC");
        target.__reviewChart.series.setData(data);target.__reviewChart.chart.timeScale().fitContent();
      }
    } catch (_) {
      target.textContent = latest ? `XAUUSD ${num(latest.value, 2)}` : "—";
    }
  }

  function renderOverview(snapshot) {
    const target = $("market-overview");
    const rows = latestRows(snapshot);
    const available = [...new Set([...state.snapshotQuotes,...state.quotes].map(row=>row.contract))].sort();
    available.forEach(contract => {
      let card = [...target.children].find(node=>node.dataset.contract===contract);
      if (!card) {
        card=document.createElement("article"); card.className="market-card"; card.dataset.contract=contract;
        card.innerHTML=`<h3>${esc(contract)}</h3><div class="last"></div><div class="mini-with-tags"><div class="mini-chart" data-mini-contract="${esc(contract)}"></div><div class="five-price" aria-label="五价位置标签"></div></div><div class="order-history"></div>`;
        card.addEventListener("click",()=>{ $("contract").value=contract; state.observedCandidateSymbol=null; renderView(); });
        target.appendChild(card);
      }
      const quote = rows.find(row=>row.contract===contract)?.quote;
      const count = state.alertEvents.filter(event=>isNewUnreadAlert(event) && (event.payload?.symbol || event.payload?.target_symbol)===contract).length;
      card.classList.toggle("alerted",count>0);
      card.classList.toggle("source-stale",Boolean(quote?.quality_reasons?.length));
      card.querySelector("h3").innerHTML=`${esc(contract)}${count ? ` · <span class="bad-text">目标成交 ${count} 笔未读${state.alertHasMore || state.alertTruncated ? "（已加载）" : ""}</span>` : ""}`;
      card.querySelector(".last").textContent=num(quote?.last_price,2);
      card.querySelector(".order-history").textContent=quote ? `${orderFor(quote,"sell").status} / ${orderFor(quote,"buy").status} · 元/克${quote.quality_reasons?.length ? ` · ${quote.quality_reasons.join("；")}` : ""}` : "该游标之前暂无观察";
      card.querySelector(".order-history").title=card.querySelector(".order-history").textContent;
    });
    renderMiniCharts(rows);
  }

  function renderMainPriceTags(symbol) {
    const row=latestRows(state.snapshot).find(row=>row.contract===symbol) || {};
    observation.tags($("main-price-tags"),row.quote,row.band,$("coordinate").value==="price" ? $("main-chart").__reviewChart?.series : null,350);
  }

  function renderMiniCharts(rows) {
    [...document.querySelectorAll("[data-mini-contract]")].forEach(node => {
      const contract=node.dataset.miniContract;
      const {quote,band} = rows.find(row=>row.contract===contract) || {};
      try {
        const library = window.LightweightCharts;
        if (!library) throw new Error("no chart library");
        let item = state.miniCharts.get(contract);
        if (!item) {
          const chart = library.createChart(node, { layout: { background: { color: "transparent" }, textColor: "#91a1af" }, grid: { vertLines: { visible: false }, horzLines: { visible: false } }, autoSize:true, height:150, rightPriceScale: { visible: false }, timeScale: { visible: false } });
          item = { chart, series: chart.addSeries(library.LineSeries, { color: "#54D6BC", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }) };
          state.miniCharts.set(contract, item);
        }
        const data=chartRows(contract,"price");
        item.series.setData(dedupeChartRows(data).map(row=>row.close==null ? {time:Math.floor(Date.parse(row.time)/1000)} : {time:Math.floor(Date.parse(row.time)/1000),value:row.close}));
        observation.layers(item,data,state.volumeEnabled);
        observation.tags(node.nextElementSibling,quote,band,item.series,node.clientHeight);
      } catch (error) { $("review-status").textContent=`小图读取失败：${error.message}`; }
    });
  }

  function isNewUnreadAlert(event) {
    return state.alertBaseline !== null && event.sequence > state.alertBaseline && state.unreadAlertIds.has(event.event_id);
  }

  function renderAlerts() {
    const events = state.alertEvents.filter((event) => state.unreadAlertIds.has(event.event_id));
    const fresh = events.filter(isNewUnreadAlert);
    const history = events.filter(event=>!isNewUnreadAlert(event));
    const displayed = [...fresh,...history].slice(0,100);
    const target = $("alert-center");
    target.classList.toggle("hidden", !events.length && !state.alertHasMore && !state.alertTruncated);
    target.classList.toggle("history-only",fresh.length===0);
    target.innerHTML = `<strong>已加载 ${state.alertEvents.length} 条提醒中 ${events.length} 笔未读目标成交 · 本次新增未读 ${fresh.length} 笔 · ${state.alertBaseline===null ? "新旧边界待确认，暂存未读" : "历史未读"} ${history.length} 笔${state.cursor ? " · 实时新消息与历史视图独立" : ""}</strong>` +
      `${events.length>100 ? "<div>仅展示前 100 笔未读；读后继续显示后续提醒。</div>" : ""}${state.alertTruncated ? "<div>提醒展示缓存已截断到最近 5000 条；以上不是全库未读总数，原事件与敞口未删除。</div><button id='alerts-reload'>从头读取历史提醒</button>" : ""}` +
      displayed.map((event) => `<div><button type="button" data-open-alert="${esc(event.event_id)}">${isNewUnreadAlert(event) ? "🔴 本次新增" : state.alertBaseline===null ? "边界待确认" : "历史未读"} · ${event.payload?.account_id ? "账户成交" : "模型成交"}｜${esc(event.payload?.symbol || event.payload?.target_symbol || "未知合约")} · ${esc(event.event_id)}</button><button type="button" data-read-alert="${esc(event.event_id)}">标为已读</button></div>`).join("")+
      (state.alertHasMore ? '<button id="alerts-more">继续读取下一页提醒（每页最多 500 条）</button>' : "");
    target.querySelectorAll("[data-read-alert]").forEach((button) => button.addEventListener("click", () => markAlertRead(button.dataset.readAlert)));
    target.querySelectorAll("[data-open-alert]").forEach(button=>button.addEventListener("click",()=>selectEvent(events.find(event=>event.event_id===button.dataset.openAlert))));
    $("alerts-more")?.addEventListener("click",()=>loadAlertState().then(renderView).catch(error=>$("review-status").textContent=error.message));
    $("alerts-reload")?.addEventListener("click",()=>{state.alertAfter=0;state.alertEvents=[];state.unreadAlertIds.clear();state.alertTruncated=false;loadAlertState().then(renderView).catch(error=>$("review-status").textContent=error.message);});
  }

  async function markAlertRead(eventId) {
    if (!state.runId) return;
    try {
      await get(`/alerts/${encodeURIComponent(eventId)}/read?run_id=${encodeURIComponent(state.runId)}`, { method: "POST" });
    } catch (error) { $("review-status").textContent=`已读保存失败：${error.message}`; return; }
    state.unreadAlertIds.delete(eventId);
    renderAlerts(); renderOverview(state.snapshot || {}); renderEvents(); renderTransactions();
  }

  async function loadAlertState() {
    if (!state.runId || state.alertLoading) return;
    state.alertLoading=true;
    try {
      const page = await get(`/alerts?run_id=${encodeURIComponent(state.runId)}&after_sequence=${state.alertAfter}&limit=500`);
      const alerts=new Map(state.alertEvents.map(event=>[event.event_id,event]));
      const read=new Set(page.read_event_ids || []);
      (page.alerts || []).forEach(event=>{
        if (String(event.event_type).split("｜")[0]!=="TARGET_FILL_CONFIRMED") return;
        alerts.set(event.event_id,event);
        if(read.has(event.event_id)) state.unreadAlertIds.delete(event.event_id);else state.unreadAlertIds.add(event.event_id);
      });
      state.alertAfter=page.next_sequence ?? Math.max(state.alertAfter,...(page.alerts || []).map(event=>event.sequence || 0));
      state.alertHasMore=Boolean(page.has_more);
      state.alertTruncated ||= alerts.size>5000;
      state.alertEvents=[...alerts.values()].slice(-5000);
      const kept=new Set(state.alertEvents.map(event=>event.event_id));
      state.unreadAlertIds=new Set([...state.unreadAlertIds].filter(id=>kept.has(id)));
    } finally {
      state.alertLoading=false;
    }
  }

  function contracts(snapshot) {
    const values = [...new Set((snapshot.quotes || []).map((row) => row.contract).filter(Boolean))];
    const select = $("contract");
    const current = select.value;
    select.innerHTML = values.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
    if (values.includes(current)) select.value = current;
    return select.value || values[0] || "";
  }

  function chartRows(symbol, coordinate = $("coordinate").value) {
    const sourceQuotes = observation.monotonic(observation.rowsAt(mergeRows(state.quotes,state.snapshotQuotes),state));
    const sourceBands = observation.monotonic(observation.rowsAt(mergeRows(state.bands,state.snapshotBands),state)).filter(row=>row.contract===symbol);
    let bandIndex=0, band={};
    return sourceQuotes.filter((row) => row.contract === symbol).map((row) => {
      const at=Date.parse(observation.stamp(row));
      while(bandIndex<sourceBands.length && Date.parse(observation.stamp(sourceBands[bandIndex]))<=at) band=validBand(sourceBands[bandIndex++]);
      const ohlc = row.ohlc || {};
      const rawBase = coordinate === "spread" ? row.international_price : coordinate === "deviation" ? band.center : 0;
      const base = rawBase != null && rawBase !== "" && Number.isFinite(Number(rawBase)) ? Number(rawBase) : null;
      const value = (x) => x == null || !Number.isFinite(Number(x)) || base == null ? null : Number(x) - base;
      return {
        time: observation.stamp(row),
        open: value(ohlc.open ?? row.last_price), high: value(ohlc.high ?? row.last_price),
        low: value(ohlc.low ?? row.last_price), close: value(ohlc.close ?? row.last_price),
        source_time: row.source_time, quote: row, band,
      };
    });
  }

  function bandRows(rows, key) {
    const coordinate = $("coordinate").value;
    return dedupeChartRows(rows.map((row) => {
      const raw = row.band && row.band[key];
      if (raw == null || !Number.isFinite(Number(raw))) return null;
      let value = Number(raw);
      if (coordinate === "spread") {
        const international = row.quote && row.quote.international_price;
        if (international == null || !Number.isFinite(Number(international))) return null;
        value -= Number(international);
      } else if (coordinate === "deviation") {
        const center = row.band && row.band.center;
        if (center == null || !Number.isFinite(Number(center))) return null;
        value -= Number(center);
      }
      return { time: row.time, close: value };
    }).filter(Boolean)).filter((row) => row.close != null)
      .map((row) => ({ time: Math.floor(Date.parse(row.time) / 1000), value: row.close }));
  }

  function dedupeChartRows(rows) {
    const buckets = new Map();
    rows.forEach((row) => {
      const millis = Date.parse(row.time);
      if (!Number.isFinite(millis)) return;
      const key = Math.floor(millis / 1000);
      const old = buckets.get(key);
      if (!old) {
        buckets.set(key, { ...row, time: new Date(key * 1000).toISOString() });
        return;
      }
      const finite = (value) => value != null && Number.isFinite(Number(value));
      if (!finite(old.open) && finite(row.open)) old.open = row.open;
      const high = [old.high, row.high].filter(finite).map(Number);
      const low = [old.low, row.low].filter(finite).map(Number);
      old.high = high.length ? Math.max(...high) : null;
      old.low = low.length ? Math.min(...low) : null;
      if (finite(row.close)) old.close = row.close;
      old.quote = row.quote;
      old.band = row.band;
    });
    return [...buckets.values()].sort((left, right) => Date.parse(left.time) - Date.parse(right.time));
  }

  function drawCharts() {
    const symbol = $("contract").value;
    state.chartData = chartRows(symbol);
    renderMainPriceTags(symbol);
    $("axis-note").textContent = $("coordinate").value === "spread"
      ? "价差＝同一条源时刻记录的国内价格减国际折算价（元/克）"
      : $("coordinate").value === "deviation"
        ? "模型偏离＝同一条源时刻记录的国内价格减中心（元/克）"
        : "绝对价格（元/克）；缺失秒保持缺失，不插值";
    const main = $("main-chart");
    const volume = $("volume-chart");
    const linked = $("linked-chart");
    const display = dedupeChartRows(state.chartData).filter((row) => [row.open, row.high, row.low, row.close].every((value) => value != null && Number.isFinite(Number(value))))
      .map((row) => ({ ...row, time: Math.floor(Date.parse(row.time) / 1000) }));
    try {
      const library = window.LightweightCharts;
      if (library && !main.__reviewChart) {
        const options = { layout: { background: { color: "transparent" }, textColor: "#91a1af" }, grid: { vertLines: { color: "#2b3b4a" }, horzLines: { color: "#2b3b4a" } }, autoSize:true, height: 350 };
        const mainChart = library.createChart(main, options);
        const linkedChart = library.createChart(linked, { ...options, height: 220 });
        const volumeChart = library.createChart(volume, { ...options, height: 88, rightPriceScale: { visible: false }, timeScale: { visible: false } });
        main.__reviewChart = { chart: mainChart, series: mainChart.addSeries(library.CandlestickSeries, { upColor: "#54D6BC", downColor: "#54D6BC", borderVisible: false, wickUpColor: "#54D6BC", wickDownColor: "#54D6BC" }) };
        linked.__reviewChart = { chart: linkedChart, series: linkedChart.addSeries(library.LineSeries, { color: "#79b8ff", lineWidth: 2 }) };
        volume.__reviewChart = { chart: volumeChart, series: volumeChart.addSeries(library.HistogramSeries, { color: "#648eb7", priceFormat: { type: "volume" }, priceScaleId: "" }) };
        if (typeof library.createSeriesMarkers === "function") {
          main.__reviewChart.markers = library.createSeriesMarkers(main.__reviewChart.series, []);
        }
        mainChart.subscribeClick((param) => {
          if (!param || param.time == null) return;
          const event = state.events.find((item) => Math.floor(Date.parse(item.occurred_at || item.recorded_at || "") / 1000) === param.time);
          if (event) selectEvent(event);
        });
        mainChart.subscribeCrosshairMove((param) => {
          if (param && param.time != null) linkedChart.setCrosshairPosition(0, param.time, linked.__reviewChart.series);
        });
        linkedChart.subscribeCrosshairMove((param) => {
          if (param && param.time != null) mainChart.setCrosshairPosition(0, param.time, main.__reviewChart.series);
        });
        state.chartCount = 2;
      }
      if (main.__reviewChart) {
        main.__reviewChart.series.setData(display);
        volume.classList.toggle("hidden", !state.volumeEnabled);
        if (volume.__reviewChart) {
          const volumeRows = state.volumeEnabled ? observation.volumes(state.chartData) : [];
          volume.__reviewChart.series.setData(volumeRows);
          volume.__reviewChart.chart.timeScale().fitContent();
        }
        const markerMap = new Map();
        visibleEvents().forEach((event) => {
          const time = Math.floor(Date.parse(event.occurred_at || event.recorded_at || "") / 1000);
          if (!Number.isFinite(time)) return;
          const label = codeName(event.event_type).split("｜")[1];
          const labels = markerMap.get(time) || [];
          if (!labels.includes(label)) labels.push(label);
          markerMap.set(time, labels);
        });
        const markers = [...markerMap.entries()].sort((left, right) => left[0] - right[0]).map(([time, labels]) => ({
          time, position: "aboveBar", color: "#ffd166", shape: "circle", text: labels.join("/").slice(0, 24),
        }));
        if (main.__reviewChart.markers) main.__reviewChart.markers.setMarkers(markers);
        main.__reviewChart.chart.timeScale().fitContent();
        observation.layers(main.__reviewChart,state.chartData,false,$("coordinate").value);
        const otherSymbol = [...new Set((state.snapshotQuotes || state.quotes || []).map((row) => row.contract).filter((value) => value && value !== symbol))][0] || symbol;
        const linkedRows = dedupeChartRows(chartRows(otherSymbol)).filter((row) => row.close != null)
          .map((row) => ({ time: Math.floor(Date.parse(row.time) / 1000), value: row.close }));
        linked.__reviewChart.series.setData(linkedRows.length ? linkedRows : display.map((row) => ({ time: row.time, value: row.close })));
        linked.__reviewChart.chart.timeScale().fitContent();
        $("linked-legend").textContent = `同时间联动：${otherSymbol}`;
      } else throw new Error("chart library unavailable");
    } catch (error) {
      main.textContent = display.length ? display.map((row) => `${row.time} ${num(row.close)}`).join("\n") : "暂无图表数据";
      linked.textContent = display.length ? `联动合约：${symbol}` : "暂无联动数据";
      $("linked-legend").textContent = display.length ? `同时间联动：${symbol}` : "暂无联动数据";
      if (!state.chartCount) state.chartCount = 2;
    }
    renderFocusCharts();
    renderMainPriceTags(symbol);
  }

  function focusData(symbol) {
    if (!symbol) return [];
    return dedupeChartRows(chartRows(symbol,"price")).map(row=>row.close==null ? {time:Math.floor(Date.parse(row.time)/1000)} : {time:Math.floor(Date.parse(row.time)/1000),value:row.close});
  }

  function renderFocusCharts() {
    const targetSymbol = $("contract").value;
    const selectedPayload = candidateContext()?.payload || {};
    const candidates = Array.isArray(selectedPayload.candidates) ? selectedPayload.candidates : [];
    const selectedCandidate = candidates.find((candidate) => candidate.selected) || candidates.find((candidate) => candidate.contract === selectedPayload.selected_symbol || candidate.symbol === selectedPayload.selected_symbol);
    const visiblePositionEvents=(state.position?.events || []).filter(event=>!state.cursor || Date.parse(event.occurred_at)<=Date.parse(state.cursor));
    const actualHedge=visiblePositionEvents.find(event=>event.payload?.leg==="hedge")?.payload?.symbol;
    const hedgeSymbol = state.observedCandidateSymbol || selectedPayload.selected_symbol || selectedPayload.hedge_symbol || (selectedCandidate && (selectedCandidate.contract || selectedCandidate.symbol)) || actualHedge;
    const focus = [
      ["target-focus-chart", targetSymbol, "#54D6BC", "target-focus-label", `目标合约：${targetSymbol || "—"}`],
      ["hedge-focus-chart", hedgeSymbol, "#84B8EF", "hedge-focus-label", `观察候选：${hedgeSymbol || "—"} · 点击候选只切换观察图，不改变实际选择`],
    ];
    focus.forEach(([id, symbol, color, labelId, label]) => {
      const node = $(id);
      $(labelId).textContent = label;
      const data = focusData(symbol);
      try {
        const library = window.LightweightCharts;
        if (!library) throw new Error("no chart library");
        let item = state.focusCharts.get(id);
        if (!item) {
          const chart = library.createChart(node, { layout: { background: { color: "transparent" }, textColor: "#91a1af" }, grid: { vertLines: { color: "#2b3b4a" }, horzLines: { color: "#2b3b4a" } }, autoSize:true, height: 220 });
          item = { chart, series: chart.addSeries(library.LineSeries, { color, lineWidth: 2 }) };
          state.focusCharts.set(id, item);
        }
        item.series.setData(data);
        observation.layers(item,chartRows(symbol,"price"),state.volumeEnabled);
        item.chart.timeScale().fitContent();
      } catch (_) { node.textContent = data.length ? data.map((row) => `${instant(new Date(row.time * 1000).toISOString())} ${num(row.value, 2)}`).join("\n") : "暂无该合约的真实成交轨迹"; }
    });
    $("focus-time").textContent = `共同时间：${state.cursor ? instant(state.cursor) : "实时"}`;
    const preview=$("candidate-mode").value==="preview";
    $("preview-status").textContent = preview
      ? `只读预览 · ${selectedPayload.target_symbol || targetSymbol} · ${selectedPayload.target_side==="sell"?"卖":"买"} · 假设 ${num(selectedPayload.assumed_price_cny_per_g,2)} 元/克 × ${num(selectedPayload.quantity_lots,0)} 手 · ${instant(selectedPayload.as_of)} · ${selectedPayload.status || "尚未就绪"} · ${selectedPayload.reason_code || ""}`
      : `冻结历史决策 · ${candidates.length} 个候选 · ${selectedPayload.hedge_mode || "模式未发布"} · 不使用当前预览覆盖决策`;
  }

  function moveCursor(deltaSeconds) {
    const stamps = state.quotes.map((row) => Date.parse(observation.stamp(row))).filter(Number.isFinite).sort((a, b) => a - b);
    if (!stamps.length) return;
    const current = state.cursor ? Date.parse(state.cursor) : stamps[stamps.length - 1];
    const next = Math.max(stamps[0],Math.min(stamps.at(-1),current+deltaSeconds*1000));
    state.cursor = new Date(next).toISOString();
    if(state.playing && next===stamps.at(-1)) state.playing=false;
    state.follow = false;
    $("follow").textContent = "恢复跟随";
    state.candidateFrozen=null;
    renderView();
  }

  function renderEvents() {
    const target = $("events");
    const events = visibleEvents();
    target.innerHTML = events.length ? events.map((event) => {
      const selected = state.selected && state.selected.event_id === event.event_id ? " selected" : "";
      const alert = isNewUnreadAlert(event) ? " target-alert" : "";
      return `<button type="button" class="event-item${selected}${alert}" data-event-id="${esc(event.event_id)}">` +
        `${instant(event.occurred_at || event.recorded_at)} · ${esc(codeName(event.event_type))} · ${esc(event.position_id || "无持仓")}</button>`;
    }).join("") : '<span class="muted">暂无事件</span>';
    target.querySelectorAll("[data-event-id]").forEach((button) => button.addEventListener("click", () => {
      selectEvent(state.events.find((event) => event.event_id === button.dataset.eventId));
    }));
  }

  function visibleEvents() {
    const left = state.historyWindow ? Date.parse(state.historyWindow.start) : -Infinity;
    const right = Math.min(state.historyWindow ? Date.parse(state.historyWindow.end) : Infinity,state.cursor ? Date.parse(state.cursor) : Infinity);
    return state.events.filter((event) => {
      const at = Date.parse(event.occurred_at || event.recorded_at || "");
      return Number.isFinite(at) && at >= left && at <= right;
    });
  }

  function candidateContext() {
    if(state.candidateFrozen) return state.candidateFrozen;
    if($("candidate-mode").value==="preview") {
      const quote=latestRows(state.snapshot).find(row=>row.contract===$("contract").value)?.quote;
      const preview=(quote?.hedge_previews || []).find(item=>item.target_side===$("preview-side").value);
      return {payload:preview || {},occurred_at:preview?.as_of,preview:true};
    }
    const at=state.cursor ? Date.parse(state.cursor) : Infinity;
    const candidates=[...visibleEvents(),...(state.position?.decisions || [])]
      .filter(event=>Array.isArray(event.payload?.candidates) && Date.parse(event.occurred_at || event.recorded_at)<=at &&
        (!state.selected?.position_id || event.position_id===state.selected.position_id));
    return candidates.sort((a,b)=>Date.parse(a.occurred_at)-Date.parse(b.occurred_at)).at(-1) ||
      (state.selected?.payload?.candidates && Date.parse(state.selected.occurred_at)<=at ? state.selected : null);
  }

  function renderCandidates(event = candidateContext()) {
    const candidates = event && event.payload && Array.isArray(event.payload.candidates)
      ? event.payload.candidates : [];
    $("decision-label").textContent = event ? `${event.preview ? "只读预览" : "冻结决策"} ${text(event.payload?.decision_id)} · ${instant(event.occurred_at || event.payload?.as_of)}${state.candidateFrozen ? " · 排名已暂停" : ""}` : "该游标前尚无候选决策";
    $("candidates").querySelector("tbody").innerHTML = candidates.map((candidate) => {
      const contract = candidate.contract || candidate.symbol;
      const reasons = candidate.rejection_reasons || candidate.reasons || (candidate.reason_code ? [candidate.reason_code] : []);
      const cost = candidate.expected_cost_cny_per_pair ?? candidate.expected_cost_cny;
      const instruction = candidate.instruction || candidate.fill || (candidate.selected ? "SELECTED_BY_V6｜V6 已选" : "—");
      return `<tr data-candidate-contract="${esc(contract || "")}">
      <td>${esc(candidate.rank)} / ${esc(candidate.rank_change ?? "—")}</td><td>${esc(contract)}</td>
      <td>${candidate.eligible ? "合格" : "拒绝"}${candidate.selected ? " / 选中" : ""}</td>
      <td>${esc(reasons.join("；"))}</td>
      <td>${num(cost, 2)}</td><td>${num(candidate.theoretical_price, 4)}</td><td>${num(candidate.slippage_cny_per_g, 4)}</td>
      <td>${num(candidate.quote_age_seconds, 1)}</td><td>${esc(instruction)}<details><summary>方向 / 拟价 / 带位置 / 原始字段</summary><pre>${esc(json(candidate))}</pre></details></td>
    </tr>`;
    }).join("");
    $("candidates").querySelectorAll("[data-candidate-contract]").forEach((row) => row.addEventListener("click", () => {
      const candidate = candidates.find((item) => (item.contract || item.symbol) === row.dataset.candidateContract);
      if (!candidate) return;
      $("hedge-focus-label").textContent = `观察候选：${row.dataset.candidateContract} · 点击候选只切换观察图，不改变实际选择`;
      state.observedCandidateSymbol = row.dataset.candidateContract;
      renderFocusCharts();
    }));
  }

  function renderPosition(position) {
    state.position = position;
    if (!position) {
      $("position-detail").textContent = "选择事件后读取账户回报；未知收益显示“—”，不补成 0。";
      return;
    }
    position=observation.positionAt(position,state.cursor);
    const future=false;
    const account = position.account || {};
    const model = position.model || {};
    const ledger = $("ledger").value;
    const modelHtml = `<div><div class="label">模型账净收益</div><div class="money">${num(model.net_pnl_cny ?? model.net_profit_cny, 2)}</div></div>`;
    const accountHtml = `<div><div class="label">账户账实际净收益</div><div class="money">${num(account.actual_net_cny ?? account.actual_net_pnl_cny, 2)}</div></div>`;
    const pnlHtml = ledger === "model" ? modelHtml : ledger === "account" ? accountHtml : modelHtml + accountHtml;
    $("position-detail").innerHTML = `<div class="pnl">${pnlHtml}</div>` +
      `<div class="calculation">账本视图：${esc(codeName(ledger))}<br>` +
      `账户状态：${esc(codeName(account.status || (position.unresolved ? "UNRESOLVED｜待核对" : "CLOSED｜已平仓")))}<br>` +
      `原因：${esc((position.reasons || account.unresolved_reasons || []).map((reason) => reason.reason || reason).join("；") || "—")}</div>` +
      `<details><summary>两腿价格、数量、乘数及收益证据</summary>${Object.entries(account.legs || {}).map(([name,leg])=>`<div class="calculation">${name==="target"?"目标腿":"对冲腿"} · 已开 ${future?"—":num(leg.opened_volume,0)} / 已平 ${future?"—":num(leg.closed_volume,0)} / 剩余 ${future?"—":num(leg.remaining_volume,0)} 手 · 毛利 ${future?"—":num(leg.gross_profit_cny,2)} 元${(leg.fills || []).filter(event=>!state.cursor || Date.parse(event.occurred_at)<=Date.parse(state.cursor)).map(event=>`<div>${esc(event.payload?.symbol)} · ${esc(event.payload?.direction)}｜${event.payload?.direction==="BUY"?"买":"卖"} · ${esc(event.payload?.offset)}｜${event.payload?.offset==="OPEN"?"开仓":"平仓"} · ${num(event.payload?.price,2)} 元/克 × ${num(event.payload?.volume,0)} 手 × ${num(event.payload?.multiplier,0)} 克/手</div>`).join("")}</div>`).join("")}<div>模型固定成本 ${num(model.fixed_cost_cny,2)} 元 · 账户实际费用 ${num(account.actual_commission_cny,2)} 元 · 未知不补零</div><pre>${esc(json(model))}</pre></details>`;
  }

  function renderTransactions() {
    const groups=new Map();
    visibleEvents().filter(event=>event.position_id).forEach(event=>{
      const list=groups.get(event.position_id) || []; list.push(event); groups.set(event.position_id,list);
    });
    const mode=$("position-filter").value;
    $("position-list").innerHTML=[...groups.entries()].filter(([id,events])=> {
      const closed=observation.positionAt(state.positions.get(id),state.cursor)?.account?.status==="CLOSED";
      return mode==="all" || (mode==="unread" && events.some(event=>state.unreadAlertIds.has(event.event_id))) ||
        (mode==="closed" && closed) || (mode==="unresolved" && !closed);
    }).map(([id,events])=>`<button data-position="${esc(id)}">${esc(id)} · ${events.length} 个事件 · ${state.positions.has(id)?codeName(observation.positionAt(state.positions.get(id),state.cursor)?.account?.status):"账户状态待核对"}${events.some(event=>state.unreadAlertIds.has(event.event_id))?" · 未读":""}</button>`).join("") || "该游标前无符合条件的交易";
    $("position-list").querySelectorAll("[data-position]").forEach(button=>button.addEventListener("click",()=>selectEvent(groups.get(button.dataset.position).at(-1))));
    const lifecycle=(state.position?.events || groups.get(state.selected?.position_id) || []).filter(event=>!state.cursor || Date.parse(event.occurred_at)<=Date.parse(state.cursor));
    $("lifecycle").innerHTML=lifecycle.map(event=>`<button data-stage="${esc(event.event_id)}">${instant(event.occurred_at)} · ${esc(codeName(event.event_type))} · ${esc(event.order_id || "无订单编号")}</button>`).join("");
    $("lifecycle").querySelectorAll("[data-stage]").forEach(button=>button.addEventListener("click",()=>selectEvent(lifecycle.find(event=>event.event_id===button.dataset.stage))));
  }

  async function selectEvent(event, pin = true) {
    if (!event) return;
    const generation = state.generation;
    state.selected = event;
    if(pin) {state.cursor=event.occurred_at || event.recorded_at; state.follow=false; state.playing=false; state.candidateFrozen=null;}
    const symbol=event.payload?.target_symbol || (event.payload?.leg!=="hedge" ? event.payload?.symbol : null);
    if (symbol && [...$("contract").options].some(option=>option.value===symbol)) $("contract").value=symbol;
    state.observedCandidateSymbol = null;
    $("selection").textContent = `已固定事件 ${text(event.event_id)} · 持仓 ${text(event.position_id)}`;
    renderView();
    $("raw-event").textContent = json(event);
    const evidence = event.payload && event.payload.raw_ticks;
    $("evidence-summary").textContent=`事件 ${text(event.event_id)} · 发生 ${instant(event.occurred_at)} · 记录 ${instant(event.recorded_at)} · ${Array.isArray(evidence)?evidence.length:0} 条内嵌逐笔证据`;
    $("raw-evidence").querySelector("tbody").innerHTML = Array.isArray(evidence) ? evidence.map((row) => `<tr><td>${esc(row.source_time)}</td><td>${num(row.last_price, 2)}</td><td>${num(row.bid_price1, 2)}</td><td>${num(row.ask_price1, 2)}</td><td>${esc(row.source || "—")}</td></tr>`).join("") : "";
    if (!event.position_id || !state.runId) return;
    try {
      const position = await get(`/positions/${encodeURIComponent(event.position_id)}?run_id=${encodeURIComponent(state.runId)}`);
      if (generation !== state.generation || state.selected !== event) return;
      renderPosition(position);
      state.positionDirty=null;
      state.positions.set(position.position_id,position);
      const targetEvent=(position.events || []).find(item=>item.payload?.leg==="target" || String(item.event_type).split("｜")[0]==="TARGET_FILL_CONFIRMED");
      const targetSymbol=targetEvent?.payload?.target_symbol || targetEvent?.payload?.symbol;
      if(targetSymbol && [...$("contract").options].some(option=>option.value===targetSymbol)) $("contract").value=targetSymbol;
      renderView();
      const annotation = await get(`/annotations?position_id=${encodeURIComponent(event.position_id)}&run_id=${encodeURIComponent(state.runId)}`);
      if (generation !== state.generation || state.selected !== event) return;
      state.annotations = annotation.annotations || [];
      $("annotations").innerHTML = state.annotations.map((item) => `<div class="annotation">${esc(codeName(item.verdict))} · ${esc(item.reason)}</div>`).join("");
    } catch (error) {
      if (generation !== state.generation || state.selected !== event) return;
      renderPosition({ account: { status: "UNRESOLVED" }, reasons: [{ reason: error.message }] });
    }
  }

  function renderUnresolved(snapshot) {
    const unresolved = snapshot.unresolved_positions || [];
    $("unresolved").textContent = unresolved.length
      ? `未解决敞口：${unresolved.map((item) => item.position_id || "未知持仓").join("、")}`
      : "未解决敞口：无";
  }

  function renderView() {
    const stamps=state.quotes.map(row=>Date.parse(observation.stamp(row))).filter(Number.isFinite);
    if(stamps.length) {
      $("time-cursor").min=String(Math.min(...stamps)); $("time-cursor").max=String(Math.max(...stamps));
      $("time-cursor").value=String(state.cursor ? Date.parse(state.cursor) : Math.max(...stamps));
    }
    $("cursor-label").textContent=state.cursor ? `历史回看 · ${instant(state.cursor)} · UTC+08:00｜北京时间` : "实时观察 · UTC+08:00｜北京时间";
    $("follow").textContent=state.follow ? "暂停跟随" : "恢复跟随";
    $("play").textContent=state.playing ? "暂停播放" : "播放历史";
    if (state.selected && state.cursor && Date.parse(state.selected.occurred_at)>Date.parse(state.cursor)) {
      const positionId=state.selected.position_id;
      state.selected=visibleEvents().filter(event=>event.position_id && event.position_id===positionId).at(-1) || null;
      if(!state.selected) state.position=null;
      ["raw-event","selection","evidence-summary","annotations"].forEach(id=>$(id).textContent="");
      $("raw-evidence").querySelector("tbody").innerHTML="";
      if(state.selected) {
        $("raw-event").textContent=json(state.selected);
        $("selection").textContent=`已固定事件 ${text(state.selected.event_id)} · 持仓 ${text(state.selected.position_id)}`;
      }
    }
    renderOverview(state.snapshot); renderInternational(state.snapshot); drawCharts(); renderEvents();
    renderCandidates(); renderTransactions(); renderPosition(state.position); renderAlerts();
    sourceHealth({quotes:latestRows(state.snapshot).map(row=>row.quote)});
    const observedRows=observation.rowsAt(state.quotes,state);
    const late=observedRows.length-observation.monotonic(observedRows).length;
    if(late) $("source-health").textContent+=` · ${late} 条来源迟到观察保留在原始证据；不倒退当前价格轨迹`;
    const visibleStamps=observation.rowsAt(state.quotes,state).map(row=>Date.parse(observation.stamp(row))/1000).filter(Number.isFinite);
    if(visibleStamps.length) {
      const from=Math.floor(Math.min(...visibleStamps)),to=Math.max(from+1,Math.max(...visibleStamps));
      const charts=[...state.miniCharts.values(),...state.focusCharts.values(),...["main-chart","linked-chart","volume-chart","international-chart"].map(id=>$(id).__reviewChart)].filter(Boolean);
      charts.filter(item=>item.series.data().length>0).forEach(item=>item.chart.timeScale().setVisibleRange({from,to}));
    }
    if(typeof window.render==="function" && state.snapshot) {
      const rows=latestRows(state.snapshot).map(({quote:row})=>({au_symbol:row.contract,source_status:row.quality_reasons?.length?"stale":"ok",
        source_timestamp:row.source_time,ingested_at:row.captured_at,au_price_cny_per_g:row.last_price,international_cny_per_g:row.international_price,
        spread_cny_per_g:row.spread,mt5_xauusd:row.sources?.xau?.price,usdcnh:row.sources?.fx?.price,comparison_status:"unknown"}));
      window.render({...state.snapshot,rows,contract_count:rows.length,comparison:{available:false}});
    }
  }

  async function loadEvents(paint = true) {
    if (!state.runId) return;
    const page = await get(`/events?run_id=${encodeURIComponent(state.runId)}&after_sequence=${state.eventAfter}&limit=500`);
    const oldIds = new Set(state.events.map((event) => event.event_id));
    state.events = [...new Map(state.events.concat(page.events || []).map((event) => [event.event_id, event])).values()].slice(-5000);
    state.eventAfter = page.next_sequence ?? state.eventAfter;
    state.eventHasMore = Boolean(page.has_more);
    // Freeze the run's high watermark on its first response, including when
    // history spans many pages. Snapshot sequence is a different counter.
    // Only the alerts endpoint owns persisted reads; replay never reopens them.
    if (state.alertBaseline === null) {
      const high = page.event_sequence_high_watermark;
      if (Number.isSafeInteger(high) && high >= 0) state.alertBaseline = high;
      else if (!state.eventHasMore) state.alertBaseline = Math.max(0,...state.events.map(event=>Number(event.sequence)||0));
    }
    try { await loadAlertState(); } catch (_) { /* migration/read-state outage must not hide the event stream */ }
    const selectedPosition=state.selected?.position_id;
    if(selectedPosition && (page.events || []).some(event=>event.position_id===selectedPosition && !oldIds.has(event.event_id))) {
      state.positionDirty=selectedPosition;
    }
    if(selectedPosition && state.positionDirty===selectedPosition) {
      const generation=state.generation;
      const position=await get(`/positions/${encodeURIComponent(selectedPosition)}?run_id=${encodeURIComponent(state.runId)}`);
      if(generation===state.generation && state.selected?.position_id===selectedPosition) {
        state.position=position; state.positions.set(selectedPosition,position);state.positionDirty=null;
      }
    }
    $("events-more").classList.toggle("hidden", !state.eventHasMore);
    $("event-gaps").textContent = (page.missing_sequence_ranges || []).length
      ? `事件序列缺口：${json(page.missing_sequence_ranges)}` : "";
    if(page.missing_sequence_ranges_truncated) $("event-gaps").textContent+=` · 缺口列表截断，以上不是全量；已接收最高事件序号 ${text(page.event_sequence_high_watermark)}`;
    if(paint) renderView();
  }

  function selectedWindow() {
    const stamps = state.snapshotQuotes.map((row) => Date.parse(observation.stamp(row))).filter(Number.isFinite);
    const anchor = stamps.length ? Math.max(...stamps) : Date.now();
    const day = $("trading-day").value;
    const mode = $("window").value;
    if (mode === "day" && day) {
      const start = new Date(`${day}T00:00:00+08:00`);
      return { start: new Date(start.getTime()-3*3600000).toISOString(), end: new Date(start.getTime()+21*3600000).toISOString() };
    }
    const seconds = Number(mode) || 900;
    return { start: new Date(anchor - seconds * 1000).toISOString(), end: new Date(anchor + 1000).toISOString() };
  }

  async function loadSeriesAndComparison(symbol, append = false, paint = true) {
    if (!symbol || state.seriesLoading) return;
    state.seriesLoading = true;
    const generation = state.generation;
    const window = state.historyWindow || selectedWindow();
    const sameSymbol = state.seriesSymbol === "*";
    const after = (append || !state.historyWindow) && sameSymbol && state.seriesAfter ? `&after=${encodeURIComponent(state.seriesAfter)}` : "";
    try {
      const series = await get(`/series?run_id=${encodeURIComponent(state.runId)}&symbol=*&start=${encodeURIComponent(window.start)}&end=${encodeURIComponent(window.end)}&bucket_ms=1000${after}`);
      if (generation !== state.generation) return;
      const keep = append || !state.historyWindow;
      state.quotes = mergeRows(keep ? state.quotes : [], series.quotes || []);
      state.bands = mergeRows(keep ? state.bands : [], series.bands || []);
      state.seriesSymbol = "*";
      state.seriesAfter = series.next_after || (sameSymbol ? state.seriesAfter : null);
      state.seriesHasMore = Boolean(series.has_more);
      $("history-more").classList.toggle("hidden", !state.seriesHasMore);
      if(paint) renderView();
      if (!after) {
        const comparison = await get(`/comparison?run_id=${encodeURIComponent(state.runId || "")}&symbol=${encodeURIComponent(symbol)}&start=${encodeURIComponent(window.start)}&end=${encodeURIComponent(window.end)}`);
        $("comparison").textContent = comparison.available === false
          ? "尚未运行对账，不能解释为 0 个不一致"
          : `对账状态：${text(comparison.status || "available")}；不一致桶：${text(comparison.mismatch_buckets)}`;
      }
    } finally { state.seriesLoading = false; }
  }

  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    try {
      const snapshot = await get("/latest");
      if (snapshot.run_id !== state.runId) resetRun();
      state.runId = snapshot.run_id || null;
      state.snapshot = snapshot;
      state.snapshotQuotes = snapshot.quotes || [];
      state.snapshotBands = snapshot.bands || [];
      state.quotes = mergeRows(state.quotes, state.snapshotQuotes);
      state.bands = mergeRows(state.bands, state.snapshotBands);
      $("demo-banner").classList.toggle("hidden", !snapshot.synthetic);
      const symbol = contracts(snapshot); renderUnresolved(snapshot);
      if(!state.historyWindow) try { await loadSeriesAndComparison(symbol,false,false); } catch (error) { $("comparison").textContent = `对账读取失败：${error.message}`; }
      if (state.runId) await loadEvents(false);
      const visible = visibleEvents();
      if (visible.length && !state.selected && !state.cursor) await selectEvent(visible[0],false);
      if (typeof window.render === "function") {
        const rows = latestRows(snapshot).map(({quote:row}) => ({
          au_symbol: row.contract, source_status: row.quality_reasons && row.quality_reasons.length ? "stale" : "ok",
          source_timestamp: row.source_time, ingested_at: row.received_at || snapshot.received_at,
          au_price_cny_per_g: row.last_price, international_cny_per_g: row.international_price,
          spread_cny_per_g: row.spread, comparison_status: "unknown",
        }));
        window.render({ ...snapshot, rows, contract_count: rows.length, comparison: snapshot.comparison || {} });
      }
      $("review-status").textContent = `V6.0｜${state.runId || "无运行流"}｜序号 ${text(snapshot.sequence)}`;
      renderView();
    } catch (error) {
      $("review-status").textContent = `市场审阅数据不可用：${error.message}`;
    } finally {
      state.loading = false;
    }
  }

  function toggleFollow() {
    state.follow = !state.follow;
    if (state.follow) {state.historyWindow = null; state.cursor=null; state.playing=false;state.seriesAfter=null;}
    else {
      const stamps=state.quotes.map(row=>Date.parse(observation.stamp(row))).filter(Number.isFinite);
      if(stamps.length) state.cursor=new Date(Math.max(...stamps)).toISOString();
    }
    $("follow").textContent = state.follow ? "暂停跟随" : "恢复跟随";
    renderView();
  }

  async function loadHistory() {
    if (state.follow) toggleFollow();
    state.historyWindow = selectedWindow();
    state.seriesAfter = null;
    state.seriesHasMore = false;
    state.events = [];
    state.eventAfter = 0;
    state.eventHasMore = false;
    state.selected = null;
    await loadSeriesAndComparison($("contract").value);
    await loadEvents();
    const visible = visibleEvents();
    if (visible.length) await selectEvent(visible[0],false);
  }

  async function submitAnnotation(event) {
    event.preventDefault();
    if (!state.selected || !state.runId) return;
    const reason = $("reason").value.trim();
    if (!reason) return;
    const response = await fetch(ANNOTATIONS_API, { method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" }, body: JSON.stringify({
      position_id: state.selected.position_id, run_id: state.runId, verdict: $("verdict").value, reason, evidence_ids: [state.selected.event_id],
    }), cache: "no-store" });
    const body = await response.json().catch(() => ({}));
    if(response.status===401 || response.status===403) {clear(); return;}
    $("annotation-status").textContent = response.ok ? "标注已追加，保留为不可变修订历史。" : (body.detail || "标注失败");
    if (response.ok) { $("reason").value = ""; await selectEvent(state.selected); }
  }

  window.MarketReview = { state, refresh, clear, toggleFollow, renderView, selectEvent, moveCursor };
  $("follow").addEventListener("click", toggleFollow);
  $("step-back").addEventListener("click", () => moveCursor(-1));
  $("step-forward").addEventListener("click", () => moveCursor(1));
  $("return-live").addEventListener("click", () => { state.cursor = null; state.follow = true; state.historyWindow=null;state.playing=false;state.candidateFrozen=null;state.seriesAfter=null; $("follow").textContent = "暂停跟随"; refresh(); renderView(); });
  $("time-cursor").addEventListener("input",()=>{state.cursor=new Date(Number($("time-cursor").value)).toISOString();state.follow=false;state.candidateFrozen=null;renderView();});
  $("play").addEventListener("click",()=>{state.playing=!state.playing;if(state.follow) toggleFollow();renderView();});
  ["candidate-mode","preview-side"].forEach(id=>$(id).addEventListener("change",()=>{state.candidateFrozen=null;state.observedCandidateSymbol=null;renderView();}));
  $("candidate-pause").addEventListener("click",()=>{state.candidateFrozen=state.candidateFrozen ? null : structuredClone(candidateContext());$("candidate-pause").textContent=state.candidateFrozen ? "恢复排名" : "暂停排名";renderCandidates();});
  $("actual-candidate").addEventListener("click",()=>{state.observedCandidateSymbol=null;renderFocusCharts();});
  $("position-filter").addEventListener("change",renderTransactions);
  $("play-speed").addEventListener("change", () => { $("review-status").textContent = `播放速度 ${$("play-speed").value}×；实时采集节拍仍由后台决定`; });
  $("volume-toggle").addEventListener("change", () => { state.volumeEnabled = $("volume-toggle").checked; renderView(); });
  $("coordinate").addEventListener("change", drawCharts);
  $("contract").addEventListener("change", () => { state.seriesAfter = null; drawCharts(); loadSeriesAndComparison($("contract").value).catch((error) => { $("review-status").textContent = error.message; }); });
  $("ledger").addEventListener("change", () => renderPosition(state.position));
  $("annotation-form").addEventListener("submit", submitAnnotation);
  $("history-load").addEventListener("click", loadHistory);
  $("history-more").addEventListener("click", () => loadSeriesAndComparison($("contract").value, true).catch((error) => { $("review-status").textContent = error.message; }));
  $("events-more").addEventListener("click", loadEvents);
  state.timer = window.setInterval(() => { if(state.playing) moveCursor(Number($("play-speed").value)); refresh(); }, 1000);
  state.timerCount = 1;
  window.absorbLoginHandoff?.().catch((error) => { $("notice").textContent = error.message; });
  window.syncUserState?.();
  refresh();
}());
