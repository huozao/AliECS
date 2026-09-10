/* Shared observation clock and chart layers. No model calculation or trading action. */
(function () {
  "use strict";
  const stamp = row => row.observed_at || row.captured_at || row.source_time;
  const finite = value => value != null && value !== "" && Number.isFinite(Number(value));
  const seconds = value => Date.parse(value) / 1000;
  function rowsAt(rows, state) {
    const right = state.cursor ? Date.parse(state.cursor) : Infinity;
    const left = state.historyWindow ? Date.parse(state.historyWindow.start) : -Infinity;
    const end = state.historyWindow ? Math.min(right, Date.parse(state.historyWindow.end)) : right;
    return rows.filter(row => Date.parse(stamp(row)) >= left && Date.parse(stamp(row)) <= end);
  }
  function latest(rows) {
    const map = new Map();
    monotonic(rows).forEach(row => map.set(row.contract,row));
    return map;
  }
  function monotonic(rows) {
    const accepted=[],sources=new Map();
    [...rows].sort((a,b)=>Date.parse(stamp(a))-Date.parse(stamp(b))).forEach(row=>{
      const previous=sources.get(row.contract),source=Date.parse(row.source_time);
      if(Number.isFinite(previous) && (!Number.isFinite(source) || source<previous)) return;
      if(Number.isFinite(source)) sources.set(row.contract,source);
      accepted.push(row);
    });
    return accepted;
  }
  function positionAt(position,cursor) {
    if(!position || !cursor) return position;
    const at=Date.parse(cursor), events=position.events || [];
    const visible=events.filter(event=>Date.parse(event.occurred_at || event.recorded_at)<=at);
    if(visible.length===events.length) return position;
    const model=visible.filter(event=>String(event.event_type).split("｜")[0]==="MODEL_RESULT").at(-1)?.payload || {};
    const legs={target:{opened_volume:0,closed_volume:0,remaining_volume:0,gross_profit_cny:null,fills:[]},hedge:{opened_volume:0,closed_volume:0,remaining_volume:0,gross_profit_cny:null,fills:[]}};
    const seen=new Set(), invalidReasons=[];let commission=0,feeKnown=true,anyTrade=false;
    visible.filter(event=>String(event.event_type).split("｜")[0]==="ACCOUNT_TRADE").forEach(event=>{
      const p=event.payload || {},leg=legs[p.leg];
      const key=`${p.account_id}|${event.trading_day}|${event.order_id}|${event.trade_id}`;
      if(!leg || !p.account_id || !event.trade_id) {invalidReasons.push({event_id:event.event_id,reason:"账户成交标识或腿缺失"});return;}
      if(seen.has(key) || !finite(p.volume)) return;
      seen.add(key);anyTrade=true;leg.fills.push(event);
      if(p.offset==="OPEN") leg.opened_volume+=Number(p.volume);
      if(p.offset==="CLOSE") leg.closed_volume+=Number(p.volume);
      leg.remaining_volume=leg.opened_volume-leg.closed_volume;
      if(finite(p.commission_cny)) commission+=Number(p.commission_cny);else feeKnown=false;
    });
    Object.values(legs).forEach(leg=>{
      if(leg.opened_volume>0 && leg.remaining_volume===0 && leg.fills.every(event=>finite(event.payload.price)&&finite(event.payload.multiplier)))
        leg.gross_profit_cny=leg.fills.reduce((sum,event)=>sum+(event.payload.direction==="SELL"?1:-1)*Number(event.payload.price)*Number(event.payload.volume)*Number(event.payload.multiplier),0);
    });
    const complete=anyTrade && Object.values(legs).every(leg=>leg.opened_volume>0 && leg.remaining_volume===0);
    const gross=complete && Object.values(legs).every(leg=>finite(leg.gross_profit_cny)) ? Object.values(legs).reduce((sum,leg)=>sum+leg.gross_profit_cny,0):null;
    const visibleIds=new Set(visible.map(event=>event.event_id));
    const reasons=[...(position.reasons || []).filter(reason=>reason.event_id && visibleIds.has(reason.event_id)),...invalidReasons];
    Object.entries(legs).forEach(([name,leg])=>{if(leg.remaining_volume) reasons.push({reason:`${name==="target"?"目标腿":"对冲腿"}剩余 ${leg.remaining_volume} 手`});});
    if(!anyTrade) reasons.push({reason:"该游标前无账户成交回报"});
    return {...position,model,events:visible,reasons,account:{status:complete?"CLOSED":"UNRESOLVED",legs,gross_profit_cny:gross,
      actual_commission_cny:anyTrade && feeKnown?commission:null,actual_net_cny:gross!=null && feeKnown?gross-commission:null}};
  }
  function segmentedLine(item,points,color) {
    item.plotRows=points;
    if(item.linePrimitive) return;
    item.series.applyOptions({color:"transparent"});
    const renderer={draw(target){target.useBitmapCoordinateSpace(({context:ctx,horizontalPixelRatio:xr,verticalPixelRatio:yr})=>{
      ctx.strokeStyle=color;ctx.lineWidth=xr;ctx.beginPath();let connected=false;
      item.plotRows.forEach(point=>{
        const x=item.chart.timeScale().timeToCoordinate(point.time), y=finite(point.value)?item.series.priceToCoordinate(point.value):null;
        if(x==null || y==null) {connected=false;return;}
        if(connected) ctx.lineTo(x*xr,y*yr);else ctx.moveTo(x*xr,y*yr);connected=true;
      });ctx.stroke();
    });}};
    item.linePrimitive={paneViews:()=>[{zOrder:()=>"normal",renderer:()=>renderer}],updateAllViews(){}};
    item.series.attachPrimitive(item.linePrimitive);
  }
  function order(row, side) {
    const item = (row?.orders || []).find(item => item.side === side);
    if (!item || !/^(EFFECTIVE|ACTIVE|WORKING|PARTIALLY_FILLED)(｜|$)/.test(item.status || ""))
      return {...(item || {}), side, price:null, status:item?.status || "NONE｜当前无有效挂单"};
    return item;
  }
  function volumes(rows) {
    let previous = null;
    const buckets = new Map();
    const seen = new Set();
    for (const row of rows) {
      const quote = row.quote;
      if (!quote) continue;
      const time = Math.floor(seconds(row.time));
      const sourceKey = `${quote.contract}|${quote.source_time}|${quote.volume}`;
      let delta = null;
      if (Object.hasOwn(quote,"volume_delta")) {
        // This field is the cumulative total of the observation bucket, not
        // a per-version increment; replacing the last version prevents double counting.
        buckets.set(time,finite(quote.volume_delta) && Number(quote.volume_delta)>=0 ? {time,value:Number(quote.volume_delta)} : {time});
        if(finite(quote.volume)) previous=quote;
        seen.add(sourceKey);
        continue;
      }
      if (!seen.has(sourceKey)) {
        if (Object.hasOwn(quote,"volume_delta")) {
          if (finite(quote.volume_delta) && Number(quote.volume_delta) >= 0) delta = Number(quote.volume_delta);
        } else if (previous && quote.trading_day && quote.trading_day === previous.trading_day &&
          finite(quote.volume) && finite(previous.volume) && Number(quote.volume) >= Number(previous.volume) &&
          seconds(quote.source_time) > seconds(previous.source_time) && seconds(quote.source_time)-seconds(previous.source_time) <= 2 &&
          !(quote.quality_reasons || []).length) delta = Number(quote.volume)-Number(previous.volume);
        seen.add(sourceKey);
        if (finite(quote.volume)) previous = quote;
      }
      const old = buckets.get(time);
      if (delta != null) buckets.set(time,{time,value:(old?.value || 0)+delta});
      else if (!old) buckets.set(time,{time});
    }
    return [...buckets.values()].sort((a,b)=>a.time-b.time);
  }
  const colors = {upper:"#84B8EF",center:"#73839A",lower:"#A0A4E8",buy:"#F0AA70",sell:"#FFC772"};
  function bandFill(item) {
    let rows=[];
    const renderer={draw(target) {target.useBitmapCoordinateSpace(({context:ctx,horizontalPixelRatio:xr,verticalPixelRatio:yr})=>{
      let segment=[];
      const paint=()=>{
        if(segment.length<2) {segment=[];return;}
        ctx.beginPath(); segment.forEach(([x,upper],i)=>i?ctx.lineTo(x*xr,upper*yr):ctx.moveTo(x*xr,upper*yr));
        [...segment].reverse().forEach(([x,,lower])=>ctx.lineTo(x*xr,lower*yr));
        ctx.closePath();ctx.fillStyle="#84B8EF18";ctx.fill();segment=[];
      };
      rows.forEach(row=>{
        const x=item.chart.timeScale().timeToCoordinate(row.time);
        const upper=finite(row.upper)?item.series.priceToCoordinate(row.upper):null;
        const lower=finite(row.lower)?item.series.priceToCoordinate(row.lower):null;
        if(x==null || upper==null || lower==null) paint();else segment.push([x,upper,lower]);
      });paint();
      for(const [side,history] of Object.entries(item.orderRows || {})) {
        ctx.strokeStyle=colors[side];ctx.lineWidth=xr;
        let previous=null;
        history.forEach(point=>{
          const x=item.chart.timeScale().timeToCoordinate(point.time);
          const y=finite(point.value)?item.series.priceToCoordinate(point.value):null;
          if(previous && x!=null) {
            ctx.beginPath();ctx.moveTo(previous.x*xr,previous.y*yr);ctx.lineTo(x*xr,previous.y*yr);
            if(y!=null) ctx.lineTo(x*xr,y*yr);
            ctx.stroke();
          }
          previous=x!=null && y!=null ? {x,y}:null;
        });
      }
    });}};
    const primitive={paneViews:()=>[{zOrder:()=>"bottom",renderer:()=>renderer}],updateAllViews(){}};
    item.series.attachPrimitive(primitive);
    item.setBandFill=value=>{rows=value;item.bandRows=value;};
  }
  function attachLayers(item) {
    if (item.layers) return;
    const lib = window.LightweightCharts;
    item.layers = {};
    bandFill(item);
    for (const [key,color] of Object.entries(colors)) item.layers[key] = item.chart.addSeries(lib.LineSeries, {
      color:key === "buy" || key === "sell" ? "transparent" : color,lineWidth:1,lineStyle:key === "center" ? 2 : 0,
      lineType: key === "buy" || key === "sell" ? 1 : 0,
      priceLineVisible:false,lastValueVisible:false,
    });
    for (const key of ["high","low"]) item.layers[key] = item.chart.addSeries(lib.LineSeries, {
      color:"#54D6BC55",lineWidth:1,priceLineVisible:false,lastValueVisible:false,
    });
    item.volume = item.chart.addSeries(lib.HistogramSeries, {color:"#648eb7",priceScaleId:"volume",priceFormat:{type:"volume"},lastValueVisible:false,priceLineVisible:false});
    item.volume.priceScale().applyOptions({scaleMargins:{top:0.84,bottom:0}});
  }
  function layers(item, rows, volumeEnabled, coordinate = "price") {
    attachLayers(item);
    const map = new Map();
    rows.forEach(row => {
      const time = Math.floor(seconds(row.time));
      const before = map.get(time);
      map.set(time,{...row,time,high:Math.max(...[before?.high,row.high].filter(finite)),low:Math.min(...[before?.low,row.low].filter(finite))});
    });
    const bucketed = [...map.values()].sort((a,b)=>a.time-b.time);
    const base = row => coordinate === "spread" ? row.quote.international_price : coordinate === "deviation" ? row.band.center : 0;
    for (const key of Object.keys(item.layers)) {
      const values = bucketed.map(row => {
        let value;
        if (key === "high" || key === "low") value = row[key];
        else {
          const raw = key === "buy" || key === "sell" ? order(row.quote,key).price : row.band?.[key];
          value = finite(raw) && finite(base(row)) ? Number(raw)-Number(base(row)) : null;
        }
        return finite(value) ? {time:row.time,value:Number(value)} : {time:row.time};
      });
      if(["upper","center","lower"].includes(key)) {
        item.layerPlots ||= {};
        item.layerPlots[key] ||= {chart:item.chart,series:item.layers[key]};
        segmentedLine(item.layerPlots[key],values,colors[key]);
      }
      item.layers[key].setData(values);
    }
    item.setBandFill(bucketed.map(row=>({time:row.time,
      upper:finite(row.band?.upper)&&finite(base(row))?Number(row.band.upper)-Number(base(row)):null,
      lower:finite(row.band?.lower)&&finite(base(row))?Number(row.band.lower)-Number(base(row)):null})));
    for(const side of ["buy","sell"]) {
      const history=new Map();
      const originalAt=time=>{
        let lo=0,hi=rows.length;
        while(lo<hi) {const mid=(lo+hi)>>1;if(seconds(rows[mid].time)<=time) lo=mid+1;else hi=mid;}
        return rows[lo-1];
      };
      const point=(entry,time)=>{
        const raw=order(entry,side).price;
        const originalRow=originalAt(time);
        const originalBase=coordinate==="price" ? 0 : originalRow ? base(originalRow) : null;
        history.set(time,finite(raw)&&finite(originalBase)?{time,value:Number(raw)-Number(originalBase)}:{time});
      };
      if(rows.some(row=>row.quote?.order_history_mode==="delta")) {
        let expected=null,needsAnchor=true;
        const signature=entry=>JSON.stringify((entry?.orders || []).find(item=>item.side===side) || null);
        rows.forEach((row,index)=>{
          const rowTime=seconds(row.time),quote=row.quote || {},entries=quote.order_history || [];
          if(needsAnchor || index===0) {
            point({orders:quote.orders || []},rowTime);
            expected=signature(quote);needsAnchor=false;
          }
          if(index>0 && quote.order_history_truncated) {
            history.set(rowTime,{time:rowTime});expected=signature(quote);needsAnchor=true;return;
          }
          entries.filter(entry=>seconds(stamp(entry))<=rowTime && seconds(stamp(entry))>=seconds(rows[0]?.time)).forEach(entry=>{
            const time=seconds(stamp(entry));point(entry,time);expected=signature(entry);
          });
          if(expected!==signature(quote)) {
            history.set(rowTime,{time:rowTime});expected=signature(quote);needsAnchor=true;
          }
        });
      } else {
        let historySignature=null;
        rows.forEach(row=>{
          const entries=row.quote?.order_history || [];
          const signature=`${entries.length}|${stamp(entries.at(-1) || {})}`;
          const histories=[...(signature===historySignature ? [] : entries),{observed_at:row.time,orders:row.quote?.orders || []}];
          historySignature=signature;
          histories.filter(entry=>seconds(stamp(entry))<=seconds(row.time) && seconds(stamp(entry))>=seconds(rows[0]?.time)).forEach(entry=>point(entry,seconds(stamp(entry))));
        });
      }
      item.orderRows ||= {};
      item.orderRows[side]=[...history.values()].sort((a,b)=>a.time-b.time);
      item.layers[side].setData(item.orderRows[side]);
    }
    item.volume.setData(volumeEnabled ? volumes(rows) : []);
    item.volume.applyOptions({visible:volumeEnabled});
    item.chart.priceScale("right").applyOptions({scaleMargins:{top:0.12,bottom:volumeEnabled ? 0.25 : 0.12}});
    item.chart.timeScale().fitContent();
  }
  function tags(node, quote, band, series, height) {
    const names = {sell:"上方卖挂单",upper:"I 价格带上边缘",current:"真实成交价",lower:"I 价格带下边缘",buy:"下方买挂单"};
    const values = {sell:order(quote,"sell").price,upper:band?.upper,current:quote?.last_price,lower:band?.lower,buy:order(quote,"buy").price};
    const items = Object.entries(values).map(([kind,value]) => ({kind,value:finite(value)?Number(value):null}));
    items.sort((a,b)=>(b.value ?? -Infinity)-(a.value ?? -Infinity));
    const valid = items.filter(item=>item.value!=null).map(item=>item.value);
    const high = Math.max(...valid), low = Math.min(...valid);
    const gap = 17, size = Math.max(height || node.clientHeight,100);
    items.forEach((item,index) => {
      item.anchor = item.value == null ? size-8 : series?.priceToCoordinate(item.value);
      if (!finite(item.anchor)) item.anchor = 12+(high-item.value)/(high-low || 1)*(size-24);
      item.y = Math.max(9,Math.min(size-9,item.anchor),index ? items[index-1].y+gap : 9);
    });
    for (let i=items.length-1;i>=0;i--) items[i].y = Math.min(items[i].y,i === items.length-1 ? size-9 : items[i+1].y-gap);
    node.style.height=`${size}px`;
    node.innerHTML=items.map(item=>`<span class="${item.kind}" title="${names[item.kind]} · 元/克${item.value==null?' · 当前无有效值':''}" aria-label="${names[item.kind]}" data-price="${item.value ?? ''}" data-anchor="${item.anchor}" style="top:${item.y}px"><i style="width:${Math.abs(item.y-item.anchor)>2?12:4}px;transform:rotate(${Math.atan2(item.anchor-item.y,12)*180/Math.PI}deg)"></i><b>${item.value==null?'—':item.value.toFixed(2)}</b></span>`).join("");
  }
  window.MarketObservation = {stamp,rowsAt,latest,monotonic,order,volumes,layers,tags,positionAt,segmentedLine};
}());
