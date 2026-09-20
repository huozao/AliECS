"""Write the 8-contract realtime fixture into a real Gold review archive.

``market_realtime_fixture`` builds the *response* a browser test can serve
directly.  This module builds the *evidence* instead: packets imported into a
temporary Gold ``ReviewArchive`` SQLite partition, so the cross-repository
tests can read them back through Gold's own HTTP read API, the AliECS review
source and the gateway without any part of the chain being faked.

The window is anchored on the wall clock because Gold's ``/realtime`` recomputes
its window from ``now`` on every request; a frozen 2026 fixture would fall out
of every window the server is willing to serve.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from market_realtime_fixture import CONTRACTS, STEP_SECONDS, WINDOW_MINUTES

RUN_ID = "run-archive-e2e"
#: The same t+.125 place → t+.500 reprice → t+.750 cancel → next-second replace
#: shape as the browser fixture, at the same 30 second cadence.
ORDER_CADENCE_SECONDS = 30
ORDER_OFFSETS = (
    (0.125, -2.0, +2.0, "ACTIVE｜挂单有效"),
    (0.500, -1.8, +1.8, "ACTIVE｜挂单有效"),
    (0.750, None, None, "CANCELLED｜已撤单"),
    (1.000, -2.2, +2.2, "ACTIVE｜挂单有效"),
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _base_price(index: int) -> float:
    return 900.0 + index * 5.0


def _orders(index: int, buy_delta, sell_delta, status) -> list[dict]:
    center = _base_price(index)
    return [
        {"side": "buy", "status": status,
         "price": None if buy_delta is None else round(center + buy_delta, 2)},
        {"side": "sell", "status": status,
         "price": None if sell_delta is None else round(center + sell_delta, 2)},
    ]


def build_packets(window_end: datetime) -> list[dict]:
    """Every packet the window needs, in published order.

    One packet per observed instant carries all eight contracts, which is how
    the live publisher batches them.  Order transitions land on their own
    instants so the ``.125``/``.750`` edges survive the per-second bucketing
    the price series applies.
    """
    window_start = window_end - timedelta(minutes=WINDOW_MINUTES)
    by_instant: dict[datetime, dict] = {}

    steps = int(WINDOW_MINUTES * 60 / STEP_SECONDS)
    for step in range(steps):
        moment = window_start + timedelta(seconds=step * STEP_SECONDS)
        quotes, bands = [], []
        for index, contract in enumerate(CONTRACTS):
            price = _base_price(index) + (step % 120) * 0.02 - 1.2
            center = _base_price(index) + (step % 120) * 0.02
            stamp = _iso(moment)
            quotes.append({
                "contract": contract, "observed_at": stamp, "source_time": stamp,
                "last_price": round(price, 2),
                "ohlc": {"high": round(price + 0.04, 2), "low": round(price - 0.04, 2)},
                # Held order state, so a poll that lands between transitions
                # still reports what is outstanding.
                "orders": _orders(index, -2.0, +2.0, "ACTIVE｜挂单有效"),
                "order_history_mode": "snapshot",
                "order_history_truncated": False,
            })
            bands.append({
                "contract": contract, "observed_at": stamp, "source_time": stamp,
                "center": round(center, 2), "lower": round(center - 2.0, 2),
                "upper": round(center + 2.0, 2), "fit_available": True,
            })
        by_instant[moment] = {"quotes": quotes, "bands": bands}

    for second in range(0, WINDOW_MINUTES * 60, ORDER_CADENCE_SECONDS):
        origin = window_start + timedelta(seconds=second)
        for offset, buy_delta, sell_delta, status in ORDER_OFFSETS:
            moment = origin + timedelta(seconds=offset)
            if moment > window_end:
                continue
            stamp = _iso(moment)
            quotes = []
            for index, contract in enumerate(CONTRACTS):
                price = _base_price(index) + (second % 120) * 0.02 - 1.2
                quotes.append({
                    "contract": contract, "observed_at": stamp, "source_time": stamp,
                    "last_price": round(price, 2),
                    "orders": _orders(index, buy_delta, sell_delta, status),
                    "order_history_mode": "snapshot",
                    "order_history_truncated": False,
                })
            # An order transition may share an instant with a price observation;
            # the later write wins for that contract list, which is what the
            # publisher does too.
            existing = by_instant.setdefault(moment, {"quotes": [], "bands": []})
            existing["quotes"] = quotes

    packets = []
    for sequence, moment in enumerate(sorted(by_instant), start=1):
        payload = by_instant[moment]
        packets.append({
            "schema_version": "market-review.v1", "model_version": "V6.0",
            "run_id": RUN_ID, "sequence": sequence, "published_at": _iso(moment),
            "quotes": payload["quotes"], "bands": payload["bands"], "events": [],
        })
    return packets


def populate(archive, window_end: datetime) -> dict:
    """Import every packet in one archive batch and report what was written."""
    packets = build_packets(window_end)
    with archive.batch():
        for packet in packets:
            archive.import_packet(packet, source="fixture")
    return {
        "run_id": RUN_ID,
        "packet_count": len(packets),
        "quote_observations": sum(len(packet["quotes"]) for packet in packets),
        "band_observations": sum(len(packet["bands"]) for packet in packets),
        "window_end": window_end,
        "window_start": window_end - timedelta(minutes=WINDOW_MINUTES),
    }
