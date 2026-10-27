from datetime import datetime
import unittest
from report_schedule import choose, effective_enabled


class ScheduleTests(unittest.TestCase):
    def test_disabled_config_and_emergency_pause_do_not_post(self):
        now = datetime(2026, 10, 28, 7)
        self.assertEqual(choose({"enabled": False}, "schedule", now, "0 22 * * *"), ("check", "check"))
        self.assertEqual(choose({"enabled": True}, "schedule", now, "0 22 * * *", override="false"),
                         ("check", "check"))
        self.assertFalse(effective_enabled({"enabled": False}, "true"))

    def test_daily_and_weekly_routing(self):
        for cron, hour, expected in (("0 22 * * *", 7, "morning"),
                                      ("0 10 * * *", 19, "evening"),
                                      ("30 22 * * 0", 7, "weekly")):
            self.assertEqual(choose({"enabled": True}, "schedule", datetime(2026, 10, 28, hour), cron),
                             (expected, "post"))

    def test_activation_only_runs_on_its_jst_date(self):
        config = {"enabled": True, "activation": {"date": "2026-10-28", "mode": "morning"}}
        self.assertEqual(choose(config, "push", datetime(2026, 10, 28, 8)), ("morning", "post"))
        self.assertEqual(choose(config, "push", datetime(2026, 10, 29, 8)), ("check", "check"))

    def test_invalid_inputs_and_delayed_editions_stop(self):
        with self.assertRaises(ValueError):
            choose({"enabled": True}, "schedule", datetime(2026, 10, 28, 16), "0 22 * * *")
        with self.assertRaises(ValueError):
            choose({"enabled": True}, "workflow_dispatch", datetime(2026, 10, 28), mode="untrusted")
        with self.assertRaises(ValueError):
            choose({"enabled": True}, "schedule", datetime(2026, 10, 28), "unknown")

    def test_manual_check_stays_read_only(self):
        self.assertEqual(choose({"enabled": False}, "workflow_dispatch", datetime(2026, 10, 28),
                                mode="check", action="post"), ("check", "check"))

    def test_branch_cannot_publish(self):
        with self.assertRaises(ValueError):
            choose({"enabled": True}, "workflow_dispatch", datetime(2026, 10, 28, 8),
                   mode="morning", action="post", ref="refs/heads/test")

