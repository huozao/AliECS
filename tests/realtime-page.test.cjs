const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const GoldMarketRealtimeState = require('../services/public-web/market/realtime-state.js');

test('realtime page derives domestic freshness from quote timestamps and polls incrementally', async () => {
  const script = fs.readFileSync(path.join(__dirname, '../services/public-web/market/realtime.js'), 'utf8');
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', innerHTML: '', hidden: false,
      classList: {toggle() {}}, addEventListener() {}, querySelector: node});
    return nodes.get(selector);
  };
  const timers = [];
  const requests = [];
  const now = new Date();
  const quote = {contract: 'SHFE.au2610', source_time: new Date(now - 1000).toISOString(),
    observed_at: now.toISOString(), last_price: 900, ohlc: {high: 901, low: 899}};
  const base = {run_id: 'run-1', quotes: [quote], bands: [], orders: [], series: {},
    international: {strategy_allowed: true, status: 'READY｜国际参考可用',
      xauusd: {price: 4000, age_seconds: 1}, usdcnh: {price: 7, age_seconds: 1}},
    window_minutes: 5};
  const responses = [
    {...base, published_at: now.toISOString()},
    {...base, next_since: now.toISOString(), source_revision: 'revision-1'},
    {...base, incremental: true, next_since: now.toISOString(), source_revision: 'revision-1'},
  ];
  const document = {hidden: false, querySelector: node, addEventListener() {},
    createElement() { return {className: '', dataset: {}, innerHTML: '', querySelector: node}; }};
  node('#window-minutes').value = '5';
  node('#contracts').append = () => {};
  const context = {document, window: {setTimeout: (callback, delay) => {timers.push({callback, delay}); return timers.length;},
    clearTimeout() {}, addEventListener() {}}, performance: {now: () => 0},
    MarketPage: {absorbLoginHandoff: async () => {}, request: async (url) => {
      requests.push(url); return responses.shift();
    }}, URLSearchParams, Date, Number, Map, Set, AbortController, console};
  vm.runInNewContext(script, context);
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);
  assert.equal(node('#domestic-status').textContent, 'FRESH｜国内行情新鲜');
  assert.match(node('#domestic-detail').textContent, /1\/1 个合约/);
  assert.match(node('#overlap-status').textContent, /^READY/);
  const poll = timers.find((timer) => timer.delay === 2000);
  assert.ok(poll, 'a subsequent poll is scheduled');
  poll.callback();
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);
  assert.equal(requests[0], '/api/v1/market/latest');
  assert.match(requests[1], /\/api\/v1\/market\/realtime/);
  assert.match(requests[2], /since=/);
  assert.doesNotMatch(requests[2], /revision=/);
});

test('realtime page shows subscribed and waiting while the source has no first observation', async () => {
  const script = fs.readFileSync(path.join(__dirname, '../services/public-web/market/realtime.js'), 'utf8');
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', innerHTML: '', hidden: false,
      classList: {toggle() {}}, addEventListener() {}, querySelector: node});
    return nodes.get(selector);
  };
  node('#window-minutes').value = '5';
  const snapshot = {schema_version: 'gold-display-bootstrap/v1', run_id: null,
    stream_epoch: 'epoch-0', source_sequence: 0, continuous: true,
    window_complete: false, window_minutes: 5, events: []};
  const requests = [];
  const document = {hidden: false, querySelector: node, addEventListener() {}};
  const context = {document, window: {setTimeout: () => 1, clearTimeout() {}, addEventListener() {}},
    performance: {now: () => 0}, GoldMarketRealtimeState,
    GoldMarketRealtimeStream: {create: (options) => ({
      async start() { options.onStatus({state: 'bootstrapping'}); options.onSnapshot(snapshot); },
      stop() {}, bootstrap() {},
    })},
    MarketPage: {absorbLoginHandoff: async () => {}, request: async (url) => { requests.push(url); return ({
      quotes: [], bands: [], orders: [], series: {}, window_minutes: 5,
    }); }}, URLSearchParams, Date, Number, Map, Set, AbortController, console};
  vm.runInNewContext(script, context);
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);
  assert.match(node('#stream-status').textContent, /等待首条行情/);
  assert.deepEqual(requests, ['/api/v1/market/latest']);
});

