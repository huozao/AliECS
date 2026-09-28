const test = require('node:test');
const assert = require('node:assert/strict');

const {create} = require('../services/public-web/market/realtime-state.js');

const event = (sequence, bucket, extra = {}) => ({
  schema_version: 'gold-display-stream/v1', kind: 'observation', run_id: 'run-1',
  stream_epoch: 'epoch-1', source_sequence: sequence, contract: 'SHFE.au2610',
  bucket_start_ms: bucket, price: 900 + sequence,
  band: {upper: 902 + sequence, center: 901 + sequence, lower: 900 + sequence},
  orders: [], ...extra,
});

test('bootstrap and stream preserve .000 and .500 display buckets', () => {
  const state = create();
  const result = state.install({schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1',
    stream_epoch: 'epoch-1', source_sequence: 8, continuous: true, window_complete: true,
    window_minutes: 5, events: [event(7, 1790575200000), event(8, 1790575200500)]});
  assert.equal(result.complete, true);
  const series = state.seriesFor('SHFE.au2610');
  assert.deepEqual(series.map((row) => row.price.time), [1790575200, 1790575200.5]);
});

test('bootstrap may join a running source at an arbitrary sequence', () => {
  const state = create();
  assert.equal(state.install({schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1',
    stream_epoch: 'epoch-1', source_sequence: 8021, continuous: true, window_complete: false,
    window_minutes: 5, events: [event(8021, 1790575200000)]}).action, 'bootstrap');
  assert.equal(state.receive(event(8022, 1790575200500)).action, 'applied');
  assert.equal(state.metadata().source_sequence, 8022);
});

test('a sequence gap stops continuity and asks for a new bootstrap', () => {
  const reasons = [];
  const state = create({onResync: ({reason}) => reasons.push(reason)});
  state.install({schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1', stream_epoch: 'epoch-1',
    source_sequence: 1, continuous: true, window_complete: false, window_minutes: 5, events: [event(1, 1000)]});
  assert.equal(state.receive(event(3, 2000)).action, 'resync');
  assert.deepEqual(reasons, ['SOURCE_SEQUENCE_GAP']);
  assert.equal(state.metadata().continuous, false);
  assert.equal(state.receive(event(4, 2500)).action, 'resync');
});

test('duplicate sequence is idempotent and a new epoch is never merged', () => {
  const state = create();
  state.install({schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1', stream_epoch: 'epoch-1',
    source_sequence: 1, continuous: true, window_complete: false, window_minutes: 5, events: [event(1, 1000)]});
  assert.equal(state.receive(event(1, 1000)).action, 'duplicate');
  assert.equal(state.receive(event(2, 1500, {stream_epoch: 'epoch-2'})).reason, 'STREAM_EPOCH_CHANGED');
});

test('a higher source sequence revises the same bucket without adding a second point', () => {
  const state = create({clock: () => 1500});
  state.install({schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1', stream_epoch: 'epoch-1',
    source_sequence: 1, continuous: true, window_complete: false, window_minutes: 5, events: [event(1, 1000)]});
  state.receive(event(2, 1000));
  const rows = state.seriesFor('SHFE.au2610');
  assert.equal(rows.length, 1);
  assert.equal(rows[0].price.value, 902);
});

test('bootstrap sequence gaps and invalid identities fail closed and clear old records', () => {
  const reasons = [];
  const state = create({onResync: ({reason}) => reasons.push(reason)});
  state.install({schema_version: 'gold-display-bootstrap/v1', run_id: 'run-1', stream_epoch: 'epoch-1',
    source_sequence: 3, continuous: true, window_complete: false, window_minutes: 5,
    events: [event(1, 1000), event(3, 2000)]});
  assert.deepEqual(reasons, ['BOOTSTRAP_SEQUENCE_GAP']);
  assert.equal(state.size(), 0);
  assert.equal(state.metadata().continuous, false);
  assert.equal(state.install({schema_version: 'gold-display-bootstrap/v1', run_id: null,
    stream_epoch: 'epoch-1', source_sequence: 0, continuous: true, window_complete: false,
    window_minutes: 5, events: [event(1, 1000)]}).reason, 'INVALID_BOOTSTRAP_IDENTITY');
});
