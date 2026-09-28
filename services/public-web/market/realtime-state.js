/* Reducer for the Gold display stream. It never makes strategy or order decisions. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.GoldMarketRealtimeState = factory();
}(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const WINDOWS = new Set([5, 10, 15]);

  function copy(value) {
    if (value == null || typeof value !== "object") return value;
    if (Array.isArray(value)) return value.map(copy);
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, copy(item)]));
  }

  function validEvent(event) {
    return Boolean(event && typeof event === "object"
      && event.schema_version === "gold-display-stream/v1"
      && event.kind === "observation"
      && typeof event.run_id === "string" && event.run_id.length > 0
      && typeof event.stream_epoch === "string" && event.stream_epoch.length > 0
      && Number.isInteger(event.source_sequence) && event.source_sequence > 0
      && typeof event.contract === "string" && event.contract.length > 0
      && Number.isInteger(event.bucket_start_ms));
  }

  function keyOf(event) {
    return `${event.contract}\u0000${event.bucket_start_ms}`;
  }

  function normalized(event) {
    if (!validEvent(event)) return null;
    return {
      ...copy(event),
      band: event.band && typeof event.band === "object" ? copy(event.band) : {},
      orders: Array.isArray(event.orders) ? copy(event.orders) : [],
    };
  }

  function create(options = {}) {
    let runId = null;
    let streamEpoch = null;
    let sourceSequence = 0;
    let continuous = false;
    let windowComplete = false;
    let windowMinutes = 5;
    let gapReason = null;
    let installed = false;
    const expectedContracts = Number.isInteger(options.expectedContracts) ? options.expectedContracts : 8;
    const clock = typeof options.clock === "function" ? options.clock : () => Date.now();
    const records = new Map();
    const onResync = typeof options.onResync === "function" ? options.onResync : () => {};

    function requestResync(reason) {
      continuous = false;
      windowComplete = false;
      gapReason = reason;
      records.clear();
      onResync({reason, run_id: runId, stream_epoch: streamEpoch, source_sequence: sourceSequence});
      return {action: "resync", reason};
    }

    function put(event) {
      const key = keyOf(event);
      const previous = records.get(key);
      if (!previous || previous.source_sequence < event.source_sequence) records.set(key, event);
    }

    function refreshWindowCompleteness(nowMs) {
      const cutoff = nowMs - windowMinutes * 60 * 1000;
      for (const [key, event] of records) {
        if (event.bucket_start_ms < cutoff) records.delete(key);
      }
      const current = [...records.values()];
      const contracts = new Set(current.map((event) => event.contract));
      const earliest = current.reduce((value, event) => Math.min(value, event.bucket_start_ms), Infinity);
      windowComplete = continuous && Number.isFinite(earliest) && earliest <= cutoff
        && contracts.size >= expectedContracts;
    }

    function install(snapshot) {
      if (!snapshot || snapshot.schema_version !== "gold-display-bootstrap/v1"
          || !WINDOWS.has(Number(snapshot.window_minutes))
          || (snapshot.run_id != null && typeof snapshot.run_id !== "string")
          || (snapshot.stream_epoch != null && typeof snapshot.stream_epoch !== "string")
          || !Number.isInteger(snapshot.source_sequence) || snapshot.source_sequence < 0
          || !Array.isArray(snapshot.events)) {
        return requestResync("INVALID_BOOTSTRAP");
      }
      runId = snapshot.run_id || null;
      streamEpoch = snapshot.stream_epoch || null;
      sourceSequence = snapshot.source_sequence;
      continuous = snapshot.continuous === true;
      windowComplete = snapshot.window_complete === true;
      windowMinutes = Number(snapshot.window_minutes);
      gapReason = snapshot.gap_reason || null;
      records.clear();
      const ordered = snapshot.events.map(normalized).filter(Boolean)
        .sort((a, b) => a.source_sequence - b.source_sequence);
      // Before the first observation there is no run identity to join yet.
      // Wait for a publication to trigger a fresh bootstrap instead of
      // recursively retrying the same empty snapshot.
      if (continuous && !runId && sourceSequence === 0 && snapshot.events.length === 0 && !windowComplete) {
        continuous = false;
        gapReason = "WAITING_FOR_SOURCE";
      }
      if (continuous && (!runId || !streamEpoch)) return requestResync("INVALID_BOOTSTRAP_IDENTITY");
      if (ordered.length && (!runId || !streamEpoch)) return requestResync("INVALID_BOOTSTRAP_IDENTITY");
      let previousSequence = null;
      for (const event of ordered) {
        if ((runId && event.run_id !== runId) || (streamEpoch && event.stream_epoch !== streamEpoch)
            || event.source_sequence > sourceSequence) {
          return requestResync("INVALID_BOOTSTRAP_EVENT");
        }
        if (previousSequence != null && event.source_sequence !== previousSequence + 1) {
          return requestResync("BOOTSTRAP_SEQUENCE_GAP");
        }
        previousSequence = event.source_sequence;
        put(event);
      }
      installed = true;
      if (continuous && snapshot.window_complete !== true) refreshWindowCompleteness(
        Number.isFinite(Number(snapshot.captured_at_ms)) ? Number(snapshot.captured_at_ms) : clock());
      return {action: "bootstrap", complete: continuous && windowComplete,
        event_count: records.size, source_sequence: sourceSequence};
    }

    function receive(raw) {
      if (!installed) return {action: "buffer"};
      const event = normalized(raw);
      if (!event) return requestResync("INVALID_EVENT");
      if (event.run_id !== runId) return requestResync("RUN_CHANGED");
      if (event.stream_epoch !== streamEpoch) return requestResync("STREAM_EPOCH_CHANGED");
      if (event.reset_reason) return requestResync(String(event.reset_reason));
      if (!continuous) return requestResync(gapReason || "STREAM_NOT_CONTINUOUS");
      if (event.source_sequence <= sourceSequence) return {action: "duplicate"};
      if (event.source_sequence !== sourceSequence + 1) return requestResync("SOURCE_SEQUENCE_GAP");
      sourceSequence = event.source_sequence;
      put(event);
      refreshWindowCompleteness(clock());
      return {action: "applied", event: copy(event), source_sequence: sourceSequence};
    }

    function eventsFor(contract) {
      return [...records.values()].filter((event) => !contract || event.contract === contract)
        .sort((a, b) => a.bucket_start_ms - b.bucket_start_ms
          || a.source_sequence - b.source_sequence).map(copy);
    }

    function seriesFor(contract) {
      const latestByBucket = new Map();
      for (const event of eventsFor(contract)) {
        const key = String(event.bucket_start_ms);
        const old = latestByBucket.get(key);
        if (!old || old.source_sequence < event.source_sequence) latestByBucket.set(key, event);
      }
      return [...latestByBucket.values()].sort((a, b) => a.bucket_start_ms - b.bucket_start_ms)
        .map((event) => ({
          event: copy(event),
          price: Number.isFinite(Number(event.price)) ? {time: event.bucket_start_ms / 1000, value: Number(event.price)} : null,
          band: Object.fromEntries(["upper", "center", "lower"].map((name) => [name,
            Number.isFinite(Number(event.band?.[name]))
              ? {time: event.bucket_start_ms / 1000, value: Number(event.band[name])} : null])),
        }));
    }

    function metadata() {
      return {run_id: runId, stream_epoch: streamEpoch, source_sequence: sourceSequence,
        continuous, window_complete: continuous && windowComplete, window_minutes: windowMinutes,
        gap_reason: gapReason, installed};
    }

    return {install, receive, eventsFor, seriesFor, metadata,
      reset: () => {
        installed = false; continuous = false; windowComplete = false; records.clear();
        runId = null; streamEpoch = null; sourceSequence = 0; gapReason = null;
      },
      size: () => records.size};
  }

  return {create, validEvent};
}));