test('realtime page does not block domesticReady when domestic quotes are older than 30s and plots stepped orders', async () => {
  const script = fs.readFileSync(path.join(__dirname, '../services/public-web/market/realtime.js'), 'utf8');
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', innerHTML: '', hidden: false,
      classList: {toggle() {}}, addEventListener() {}, querySelector: node});
    return nodes.get(selector);
  };
  const seriesCreated = [];
  const priceLinesCreated = [];
  const createdSeriesMock = (type, options) => {
    const s = {
      type, options, data: [],
      setData(d) { s.data = d; },
      update(p) { s.data.push(p); },
      createPriceLine(opt) {
        const pl = { options: opt, applyOptions(o) { Object.assign(pl.options, o); } };
        priceLinesCreated.push(pl);
        return pl;
      },
      removePriceLine(pl) {
        const idx = priceLinesCreated.indexOf(pl);
        if (idx !== -1) priceLinesCreated.splice(idx, 1);
      }
    };
    seriesCreated.push(s);
    return s;
  };
  const LightweightCharts = {
    LineSeries: 'LineSeries',
    LineType: {Simple: 0, WithSteps: 1, Curved: 2},
    createChart: () => ({
      addSeries: (type, options) => createdSeriesMock(type, options),
      remove() {}
    })
  };
  const now = new Date();
  // 120s old domestic quote (simulating sparse / far-month contract)
  const oldDomesticTime = new Date(now.getTime() - 120000).toISOString();
  const quote = {
    contract: 'SHFE.au2612',
    source_time: oldDomesticTime,
    observed_at: oldDomesticTime,
    last_price: 902.5,
    ohlc: {high: 903, low: 901},
    orders: [
      {side: 'buy', price: 901.0, status: 'EFFECTIVE｜有效挂单'},
      {side: 'sell', price: 904.0, status: 'EFFECTIVE｜有效挂单'}
    ]
  };
  const band = {
    contract: 'SHFE.au2612',
    source_time: oldDomesticTime,
    upper: 905.0,
    center: 902.5,
    lower: 900.0
  };
  const base = {
    run_id: 'run-old-domestic',
    quotes: [quote],
    bands: [band],
    orders: [{contract: 'SHFE.au2612', buy: {price: 901.0, status: 'EFFECTIVE'}, sell: {price: 904.0, status: 'EFFECTIVE'}}],
    series: {'SHFE.au2612': {quotes: [quote], bands: [band]}},
    international: {strategy_allowed: true, status: 'READY｜国际参考可用',
      xauusd: {price: 4000, age_seconds: 1}, usdcnh: {price: 7, age_seconds: 1}},
    window_minutes: 5
  };
  node('#window-minutes').value = '5';
  node('#contracts').append = () => {};
  const document = {hidden: false, querySelector: node, addEventListener() {},
    createElement() { return {className: '', dataset: {}, innerHTML: '', querySelector: node}; }};
  const context = {
    document,
    window: {setTimeout: () => 1, clearTimeout() {}, addEventListener() {}, LightweightCharts},
    performance: {now: () => 0},
    LightweightCharts,
    MarketPage: {absorbLoginHandoff: async () => {}, request: async (url) => base},
    URLSearchParams, Date, Number, Map, Set, AbortController, console
  };
  vm.runInNewContext(script, context);
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);
  // Domestic must not be blocked despite age > 30s
  assert.equal(node('#domestic-status').textContent, 'FRESH｜国内行情新鲜');
  assert.match(node('#overlap-status').textContent, /^READY/);
  assert.match(node('#strategy-status').textContent, /^MARKET_READY/);
  // Check that stepped series were registered for sell and buy orders
  const sellStepSeries = seriesCreated.find((s) => s.options?.color === '#FFC772' && s.options?.lineType === 1);
  const buyStepSeries = seriesCreated.find((s) => s.options?.color === '#F0AA70' && s.options?.lineType === 1);
  assert.ok(sellStepSeries, 'stepped sell order series was created');
  assert.ok(buyStepSeries, 'stepped buy order series was created');
  assert.equal(sellStepSeries.data[0]?.value, 904.0);
  assert.equal(buyStepSeries.data[0]?.value, 901.0);
  // Check that price lines for resting orders were created
  const sellPriceLine = priceLinesCreated.find((pl) => pl.options?.color === '#FFC772');
  const buyPriceLine = priceLinesCreated.find((pl) => pl.options?.color === '#F0AA70');
  assert.ok(sellPriceLine, 'sell price line was created');
  assert.ok(buyPriceLine, 'buy price line was created');
  assert.equal(sellPriceLine.options?.price, 904.0);
  assert.equal(buyPriceLine.options?.price, 901.0);
  assert.match(sellPriceLine.options?.title, /卖 904\.00/);
  assert.match(buyPriceLine.options?.title, /买 901\.00/);
});

