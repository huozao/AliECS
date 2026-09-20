"""Frozen 8-contract realtime fixture: 15 minutes at 0.5s, with real bands and orders.

The previous browser fixture carried one contract, one quote and no bands, so
the chart area was legitimately empty while the assertions — which only read
label text — still passed.  Anything that claims the palette or the order
ladder is correct has to be drawn from data like this instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

CONTRACTS = (
    "SHFE.au2510", "SHFE.au2512", "SHFE.au2602", "SHFE.au2604",
    "SHFE.au2606", "SHFE.au2608", "SHFE.au2610", "SHFE.au2612",
)
WINDOW_MINUTES = 15
STEP_SECONDS = 0.5
WINDOW_END = datetime(2026, 9, 10, 1, 0, 0, tzinfo=timezone.utc)
WINDOW_START = WINDOW_END - timedelta(minutes=WINDOW_MINUTES)
RUN_ID = "run-fixture-8"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _base_price(index: int) -> float:
    return 900.0 + index * 5.0


def series_for(contract: str, index: int) -> dict:
    """One quote and one band every 0.5s across the whole window."""
    quotes, bands = [], []
    steps = int(WINDOW_MINUTES * 60 / STEP_SECONDS)
    for step in range(steps):
        moment = WINDOW_START + timedelta(seconds=step * STEP_SECONDS)
        # A slow drift plus a small oscillation, so the line is visibly a line.
        price = _base_price(index) + (step % 120) * 0.02 - 1.2
        stamp = _iso(moment)
        quotes.append({
            "last_price": round(price, 2), "observed_at": stamp, "source_time": stamp,
            "snapshot_sequence": step,
            "ohlc": {"high": round(price + 0.04, 2), "low": round(price - 0.04, 2)},
        })
        center = _base_price(index) + (step % 120) * 0.02
        bands.append({
            "contract": contract, "center": round(center, 2),
            "lower": round(center - 2.0, 2), "upper": round(center + 2.0, 2),
            "observed_at": stamp, "source_time": stamp, "snapshot_sequence": step,
        })
    return {"quotes": quotes, "bands": bands}


def _leg(side: str, price: float | None, status: str) -> dict:
    """``MarketObservation.order`` matches on ``side`` and an EFFECTIVE-like status."""
    return {"side": side, "status": status,
            "price": None if price is None else round(price, 2)}


def order_series_for(contract: str, index: int) -> list[dict]:
    """The t+.125 place → t+.500 reprice → t+.750 cancel → next-second replace shape."""
    rows = []
    for second in range(0, WINDOW_MINUTES * 60, 30):
        origin = WINDOW_START + timedelta(seconds=second)
        center = _base_price(index)
        for offset, buy_price, sell_price, status in (
            (0.125, center - 2.0, center + 2.0, "ACTIVE｜挂单有效"),
            (0.500, center - 1.8, center + 1.8, "ACTIVE｜挂单有效"),
            (0.750, None, None, "CANCELLED｜已撤单"),
            (1.000, center - 2.2, center + 2.2, "ACTIVE｜挂单有效"),
        ):
            stamp = _iso(origin + timedelta(seconds=offset))
            rows.append({
                "contract": contract, "observed_at": stamp, "source_time": stamp,
                "orders": [_leg("buy", buy_price, status), _leg("sell", sell_price, status)],
                "order_history": [],
            })
    return rows


def realtime_body(*, window_minutes: int = WINDOW_MINUTES) -> dict:
    quotes, bands, series, orders, order_series, ranking = [], [], {}, [], {}, []
    for index, contract in enumerate(CONTRACTS):
        data = series_for(contract, index)
        series[contract] = data
        quotes.append({"contract": contract, **data["quotes"][-1]})
        bands.append(data["bands"][-1])
        rows = order_series_for(contract, index)
        order_series[contract] = rows
        latest = {item["side"]: item for item in rows[-1]["orders"]}
        orders.append({"contract": contract, "buy": latest["buy"], "sell": latest["sell"]})
        ranking.append({
            "target_contract": contract,
            "contract": CONTRACTS[(index + 1) % len(CONTRACTS)],
            "rank": 1, "reason": "SYNTHETIC｜合成夹具",
        })
    return {
        "window_minutes": window_minutes,
        "window_start": _iso(WINDOW_START), "window_end": _iso(WINDOW_END),
        "run_id": RUN_ID, "quotes": quotes, "bands": bands, "series": series,
        "orders": orders, "order_series": order_series, "orders_truncated": False,
        "hedge_ranking": ranking,
        "freshness": {"published_at": _iso(WINDOW_END), "received_at": _iso(WINDOW_END)},
        "next_cursor": None, "truncated": False,
    }


#: Appended to the *real* vendor bundle by the test's network layer, so every
#: call still runs the genuine implementation and only its arguments are seen.
#: Patching from an init script is not reliable: the bundle defines the global
#: itself, and the page's own script runs immediately after it.
CHART_PROBE = """
;(function () {
  window.__chartProbe = {created: 0, series: 0, setData: [], byChart: {}};
  const real = window.LightweightCharts;
  if (!real || !real.createChart) return;
  // 5.0.8 的导出对象是 Object.freeze 过的，createChart 不可写，就地改会静默失败。
  // 所以复制一份可写的外壳，createChart 仍然转调真实实现。
  const wrapper = {};
  for (const key of Object.keys(real)) wrapper[key] = real[key];
  wrapper.createChart = function (...args) {
    const chartIndex = window.__chartProbe.created;
    window.__chartProbe.created += 1;
    window.__chartProbe.byChart[chartIndex] = [];
    const chart = real.createChart.apply(real, args);
    const realAdd = chart.addSeries.bind(chart);
    chart.addSeries = function (...seriesArgs) {
      window.__chartProbe.series += 1;
      const series = realAdd(...seriesArgs);
      const realSetData = series.setData.bind(series);
      series.setData = function (data) {
        const count = Array.isArray(data) ? data.length : 0;
        window.__chartProbe.setData.push(count);
        window.__chartProbe.byChart[chartIndex].push(count);
        return realSetData(data);
      };
      return series;
    };
    return chart;
  };
  window.LightweightCharts = wrapper;
})();
"""
