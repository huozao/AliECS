const test = require('node:test');
const assert = require('node:assert/strict');

const {create} = require('../services/public-web/market/realtime-stream.js');
const {create: createState} = require('../services/public-web/market/realtime-state.js');

class FakeCentrifuge {
  constructor(endpoint, options) {
    this.endpoint = endpoint;
    this.options = options;
    this.listeners = new Map();
    FakeCentrifuge.instance = this;
  }
  on(name, callback) { this.listeners.set(name, callback); return this; }
  connect() { this.listeners.get('connecting')?.({}); }
  disconnect() { this.disconnected = true; }
  emit(name, value) { this.listeners.get(name)?.(value); }
}

const snapshot = (sequence = 1) => ({
  schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1', stream_epoch: 'epoch-1',
  source_sequence: sequence, continuous: true, window_complete: false, window_minutes: 5, events: [],
});

const publication = (sequence) => ({
  schema_version: 'gold-display-stream/v1', kind: 'observation', run_id: 'run-1',
  stream_epoch: 'epoch-1', source_sequence: sequence, contract: 'SHFE.au2610',
  bucket_start_ms: 1000 + sequence * 500, price: 900, band: {}, orders: [],
});

test('stream subscribes before bootstrap and replays publications received during bootstrap', async () => {
  const calls = [];
  let release;
  const waiting = new Promise((resolve) => { release = resolve; });
  const stream = create({
    CentrifugeClass: FakeCentrifuge,
    endpoint: 'ws://example.com/connection/websocket',
    fetchToken: async () => ({token: 'short-lived'}),
    fetchBootstrap: async () => { calls.push('bootstrap'); await waiting; return snapshot(1); },
    onSnapshot: (value) => calls.push(['snapshot', value.source_sequence]),
    onEvent: (value) => calls.push(['event', value.source_sequence]),
  });
  const started = stream.start();
  await new Promise(setImmediate);
  const client = FakeCentrifuge.instance;
  client.emit('subscribed', {channel: 'gold:market'});
  await new Promise(setImmediate);
  client.emit('publication', {channel: 'gold:market', data: publication(2)});
  release();
  await started;
  assert.equal(JSON.stringify(calls), JSON.stringify(['bootstrap', ['snapshot', 1], ['event', 2]]));
  assert.equal(client.options.token, 'short-lived');
  assert.equal(client.endpoint, 'ws://example.com/connection/websocket');
  stream.stop();
  assert.equal(client.disconnected, true);
});

test('unrelated channels are ignored and reconnect causes a fresh bootstrap', async () => {
  const snapshots = [];
  const stream = create({
    CentrifugeClass: FakeCentrifuge,
    fetchToken: async () => ({token: 't'}),
    fetchBootstrap: async () => { snapshots.push(true); return snapshot(1); },
    onSnapshot: () => {}, onEvent: () => {}, subscriptionTimeoutMs: 100,
  });
  const started = stream.start();
  await new Promise(setImmediate);
  const client = FakeCentrifuge.instance;
  client.emit('publication', {channel: 'other', data: publication(2)});
  client.emit('subscribed', {channel: 'gold:market'});
  await started;
  client.emit('disconnected', {code: 1});
  client.emit('subscribed', {channel: 'gold:market'});
  await new Promise(setImmediate);
  assert.equal(snapshots.length, 2);
  assert.equal(stream.bufferedCount, 0);
  stream.stop();
});

test('missing dependencies fail before creating a client', async () => {
  const stream = create({fetchToken: async () => ({token: 't'}), fetchBootstrap: async () => snapshot()});
  await assert.rejects(stream.start(), /dependencies unavailable/);
});

test('publication arriving while snapshot callback runs is drained before readiness', async () => {
  const seen = [];
  const stream = create({
    CentrifugeClass: FakeCentrifuge,
    fetchToken: async () => ({token: 't'}),
    fetchBootstrap: async () => snapshot(1),
    onSnapshot: () => FakeCentrifuge.instance.emit('publication', {
      channel: 'gold:market', data: publication(2),
    }),
    onEvent: (value) => seen.push(value.source_sequence),
  });
  const started = stream.start();
  await new Promise(setImmediate);
  FakeCentrifuge.instance.emit('subscribed', {channel: 'gold:market'});
  await started;
  assert.deepEqual(seen, [2]);
  assert.equal(stream.bufferedCount, 0);
  stream.stop();
});

test('empty source waits for first publication without looping bootstrap requests', async () => {
  let calls = 0;
  let stream;
  const reducer = createState({onResync: ({reason}) => { void stream.bootstrap(reason); }});
  stream = create({
    CentrifugeClass: FakeCentrifuge,
    fetchToken: async () => ({token: 't'}),
    fetchBootstrap: async () => {
      calls += 1;
      if (calls > 3) throw new Error('bootstrap loop');
      return calls === 1 ? {
        schema_version: 'gold-display-bootstrap/v1', run_id: null, stream_epoch: 'epoch-0',
        source_sequence: 0, continuous: true, window_complete: false,
        window_minutes: 5, events: [],
      } : {...snapshot(1), events: [publication(1)]};
    },
    onSnapshot: (value) => reducer.install(value),
    onEvent: (value) => reducer.receive(value),
  });
  const started = stream.start();
  await new Promise(setImmediate);
  FakeCentrifuge.instance.emit('subscribed', {channel: 'gold:market'});
  await started;
  assert.equal(calls, 1);
  assert.equal(reducer.metadata().installed, true);
  assert.equal(reducer.metadata().continuous, false);

  FakeCentrifuge.instance.emit('publication', {channel: 'gold:market', data: publication(1)});
  await new Promise(setImmediate);
  assert.equal(calls, 2);
  assert.equal(reducer.metadata().run_id, 'run-1');
  assert.equal(reducer.metadata().continuous, true);
  stream.stop();
});