test('realtime page renders cards from latest quotes and bands when stream bootstrap has empty events', async () => {
  const script = fs.readFileSync(path.join(__dirname, '../services/public-web/market/realtime.js'), 'utf8');
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', innerHTML: '', hidden: false,
      classList: {toggle() {}}, addEventListener() {}, querySelector: node});
    return nodes.get(selector);
  };
  const appendedCards = [];
  node('#contracts').append = (el) => appendedCards.push(el);
  const now = new Date();
  const quote = {
    contract: 'SHFE.au2610',
    source_time: now.toISOString(),
    observed_at: now.toISOString(),
    last_price: 896.18,
    ohlc: {high: 896.20, low: 896.14}
  };
  const band = {
    contract: 'SHFE.au2610',
    source_time: now.toISOString(),
    upper: 898.17,
    center: 896.17,
    lower: 894.17
  };
  const latestHead = {
    run_id: 'live-review-test',
    quotes: [quote],
    bands: [band],
    orders: [{contract: 'SHFE.au2610', buy: {price: 895.0, status: 'EFFECTIVE'}, sell: {price: 899.0, status: 'EFFECTIVE'}}],
    international: {strategy_allowed: true, status: 'READY｜国际参考可用',
      xauusd: {price: 4100, age_seconds: 2}, usdcnh: {price: 7.1, age_seconds: 2}},
    window_minutes: 5
  };
  // Stream connects with continuous=true but events=[]
  const emptyBootstrap = {
    schema_version: 'gold-display-bootstrap/v1',
    run_id: 'live-review-test',
    stream_epoch: 'epoch-1',
    source_sequence: 100,
    continuous: true,
    window_complete: false,
    window_minutes: 5,
    events: []
  };
  const document = {
    hidden: false,
    querySelector: node,
    addEventListener() {},
    createElement() {
      const card = {className: '', dataset: {}, innerHTML: '', querySelector: node};
      return card;
    }
  };
  node('#window-minutes').value = '5';
  const context = {
    document,
    window: {setTimeout: () => 1, clearTimeout() {}, addEventListener() {}},
    performance: {now: () => 0},
    GoldMarketRealtimeState,
    GoldMarketRealtimeStream: {
      create: (options) => ({
        async start() {
          options.onStatus({state: 'bootstrapping'});
          options.onSnapshot(emptyBootstrap);
        },
        stop() {},
        bootstrap() {}
      })
    },
    MarketPage: {
      absorbLoginHandoff: async () => {},
      request: async (url) => {
        if (url.includes('/latest')) return latestHead;
        return {quotes: [], bands: [], orders: [], series: {}, window_minutes: 5};
      }
    },
    URLSearchParams, Date, Number, Map, Set, AbortController, console
  };
  vm.runInNewContext(script, context);
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);
  // Verify that even with empty stream bootstrap, contract card was created and populated from latestHead
  assert.equal(appendedCards.length, 1, 'one contract card should be created');
  assert.equal(appendedCards[0].dataset.contract, 'SHFE.au2610');
});

