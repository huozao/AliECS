"""定时全量的锚点调度：把同步固定到夜间，避开白天工作时段。

锚点时刻按北京时间解释（容器内是 UTC）；不设锚点时保持"跑完睡一个周期"的原行为。
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tplus_datahub.jobs.worker_loop import (
    BEIJING,
    _normalize_anchor_time,
    _resolve_sync_config,
    next_scheduled_full_due,
    run_forever,
)


def _beijing(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=BEIJING).astimezone(timezone.utc)


class NextDueTests(unittest.TestCase):
    def test_never_run_goes_immediately(self):
        now = _beijing(2026, 8, 5, 14)
        self.assertEqual(next_scheduled_full_due(now, None, 86400, "02:00"), now)

    def test_without_anchor_keeps_relative_interval(self):
        last = _beijing(2026, 8, 5, 14)
        self.assertEqual(
            next_scheduled_full_due(last, last, 86400, ""),
            last + timedelta(seconds=86400),
        )

    def test_anchor_pulls_next_run_to_that_beijing_hour(self):
        """白天 14:00 跑过一次后，下一次应落在次日凌晨 02:00 北京时间。"""
        last = _beijing(2026, 8, 5, 14)
        due = next_scheduled_full_due(last, last, 86400, "02:00")
        self.assertEqual(due, _beijing(2026, 8, 6, 2))
        self.assertEqual(due.astimezone(BEIJING).hour, 2)

    def test_anchor_is_beijing_not_utc(self):
        """02:00 必须是北京时间；若误按 UTC 处理会落在北京 10:00 的工作时段。"""
        last = _beijing(2026, 8, 5, 14)
        due = next_scheduled_full_due(last, last, 86400, "02:00")
        self.assertNotEqual(due.astimezone(BEIJING).hour, 10)
        self.assertEqual(due.utcoffset(), timedelta(0))
        self.assertEqual(due.hour, 18)  # 北京 02:00 = 前一日 UTC 18:00

    def test_run_just_before_anchor_snaps_to_the_same_day_anchor(self):
        """01:30 跑过一次，相位对齐会让它 30 分钟后补到当天 02:00，而不是等一整天。"""
        last = _beijing(2026, 8, 5, 1, 30)
        self.assertEqual(next_scheduled_full_due(last, last, 86400, "02:00"), _beijing(2026, 8, 5, 2))

    def test_run_exactly_at_anchor_moves_to_next_day(self):
        last = _beijing(2026, 8, 5, 2)
        self.assertEqual(next_scheduled_full_due(last, last, 86400, "02:00"), _beijing(2026, 8, 6, 2))

    def test_twelve_hour_interval_keeps_anchor_phase(self):
        last = _beijing(2026, 8, 5, 3)
        self.assertEqual(next_scheduled_full_due(last, last, 43200, "02:00"), _beijing(2026, 8, 5, 14))

    def test_naive_last_full_is_treated_as_utc(self):
        """DB 里取到的 started_at 可能是 naive，不能因此崩掉调度。"""
        naive = datetime(2026, 8, 5, 6, 0)
        due = next_scheduled_full_due(_beijing(2026, 8, 5, 20), naive, 86400, "02:00")
        self.assertIsNotNone(due.tzinfo)


class AnchorNormalizeTests(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(_normalize_anchor_time("02:00"), "02:00")
        self.assertEqual(_normalize_anchor_time("2:5"), "02:05")
        self.assertEqual(_normalize_anchor_time(" 23:59 "), "23:59")

    def test_invalid_values_fall_back_to_no_anchor(self):
        for bad in ["", None, "24:00", "12:60", "abc", "12", "12:00:00", "-1:00"]:
            self.assertEqual(_normalize_anchor_time(bad), "", msg=f"{bad!r} 应视为未设锚点")


class ResolveConfigTests(unittest.TestCase):
    def test_anchor_read_from_config(self):
        enabled, interval, anchor = _resolve_sync_config(
            lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"}
        )
        self.assertTrue(enabled)
        self.assertEqual(interval, 86400)
        self.assertEqual(anchor, "02:00")

    def test_missing_anchor_defaults_empty(self):
        _, _, anchor = _resolve_sync_config(lambda: {"enabled": True, "interval_seconds": 86400})
        self.assertEqual(anchor, "")

    def test_config_error_falls_back_without_anchor(self):
        def boom():
            raise RuntimeError("db down")

        enabled, _, anchor = _resolve_sync_config(boom)
        self.assertTrue(enabled)
        self.assertEqual(anchor, "")


class RunForeverAnchorTests(unittest.TestCase):
    def test_skips_full_sync_when_not_due(self):
        """容器在白天重建时，不该立刻补跑一次全量。"""
        calls = []
        sleeps = []
        last_full = _beijing(2026, 8, 5, 2)
        run_forever(
            sync_once=lambda: calls.append("sync") or 0,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"},
            read_last_full=lambda: last_full,
            now=lambda: _beijing(2026, 8, 5, 14),
            sleep=sleeps.append,
            max_runs=2,
        )
        self.assertEqual(calls, [])
        self.assertTrue(sleeps)
        self.assertEqual(sum(sleeps), 12 * 3600)  # 从北京 14:00 睡到次日 02:00

    def test_sleep_after_full_run_targets_next_anchor(self):
        """跑完必须睡到下一个锚点。睡固定一个周期会整轮错过锚点，锚点永远收敛不了
        （2026-08-05 生产实测：08-04 18:38 跑完睡 86400 秒，08-05 02:00 那次直接没跑）。"""
        sleeps = []
        run_forever(
            sync_once=lambda: 0,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"},
            read_last_full=lambda: _beijing(2026, 8, 3, 2),
            now=lambda: _beijing(2026, 8, 4, 18, 38),
            sleep=sleeps.append,
            max_runs=2,
        )
        self.assertEqual(sum(sleeps), 7 * 3600 + 22 * 60)  # 18:38 → 次日 02:00

    def test_sleep_without_anchor_keeps_fixed_interval(self):
        """没设锚点时保持"跑完睡一个周期"的原行为不变。"""
        sleeps = []
        run_forever(
            sync_once=lambda: 0,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 3600, "anchor_time": ""},
            read_last_full=lambda: None,
            now=lambda: _beijing(2026, 8, 4, 18, 38),
            sleep=sleeps.append,
            max_runs=2,
        )
        self.assertEqual(sum(sleeps), 3600)

    def test_overrunning_full_sync_does_not_busy_loop(self):
        """全量跑过了锚点时刻才结束，不能算出 1 秒等待去空转，要顺延一个周期。"""
        sleeps = []
        clock = [_beijing(2026, 8, 4, 18, 38)]

        def sync_once():
            clock[0] = _beijing(2026, 8, 5, 3)  # 跑了 8 小时多，醒来已过锚点
            return 0

        run_forever(
            sync_once=sync_once,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"},
            read_last_full=lambda: _beijing(2026, 8, 3, 2),
            now=lambda: clock[0],
            sleep=sleeps.append,
            max_runs=2,
        )
        self.assertEqual(sum(sleeps), 23 * 3600)  # 03:00 → 次日 02:00，不是 1 秒

    def test_runs_when_due(self):
        calls = []
        last_full = _beijing(2026, 8, 4, 2)
        run_forever(
            sync_once=lambda: calls.append("sync") or 0,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"},
            read_last_full=lambda: last_full,
            now=lambda: _beijing(2026, 8, 5, 3),
            sleep=lambda _s: None,
            max_runs=1,
        )
        self.assertEqual(calls, ["sync"])

    def test_unreadable_last_full_runs_immediately(self):
        """读不到上次时间就按旧行为立即跑，不能因此不同步。"""
        calls = []

        def boom():
            raise RuntimeError("db down")

        run_forever(
            sync_once=lambda: calls.append("sync") or 0,
            read_sync_config=lambda: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"},
            read_last_full=boom,
            now=lambda: _beijing(2026, 8, 5, 14),
            sleep=lambda _s: None,
            max_runs=1,
        )
        self.assertEqual(calls, ["sync"])


class ScheduleHotReloadTests(unittest.TestCase):
    """睡眠中途改调度配置必须即时生效。

    2026-08-11 生产实测：08-10 17:51 那轮按当时的锚点 15:00 算出「睡到 08-11 15:00」，
    19:28 把锚点改成 01:00 之后 worker 仍在睡，08-11 01:00 那轮整轮没跑——时间线上
    连一条失败记录都没有。根因是睡眠总长只在进入睡眠时算一次，分片循环里只递减。
    """

    def _run(self, *, config_at, last_full, start, max_runs=2, poll="600"):
        calls = []
        sleeps = []
        clock = [start]

        def now():
            return clock[0]

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] = clock[0] + timedelta(seconds=seconds)

        # 生产是 30 秒一片；测试放大到 600 秒只为少转几十圈，唤醒语义不变。
        with mock.patch.dict(os.environ, {"TPLUS_SYNC_POLL_SECONDS": poll}):
            run_forever(
                sync_once=lambda: calls.append(now()) or 0,
                read_sync_config=lambda: config_at(clock[0]),
                read_last_full=lambda: last_full,
                now=now,
                sleep=sleep,
                max_runs=max_runs,
            )
        return calls, sleeps

    def test_anchor_moved_earlier_mid_sleep_wakes_at_new_anchor(self):
        changed_at = _beijing(2026, 8, 10, 19, 28)

        def config_at(t):
            return {
                "enabled": True,
                "interval_seconds": 86400,
                "anchor_time": "01:00" if t >= changed_at else "15:00",
            }

        # 3 轮：第 1 轮按旧锚点睡下 → 改配置后退出睡眠、第 2 轮重新规划睡到 01:00 → 第 3 轮跑。
        calls, _sleeps = self._run(
            config_at=config_at,
            last_full=_beijing(2026, 8, 10, 17, 51),
            start=_beijing(2026, 8, 10, 17, 55),
            max_runs=3,
        )
        self.assertEqual(len(calls), 1, "改成 01:00 后必须在 01:00 那轮跑，而不是睡到 15:00")
        self.assertGreaterEqual(calls[0], _beijing(2026, 8, 11, 1))
        self.assertLess(calls[0] - _beijing(2026, 8, 11, 1), timedelta(seconds=600))

    def test_interval_shortened_mid_sleep_wakes_early(self):
        """改的是间隔而不是时刻，同样要即时生效。"""
        changed_at = _beijing(2026, 8, 5, 5)

        def config_at(t):
            return {
                "enabled": True,
                "interval_seconds": 43200 if t >= changed_at else 86400,
                "anchor_time": "02:00",
            }

        calls, _sleeps = self._run(
            config_at=config_at,
            last_full=_beijing(2026, 8, 5, 2),
            start=_beijing(2026, 8, 5, 3),
            max_runs=3,
        )
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], _beijing(2026, 8, 5, 14))
        self.assertLess(calls[0] - _beijing(2026, 8, 5, 14), timedelta(seconds=600))

    def test_unchanged_config_still_sleeps_the_full_span(self):
        """热读不能把正常睡眠打断成空转——配置没动就必须睡满到锚点。"""
        calls, sleeps = self._run(
            config_at=lambda _t: {"enabled": True, "interval_seconds": 86400, "anchor_time": "02:00"},
            last_full=_beijing(2026, 8, 5, 2),
            start=_beijing(2026, 8, 5, 14),
        )
        self.assertEqual(sum(sleeps), 12 * 3600)
        self.assertEqual(len(calls), 1)

    def test_disabling_mid_sleep_does_not_wake_early(self):
        """关掉定时只是不跑，不该把 worker 提前叫醒。"""
        off_at = _beijing(2026, 8, 5, 20)

        def config_at(t):
            return {"enabled": t < off_at, "interval_seconds": 86400, "anchor_time": "02:00"}

        calls, sleeps = self._run(
            config_at=config_at,
            last_full=_beijing(2026, 8, 5, 2),
            start=_beijing(2026, 8, 5, 14),
        )
        self.assertEqual(sum(sleeps), 12 * 3600)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
