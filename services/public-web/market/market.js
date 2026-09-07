/* V6 market review client: bounded, read-only evidence browsing. */
(function () {
  "use strict";

  const API = "/api/v1/market";
  const ANNOTATIONS_API = "/api/v1/market/annotations";
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
  };

  const $ = (id) => document.getElementById(id);
  const text = (value) => String(value ?? "—");
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const json = (value) => JSON.stringify(value ?? {}, null, 2);
  const num = (value, digits = 3) => value == null || value === "" || !Number.isFinite(Number(value))
    ? "—" : Number(value).toFixed(digits);

  function authHeaders() {
    const token = window.AliECSAuth && window.AliECSAuth.getToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function get(path) {
    const generation = state.generation;
    const response = await fetch(`${API}${path}`, { headers: authHeaders(), cache: "no-store" });
    const body = await response.json().catch(() => ({}));
    if (generation !== state.generation) throw new Error("请求已失效");
    if (response.status === 401 || response.status === 403) clear();
    if (!response.ok) throw new Error(body.detail || `接口返回 HTTP ${response.status}`);
    return body;
  }

  function resetRun() {
    state.generation += 1;
    Object.assign(state, { events: [], quotes: [], bands: [], snapshotQuotes: [], snapshotBands: [],
      snapshot: null, selected: null, position: null, annotations: [], chartData: [],
      eventAfter: 0, eventHasMore: false, seriesAfter: null, seriesHasMore: false,
      seriesSymbol: null, historyWindow: null, runId: null });
    ["raw-event", "annotations", "selection", "event-gaps", "unresolved", "comparison", "annotation-status"].forEach((id) => { $(id).textContent = ""; });
    $("raw-evidence").querySelector("tbody").innerHTML = "";
    $("reason").value = "";
    $("contract").innerHTML = "";
    $("history-more").classList.add("hidden");
    $("events-more").classList.add("hidden");
    renderCandidates(null); renderPosition(null); renderEvents(); drawCharts();
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
    [...previous, ...incoming].forEach((row) => rows.set(`${row.contract}|${row.source_time}`, row));
    return [...rows.values()].sort((a, b) => Date.parse(a.source_time) - Date.parse(b.source_time)).slice(-10000);
  }

  function sourceHealth(snapshot) {
    const rows = Array.isArray(snapshot && snapshot.quotes) ? snapshot.quotes : [];
    const ages = [];
    rows.forEach((row) => Object.values(row.sources || {}).forEach((source) => {
      if (Number.isFinite(Number(source.age_seconds))) ages.push(Number(source.age_seconds));
    }));
    $("source-health").textContent = ages.length
      ? `源时钟年龄：${Math.max(...ages).toFixed(0)} 秒；保留源时刻与服务器接收时刻`
      : "源时钟年龄：—；缺少可用源时刻";
  }

  function contracts(snapshot) {
    const values = [...new Set((snapshot.quotes || []).map((row) => row.contract).filter(Boolean))];
    const select = $("contract");
    const current = select.value;
    select.innerHTML = values.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
    if (values.includes(current)) select.value = current;
    return select.value || values[0] || "";
  }

  function chartRows(symbol) {
    const sourceQuotes = (state.quotes || []).some((row) => row.contract === symbol)
      ? state.quotes : state.snapshotQuotes;
    const sourceBands = (state.bands || []).some((row) => row.contract === symbol)
      ? state.bands : state.snapshotBands;
    const bands = new Map((sourceBands || []).filter((row) => row.contract === symbol)
      .map((row) => [Math.floor(Date.parse(row.source_time) / 1000), row]));
    return (sourceQuotes || []).filter((row) => row.contract === symbol).map((row) => {
      const band = bands.get(Math.floor(Date.parse(row.source_time) / 1000)) || {};
      const ohlc = row.ohlc || {};
      const coordinate = $("coordinate").value;
      const rawBase = coordinate === "spread" ? row.international_price : coordinate === "deviation" ? band.center : 0;
      const base = rawBase != null && rawBase !== "" && Number.isFinite(Number(rawBase)) ? Number(rawBase) : null;
      const value = (x) => x == null || !Number.isFinite(Number(x)) || base == null ? null : Number(x) - base;
      return {
        time: row.source_time,
        open: value(ohlc.open ?? row.last_price), high: value(ohlc.high ?? row.last_price),
        low: value(ohlc.low ?? row.last_price), close: value(ohlc.close ?? row.last_price),
        source_time: row.source_time, quote: row, band,
      };
    });
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
    $("axis-note").textContent = $("coordinate").value === "spread"
      ? "价差＝同一条源时刻记录的国内价格减国际折算价（元/克）"
      : $("coordinate").value === "deviation"
        ? "模型偏离＝同一条源时刻记录的国内价格减中心（元/克）"
        : "绝对价格（元/克）；缺失秒保持缺失，不插值";
    const main = $("main-chart");
    const linked = $("linked-chart");
    const display = dedupeChartRows(state.chartData).filter((row) => [row.open, row.high, row.low, row.close].every((value) => value != null && Number.isFinite(Number(value))))
      .map((row) => ({ ...row, time: Math.floor(Date.parse(row.time) / 1000) }));
    try {
      const library = window.LightweightCharts;
      if (library && !main.__reviewChart) {
        const options = { layout: { background: { color: "transparent" }, textColor: "#91a1af" }, grid: { vertLines: { color: "#2b3b4a" }, horzLines: { color: "#2b3b4a" } }, width: main.clientWidth || 320, height: 350 };
        const mainChart = library.createChart(main, options);
        const linkedChart = library.createChart(linked, { ...options, height: 220 });
        main.__reviewChart = { chart: mainChart, series: mainChart.addSeries(library.CandlestickSeries, { upColor: "#61d59a", downColor: "#ff8980", borderVisible: false, wickUpColor: "#61d59a", wickDownColor: "#ff8980" }) };
        linked.__reviewChart = { chart: linkedChart, series: linkedChart.addSeries(library.LineSeries, { color: "#79b8ff", lineWidth: 2 }) };
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
        const markerMap = new Map();
        visibleEvents().forEach((event) => {
          const time = Math.floor(Date.parse(event.occurred_at || event.recorded_at || "") / 1000);
          if (!Number.isFinite(time)) return;
          const label = String(event.event_type || "事件").split("｜")[0];
          const labels = markerMap.get(time) || [];
          if (!labels.includes(label)) labels.push(label);
          markerMap.set(time, labels);
        });
        const markers = [...markerMap.entries()].sort((left, right) => left[0] - right[0]).map(([time, labels]) => ({
          time, position: "aboveBar", color: "#ffd166", shape: "circle", text: labels.join("/").slice(0, 24),
        }));
        if (main.__reviewChart.markers) main.__reviewChart.markers.setMarkers(markers);
        main.__reviewChart.chart.timeScale().fitContent();
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
  }

  function renderEvents() {
    const target = $("events");
    const events = visibleEvents();
    target.innerHTML = events.length ? events.map((event) => {
      const selected = state.selected && state.selected.event_id === event.event_id ? " selected" : "";
      return `<button type="button" class="event-item${selected}" data-event-id="${esc(event.event_id)}">` +
        `${esc(event.occurred_at || event.recorded_at || "—")} · ${esc(event.event_type || "事件")} · ${esc(event.position_id || "无持仓")}</button>`;
    }).join("") : '<span class="muted">暂无事件</span>';
    target.querySelectorAll("[data-event-id]").forEach((button) => button.addEventListener("click", () => {
      selectEvent(state.events.find((event) => event.event_id === button.dataset.eventId));
    }));
  }

  function visibleEvents() {
    if (!state.historyWindow) return state.events;
    const left = Date.parse(state.historyWindow.start);
    const right = Date.parse(state.historyWindow.end);
    return state.events.filter((event) => {
      const at = Date.parse(event.occurred_at || event.recorded_at || "");
      return Number.isFinite(at) && at >= left && at <= right;
    });
  }

  function renderCandidates(event) {
    const candidates = event && event.payload && Array.isArray(event.payload.candidates)
      ? event.payload.candidates : [];
    $("decision-label").textContent = event ? `决策 ${text(event.payload && event.payload.decision_id)}` : "";
    $("candidates").querySelector("tbody").innerHTML = candidates.map((candidate) => {
      const contract = candidate.contract || candidate.symbol;
      const reasons = candidate.rejection_reasons || candidate.reasons || (candidate.reason_code ? [candidate.reason_code] : []);
      const cost = candidate.expected_cost_cny_per_pair ?? candidate.expected_cost_cny;
      const instruction = candidate.instruction || candidate.fill || (candidate.selected ? "SELECTED_BY_V6｜V6 已选" : "—");
      return `<tr>
      <td>${esc(candidate.rank)}</td><td>${esc(contract)}</td>
      <td>${candidate.eligible ? "合格" : "拒绝"}${candidate.selected ? " / 选中" : ""}</td>
      <td>${esc(reasons.join("；"))}</td>
      <td>${num(cost, 2)}</td><td>${num(candidate.theoretical_price, 4)}</td><td>${num(candidate.slippage_cny_per_g, 4)}</td>
      <td>${num(candidate.quote_age_seconds, 1)}</td><td>${esc(instruction)}</td>
    </tr>`;
    }).join("");
  }

  function renderPosition(position) {
    state.position = position;
    if (!position) {
      $("position-detail").textContent = "选择事件后读取账户回报；未知收益显示“—”，不补成 0。";
      return;
    }
    const account = position.account || {};
    const model = position.model || {};
    const ledger = $("ledger").value;
    const modelHtml = `<div><div class="label">模型账净收益</div><div class="money">${num(model.net_pnl_cny ?? model.net_profit_cny, 2)}</div></div>`;
    const accountHtml = `<div><div class="label">账户账实际净收益</div><div class="money">${num(account.actual_net_cny ?? account.actual_net_pnl_cny, 2)}</div></div>`;
    const pnlHtml = ledger === "model" ? modelHtml : ledger === "account" ? accountHtml : modelHtml + accountHtml;
    $("position-detail").innerHTML = `<div class="pnl">${pnlHtml}</div>` +
      `<div class="calculation">账本视图：${esc(ledger)}<br>` +
      `账户状态：${esc(account.status || (position.unresolved ? "UNRESOLVED｜待核对" : "CLOSED｜已平仓"))}<br>` +
      `原因：${esc((position.reasons || account.unresolved_reasons || []).map((reason) => reason.reason || reason).join("；") || "—")}</div>`;
  }

  async function selectEvent(event) {
    if (!event) return;
    const generation = state.generation;
    state.selected = event;
    $("selection").textContent = `已固定事件 ${text(event.event_id)} · 持仓 ${text(event.position_id)}`;
    renderEvents(); renderCandidates(event);
    $("raw-event").textContent = json(event);
    const evidence = event.payload && event.payload.raw_ticks;
    $("raw-evidence").querySelector("tbody").innerHTML = Array.isArray(evidence) ? evidence.map((row) => `<tr><td>${esc(row.source_time)}</td><td>${num(row.last_price, 2)}</td><td>${num(row.bid_price1, 2)}</td><td>${num(row.ask_price1, 2)}</td><td>${esc(row.source || "—")}</td></tr>`).join("") : "";
    if (!event.position_id || !state.runId) return;
    try {
      const position = await get(`/positions/${encodeURIComponent(event.position_id)}?run_id=${encodeURIComponent(state.runId)}`);
      if (generation !== state.generation || state.selected !== event) return;
      renderPosition(position);
      const annotation = await get(`/annotations?position_id=${encodeURIComponent(event.position_id)}&run_id=${encodeURIComponent(state.runId)}`);
      if (generation !== state.generation || state.selected !== event) return;
      state.annotations = annotation.annotations || [];
      $("annotations").innerHTML = state.annotations.map((item) => `<div class="annotation">${esc(item.verdict)} · ${esc(item.reason)}</div>`).join("");
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

  async function loadEvents() {
    if (!state.runId) return;
    const page = await get(`/events?run_id=${encodeURIComponent(state.runId)}&after_sequence=${state.eventAfter}&limit=500`);
    state.events = [...new Map(state.events.concat(page.events || []).map((event) => [event.event_id, event])).values()].slice(-5000);
    state.eventAfter = page.next_sequence ?? state.eventAfter;
    state.eventHasMore = Boolean(page.has_more);
    $("events-more").classList.toggle("hidden", !state.eventHasMore);
    $("event-gaps").textContent = (page.missing_sequence_ranges || []).length
      ? `事件序列缺口：${json(page.missing_sequence_ranges)}` : "";
    renderEvents();
    drawCharts();
  }

  function selectedWindow() {
    const stamps = state.snapshotQuotes.map((row) => Date.parse(row.source_time || "")).filter(Number.isFinite);
    const anchor = stamps.length ? Math.max(...stamps) : Date.now();
    const day = $("trading-day").value;
    const mode = $("window").value;
    if (mode === "day" && day) {
      const start = new Date(`${day}T00:00:00+08:00`);
      return { start: start.toISOString(), end: new Date(start.getTime() + 86400000).toISOString() };
    }
    const seconds = Number(mode) || 900;
    return { start: new Date(anchor - seconds * 1000).toISOString(), end: new Date(anchor + 1000).toISOString() };
  }

  async function loadSeriesAndComparison(symbol, append = false) {
    if (!symbol || state.seriesLoading) return;
    state.seriesLoading = true;
    const generation = state.generation;
    const window = state.historyWindow || selectedWindow();
    const sameSymbol = state.seriesSymbol === symbol;
    const after = (append || !state.historyWindow) && sameSymbol && state.seriesAfter ? `&after=${encodeURIComponent(state.seriesAfter)}` : "";
    try {
      const series = await get(`/series?run_id=${encodeURIComponent(state.runId)}&symbol=${encodeURIComponent(symbol)}&start=${encodeURIComponent(window.start)}&end=${encodeURIComponent(window.end)}&bucket_ms=1000${after}`);
      if (generation !== state.generation) return;
      const keep = append || !state.historyWindow;
      state.quotes = mergeRows(keep ? state.quotes : [], series.quotes || []);
      state.bands = mergeRows(keep ? state.bands : [], series.bands || []);
      state.seriesSymbol = symbol;
      state.seriesAfter = series.next_after || (sameSymbol ? state.seriesAfter : null);
      state.seriesHasMore = Boolean(series.has_more);
      $("history-more").classList.toggle("hidden", !state.seriesHasMore);
      drawCharts();
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
      if (state.historyWindow) return;
      state.quotes = mergeRows(state.quotes, state.snapshotQuotes);
      state.bands = mergeRows(state.bands, state.snapshotBands);
      $("demo-banner").classList.toggle("hidden", !snapshot.synthetic);
      sourceHealth(snapshot); const symbol = contracts(snapshot); renderUnresolved(snapshot); drawCharts();
      try { await loadSeriesAndComparison(symbol); drawCharts(); } catch (error) { $("comparison").textContent = `对账读取失败：${error.message}`; }
      if (state.runId && (!state.events.length || state.follow)) await loadEvents();
      const visible = visibleEvents();
      if (visible.length && !state.selected) await selectEvent(visible[0]);
      if (typeof window.render === "function") {
        const rows = snapshot.rows || state.quotes.map((row) => ({
          au_symbol: row.contract, source_status: row.quality_reasons && row.quality_reasons.length ? "stale" : "ok",
          source_timestamp: row.source_time, ingested_at: row.received_at || snapshot.received_at,
          au_price_cny_per_g: row.last_price, international_cny_per_g: row.international_price,
          spread_cny_per_g: row.spread, comparison_status: "unknown",
        }));
        window.render({ ...snapshot, rows, contract_count: rows.length, comparison: snapshot.comparison || {} });
      }
      $("review-status").textContent = `V6.0｜${state.runId || "无运行流"}｜序号 ${text(snapshot.sequence)}`;
    } catch (error) {
      $("review-status").textContent = `市场审阅数据不可用：${error.message}`;
    } finally {
      state.loading = false;
    }
  }

  function toggleFollow() {
    state.follow = !state.follow;
    if (state.follow) state.historyWindow = null;
    $("follow").textContent = state.follow ? "暂停跟随" : "恢复跟随";
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
    if (visible.length) await selectEvent(visible[0]);
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
    $("annotation-status").textContent = response.ok ? "标注已追加，保留为不可变修订历史。" : (body.detail || "标注失败");
    if (response.ok) { $("reason").value = ""; await selectEvent(state.selected); }
  }

  window.MarketReview = { state, refresh, clear, toggleFollow };
  $("follow").addEventListener("click", toggleFollow);
  $("coordinate").addEventListener("change", drawCharts);
  $("contract").addEventListener("change", () => { state.seriesAfter = null; drawCharts(); loadSeriesAndComparison($("contract").value).catch((error) => { $("review-status").textContent = error.message; }); });
  $("ledger").addEventListener("change", () => renderPosition(state.position));
  $("annotation-form").addEventListener("submit", submitAnnotation);
  $("history-load").addEventListener("click", loadHistory);
  $("history-more").addEventListener("click", () => loadSeriesAndComparison($("contract").value, true).catch((error) => { $("review-status").textContent = error.message; }));
  $("events-more").addEventListener("click", loadEvents);
  state.timer = window.setInterval(() => { if (state.follow) refresh(); }, 1000);
  state.timerCount = 1;
  window.absorbLoginHandoff?.().catch((error) => { $("notice").textContent = error.message; });
  window.syncUserState?.();
  refresh();
}());
