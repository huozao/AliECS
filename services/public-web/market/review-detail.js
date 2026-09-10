/* Lazy index/detail controller shared by today and history. */
window.MarketReviewIndex = (() => {
  "use strict";
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const chart = (node, rows, color) => {
    const points = (rows?.quotes || []).map((row) => ({time: Date.parse(row.observed_at || row.source_time) / 1000, value: Number(row.last_price ?? row.center)})).filter((row) => Number.isFinite(row.time) && Number.isFinite(row.value));
    if (!window.LightweightCharts || !points.length) { node.textContent = points.length ? "图表库不可用" : "该阶段没有可用价格曲线"; return; }
    const instance = LightweightCharts.createChart(node, {height: 180, autoSize:true, layout:{background:{color:"transparent"},textColor:"#91a1af"},grid:{vertLines:{visible:false},horzLines:{visible:false}}});
    instance.addSeries(LightweightCharts.LineSeries, {color}).setData(points);
    const band = (rows?.bands || []).map((row) => ({time: Date.parse(row.observed_at || row.source_time) / 1000, value: Number(row.center)})).filter((row) => Number.isFinite(row.time) && Number.isFinite(row.value));
    if (band.length) instance.addSeries(LightweightCharts.LineSeries, {color: "#f6c85f"}).setData(band);
    return instance;
  };
  function mount(scope) {
    const root = document.querySelector("#events"), detail = document.querySelector("#detail"), more = document.querySelector("#more"), login = document.querySelector("#login");
    const state = {cursor: null, request: null, select: null, charts: [], serial: 0};
    const filters = () => ({scope, date_from: document.querySelector("#date-from")?.value || "", date_to: document.querySelector("#date-to")?.value || "", symbol: document.querySelector("#symbol")?.value.trim() || ""});
    function clearDetail() { state.request?.abort(); state.charts.forEach((item) => item.remove()); state.charts=[]; detail.textContent="选择一笔错单后加载同一运行编号和持仓的有界复盘。"; }
    function render(items, append) {
      if (!append) root.innerHTML = "";
      for (const event of items) { const button=document.createElement("button"); button.type="button"; button.dataset.event=event.event_id; button.textContent=`${event.trading_day || "交易日未知"} · ${event.occurred_at || "—"} · ${event.symbol || "—"} · ${event.event_type || "—"} · 持仓 ${event.position_id || "—"}`; root.append(button, document.createElement("br")); }
    }
    async function load(append=false) {
      const query = new URLSearchParams({...filters(), limit:"50"}); if (append && state.cursor) query.set("after",state.cursor);
      root.dataset.state="loading";
      try { const body=await MarketPage.request(`/api/v1/market/events/index?${query}`); render(body.items || body.events || [],append); state.cursor=body.next_cursor; more.hidden=!body.has_more; root.dataset.state=`${body.effective_trading_day ? `有效交易日 ${body.effective_trading_day}` : "尚无交易日数据"}`; login.hidden=true; }
      catch (error) { root.textContent=error.message; login.hidden=!(error.cause === "login" || error.cause === "forbidden"); } finally { delete root.dataset.state; }
    }
    async function show(eventId) {
      state.request?.abort(); const serial=++state.serial; clearDetail(); const controller=new AbortController(); state.request=controller; detail.textContent="读取该笔复盘…";
      try { const body=await MarketPage.request(`/api/v1/market/events/${encodeURIComponent(eventId)}/detail`,{controller}); if (serial !== state.serial) return;
        const event=body.event || {}; const position=body.position || {}; detail.innerHTML=`<h2>${esc(event.event_type || "错单复盘")}</h2><p>发生：${esc(event.occurred_at)} · 运行：${esc(body.run_id)} · 持仓：${esc(body.position_id)}</p><p>模型账：${esc(position.model?.net_profit_cny ?? "—")}；账户账：${esc(position.account?.actual_net_cny ?? "—")}；未解决敞口：${position.unresolved ? "是" : "否"}</p><div class="detail-charts"><div data-chart="target"></div><div data-chart="hedge"></div></div><h3>生命周期</h3><ol>${(body.lifecycle || position.events || []).slice(0,200).map((row)=>`<li>${esc(row.occurred_at)} · ${esc(row.event_type)}</li>`).join("") || "<li>没有可用生命周期证据</li>"}</ol>`;
        const series=body.series || {}; const nodes=detail.querySelectorAll("[data-chart]"); state.charts=[chart(nodes[0],series.target || series,"#54d6bc"),chart(nodes[1],series.hedge || series,"#ef9a9a")].filter(Boolean);
      } catch (error) { if (error.name !== "AbortError") detail.textContent=error.message; if (error.cause === "login" || error.cause === "forbidden") login.hidden=false; }
    }
    root.addEventListener("click", (event) => {const id=event.target.closest("[data-event]")?.dataset.event;if(id)show(id);});
    more.addEventListener("click",()=>load(true)); login?.addEventListener("click",()=>MarketPage.login()); document.querySelector("#filter")?.addEventListener("click",()=>{state.cursor=null;clearDetail();load(false);});
    MarketPage.absorbLoginHandoff().catch((error)=>{root.textContent=error.message; login.hidden=false;}).finally(()=>load(false));
  }
  return {mount};
})();