test('realtime page creates disconnected order segments with true gap when order is canceled and repriced', async () => {
  const script = fs.readFileSync(path.join(__dirname, '../services/public-web/market/realtime.js'), 'utf8');
  const nodes = new Map();
  const node = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {textContent: '', innerHTML: '', hidden: false,
      classList: {toggle() {}}, addEventListener() {}, querySelector: node});
    return nodes.get(selector);
  };
  const seriesCreated = [];
  const priceLinesCreated = [];
  const createdSeriesMock = (type, options) => {
    const s = {
      type, options, data: [],
      setData(d) { s.data = d; },
      update(p) { s.data.push(p); },
      createPriceLine(opt) {
        const pl = { options: opt, applyOptions(o) { Object.assign(pl.options, o); } };
        priceLinesCreated.push(pl);
        return pl;
      },
      removePriceLine(pl) {
        const idx = priceLinesCreated.indexOf(pl);
        if (idx !== -1) priceLinesCreated.splice(idx, 1);
      }
    };
    seriesCreated.push(s);
    return s;
  };
  const LightweightCharts = {
    LineSeries: 'LineSeries',
    LineType: {Simple: 0, WithSteps: 1, Curved: 2},
    createChart: () => ({
      addSeries: (type, options) => createdSeriesMock(type, options),
      remove() {}
    })
  };
  const baseT = 1787638000;
  // Quotes sequence:
  // t=0..20: sell order at 904.0
  // t=25..35: NO order (canceled, gap!)
  // t=40..60: new sell order at 906.0
  const quotes = [
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 0) * 1000, sell_order_price: 904.0, last_price: 902.0 },
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 10) * 1000, sell_order_price: 904.0, last_price: 902.2 },
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 20) * 1000, sell_order_price: 904.0, last_price: 902.5 },
    // Gap: orders canceled due to price deviation
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 25) * 1000, sell_order_price: null, last_price: 903.0 },
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 30) * 1000, sell_order_price: null, last_price: 903.5 },
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 35) * 1000, sell_order_price: null, last_price: 904.0 },
    // Replaced order at new price
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 40) * 1000, sell_order_price: 906.0, last_price: 904.2 },
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 50) * 1000, sell_order_price: 906.0, last_price: 904.5 },
    { contract: 'SHFE.au2612', bucket_start_ms: (baseT + 60) * 1000, sell_order_price: 906.0, last_price: 904.8 },
  ];
  const base = {
    run_id: 'run-gap-test',
    quotes,
    bands: [],
    orders: [{ contract: 'SHFE.au2612', sell: { price: 906.0, status: 'EFFECTIVE' }, buy: null }],
    series: { 'SHFE.au2612': { quotes, bands: [] } },
    international: { strategy_allowed: true, status: 'READY｜国际参考可用',
      xauusd: { price: 4000, age_seconds: 1 }, usdcnh: { price: 7, age_seconds: 1 } },
    window_minutes: 5
  };
  node('#window-minutes').value = '5';
  node('#contracts').append = () => {};
  const document = { hidden: false, querySelector: node, addEventListener() {},
    createElement() { return { className: '', dataset: {}, innerHTML: '', querySelector: node }; } };
  const context = {
    document,
    window: { setTimeout: () => 1, clearTimeout() {}, addEventListener() {}, LightweightCharts },
    performance: { now: () => 0 },
    LightweightCharts,
    MarketPage: { absorbLoginHandoff: async () => {}, request: async () => base },
    URLSearchParams, Date, Number, Map, Set, AbortController, console
  };
  vm.runInNewContext(script, context);
  for (let i = 0; i < 4; i++) await new Promise(setImmediate);

  // Filter sell series (golden color #FFC772) with non-empty data
  const sellSeriesList = seriesCreated.filter((s) => s.options?.color === '#FFC772' && s.data.length > 0);
  assert.equal(sellSeriesList.length, 2, 'should create exactly 2 separate sell segments separated by a gap');

  // Verify Segment 1: from t=baseT to t=baseT+20 at price 904.0
  const seg1 = sellSeriesList[0];
  assert.equal(seg1.data[0].time, baseT);
  assert.equal(seg1.data[0].value, 904.0);
  assert.equal(seg1.data[seg1.data.length - 1].time, baseT + 20);
  assert.equal(seg1.data[seg1.data.length - 1].value, 904.0);

  // Verify Segment 2: from t=baseT+40 to t=baseT+60 at price 906.0 (跳空新价位)
  const seg2 = sellSeriesList[1];
  assert.equal(seg2.data[0].time, baseT + 40);
  assert.equal(seg2.data[0].value, 906.0);
  assert.equal(seg2.data[seg2.data.length - 1].time, baseT + 60);
  assert.equal(seg2.data[seg2.data.length - 1].value, 906.0);

  // Assert that NO data point exists during the cancellation gap (baseT+21 to baseT+39)
  const allSellTimes = [...seg1.data.map(p => p.time), ...seg2.data.map(p => p.time)];
  for (let t = baseT + 21; t <= baseT + 39; t++) {
    assert.equal(allSellTimes.includes(t), false, `time ${t} must be in the gap with NO line drawn`);
  }

  // Active PriceLine should be at 906.00
  const activeSellPriceLine = priceLinesCreated.find((pl) => pl.options?.color === '#FFC772');
  assert.ok(activeSellPriceLine, 'active sell price line should exist');
  assert.equal(activeSellPriceLine.options.price, 906.0);
  assert.match(activeSellPriceLine.options.title, /卖 906\.00/);
});



