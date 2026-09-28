/* Centrifuge transport: server-side subscription, then one atomic bootstrap. */
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.GoldMarketRealtimeStream = factory(root);
}(typeof self !== "undefined" ? self : this, function (global) {
  "use strict";

  function endpointFromLocation() {
    if (global && global.GoldMarketStreamConfig?.endpoint) return global.GoldMarketStreamConfig.endpoint;
    if (!global || !global.location) return "ws://127.0.0.1/connection/websocket";
    return `${global.location.protocol === "https:" ? "wss:" : "ws:"}//${global.location.host}/connection/websocket`;
  }

  function create(options = {}) {
    const CentrifugeClass = options.CentrifugeClass || global?.Centrifuge;
    const endpoint = options.endpoint || endpointFromLocation();
    const channel = options.channel || "gold:market";
    const fetchToken = options.fetchToken;
    const fetchBootstrap = options.fetchBootstrap;
    const onSnapshot = typeof options.onSnapshot === "function" ? options.onSnapshot : () => {};
    const onEvent = typeof options.onEvent === "function" ? options.onEvent : () => {};
    const onStatus = typeof options.onStatus === "function" ? options.onStatus : () => {};
    const resync = typeof options.resync === "function" ? options.resync : null;
    const bufferLimit = Number.isInteger(options.bufferLimit) ? options.bufferLimit : 4096;
    const subscriptionTimeoutMs = options.subscriptionTimeoutMs || 5000;
    let client = null;
    let stopped = false;
    let subscribed = false;
    let ready = false;
    let bootstrapping = false;
    let bootstrapAgain = false;
    let bootstrapPromise = null;
    let startPromise = null;
    let timer = null;
    let buffered = [];
    let resolveSubscribed;
    let rejectSubscribed;
    const subscribedPromise = new Promise((resolve, reject) => {
      resolveSubscribed = resolve;
      rejectSubscribed = reject;
    });

    function fail(error) {
      onStatus({state: "error", error});
      return error;
    }

    function publication(ctx) {
      if (!ctx || ctx.channel !== channel || !ctx.data) return;
      if (bootstrapping || !ready || !subscribed) {
        buffered.push(ctx.data);
        if (buffered.length > bufferLimit) {
          buffered = [];
          bootstrapAgain = true;
          onStatus({state: "gap", reason: "CLIENT_BUFFER_OVERFLOW"});
        }
        return;
      }
      onEvent(ctx.data);
    }

    function bootstrap(reason) {
      if (stopped) return false;
      if (bootstrapping) { bootstrapAgain = true; return bootstrapPromise; }
      bootstrapping = true;
      ready = false;
      onStatus({state: "bootstrapping", reason: reason || "initial"});
      bootstrapPromise = (async () => {
        try {
          do {
            bootstrapAgain = false;
            const snapshot = await fetchBootstrap();
            if (stopped) return false;
            onSnapshot(snapshot);
            // A publication can arrive while onSnapshot or onEvent is painting.
            // Drain until the buffer stays empty so the bootstrap seam cannot
            // silently lose a source sequence.
            do {
              const pending = buffered.splice(0).sort((a, b) => (a.source_sequence || 0) - (b.source_sequence || 0));
              for (const event of pending) onEvent(event);
            } while (buffered.length);
          } while (bootstrapAgain && !stopped);
          ready = !stopped;
          return ready;
        } catch (error) {
          buffered = [];
          ready = false;
          fail(error);
          if (resync) resync(error);
          return false;
        } finally {
          bootstrapping = false;
          bootstrapPromise = null;
        }
      })();
      return bootstrapPromise;
    }

    function subscribedEvent(ctx) {
      if (!ctx || ctx.channel !== channel || subscribed) return;
      subscribed = true;
      if (timer != null) { clearTimeout(timer); timer = null; }
      onStatus({state: "subscribed", channel});
      void bootstrap("subscribed").then((ready) => {
        if (ready) resolveSubscribed(ctx);
        else rejectSubscribed(new Error("market stream bootstrap failed"));
      });
    }

    function start() {
      if (stopped) return Promise.reject(new Error("stream stopped"));
      if (startPromise) return startPromise;
      if (!CentrifugeClass || typeof fetchToken !== "function" || typeof fetchBootstrap !== "function") {
        return Promise.reject(new Error("realtime stream dependencies unavailable"));
      }
      onStatus({state: "token"});
      startPromise = Promise.resolve(fetchToken()).then((tokenBody) => {
        if (!tokenBody?.token) throw new Error("market stream token missing");
        client = new CentrifugeClass(endpoint, {
          token: tokenBody.token,
          getToken: async () => {
            let refreshed;
            try { refreshed = await fetchToken(); }
            catch (error) {
              if ((error?.cause === "login" || error?.cause === "forbidden")
                  && typeof CentrifugeClass.UnauthorizedError === "function") {
                throw new CentrifugeClass.UnauthorizedError();
              }
              throw error;
            }
            if (!refreshed?.token) throw new Error("market stream token refresh missing");
            return refreshed.token;
          },
        });
        client.on("publication", publication);
        client.on("subscribed", subscribedEvent);
        client.on("connecting", (ctx) => onStatus({state: "connecting", context: ctx}));
        client.on("connected", (ctx) => onStatus({state: "connected", context: ctx}));
        client.on("disconnected", (ctx) => {
          subscribed = false;
          ready = false;
          buffered = [];
          onStatus({state: "disconnected", context: ctx});
        });
        client.on("subscribing", (ctx) => {
          subscribed = false;
          ready = false;
          onStatus({state: "subscribing", context: ctx});
        });
        client.on("unsubscribed", (ctx) => {
          subscribed = false;
          ready = false;
          buffered = [];
          onStatus({state: "unsubscribed", context: ctx});
          if (resync) resync(new Error("market stream unsubscribed"));
        });
        client.on("error", (error) => onStatus({state: "error", error}));
        client.connect();
        timer = setTimeout(() => {
          if (!subscribed && !stopped) {
            const error = new Error("market stream subscription timeout");
            rejectSubscribed(error);
            fail(error);
          }
        }, subscriptionTimeoutMs);
        return subscribedPromise;
      }).catch((error) => { fail(error); startPromise = null; throw error; });
      return startPromise;
    }

    function stop() {
      stopped = true;
      if (timer != null) clearTimeout(timer);
      buffered = [];
      startPromise = null;
      if (client && typeof client.disconnect === "function") client.disconnect();
    }

    return {start, stop, bootstrap: (reason) => bootstrap(reason), endpoint,
      getClient: () => client, get bufferedCount() { return buffered.length; }};
  }

  return {create};
}));
