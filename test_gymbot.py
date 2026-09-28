import unittest
from datetime import datetime, timedelta

import gymbot
from gymbot import SGT, check_thresholds, in_alert_hours, parse_hhmm, parse_range, short_name


def gym(gym_id, pct, closed=False):
    return {"id": gym_id, "name": f"{gym_id} ActiveSG Gym", "capacityPercentage": pct, "isClosed": closed}


class CheckThresholdsTest(unittest.TestCase):
    def test_alerts_at_or_below_threshold(self):
        to_alert, alerted = check_thresholds([gym("a", 30), gym("b", 20), gym("c", 31)], ["a", "b", "c"], 30, [])
        self.assertEqual([g["id"] for g in to_alert], ["a", "b"])
        self.assertEqual(alerted, {"a", "b"})

    def test_ignores_untracked_and_closed(self):
        to_alert, alerted = check_thresholds([gym("a", 10), gym("b", 10, closed=True)], ["b"], 30, [])
        self.assertEqual(to_alert, [])
        self.assertEqual(alerted, set())

    def test_alerts_once_until_recovered_past_hysteresis(self):
        tracked, threshold, alerted = ["a"], 30, set()
        sent = []
        # Dips, hovers near the threshold, recovers only slightly, then properly.
        for pct in [40, 29, 31, 30, 34, 28, 36, 27]:
            to_alert, alerted = check_thresholds([gym("a", pct)], tracked, threshold, alerted)
            sent += [pct for _ in to_alert]
        self.assertEqual(sent, [29, 27])

    def test_missing_gym_keeps_alert_state(self):
        _, alerted = check_thresholds([], ["a"], 30, ["a"])
        self.assertEqual(alerted, {"a"})


class HoursTest(unittest.TestCase):
    hours = {"weekday": ["18:00", "22:00"], "weekend": ["08:00", "22:00"]}

    def at(self, day, hhmm):
        # 2026-09-28 is a Monday.
        h, m = map(int, hhmm.split(":"))
        return datetime(2026, 9, 28, h, m, tzinfo=SGT) + timedelta(days=day)

    def test_weekday_window(self):
        self.assertFalse(in_alert_hours(self.at(0, "17:59"), self.hours))
        self.assertTrue(in_alert_hours(self.at(0, "18:00"), self.hours))
        self.assertTrue(in_alert_hours(self.at(4, "21:59"), self.hours))
        self.assertFalse(in_alert_hours(self.at(4, "22:00"), self.hours))

    def test_weekend_window(self):
        self.assertTrue(in_alert_hours(self.at(5, "08:00"), self.hours))  # Saturday
        self.assertTrue(in_alert_hours(self.at(6, "12:00"), self.hours))  # Sunday
        self.assertFalse(in_alert_hours(self.at(6, "07:59"), self.hours))

    def test_parsing(self):
        self.assertEqual(parse_hhmm("7"), "07:00")
        self.assertEqual(parse_hhmm("18:30"), "18:30")
        self.assertEqual(parse_range("7-22"), ["07:00", "22:00"])
        self.assertEqual(parse_range("18:00-24:00"), ["18:00", "24:00"])
        for bad in ["22-7", "7", "25-26", "7:60-8", "abc", "7:00-7:00"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_range(bad)


class ShortNameTest(unittest.TestCase):
    def test_short_names(self):
        self.assertEqual(short_name("Jurong East ActiveSG Gym"), "Jurong East")
        self.assertEqual(short_name("ActiveSG Gym @ Toa Payoh"), "Toa Payoh")
        self.assertEqual(short_name("ActiveSG Sport Park @ Teck Ghee ActiveSG Gym"), "Teck Ghee")
        self.assertEqual(short_name("Heartbeat @ Bedok ActiveSG Gym"), "Heartbeat @ Bedok")


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, **extra):
        self.sent.append(text)

    def call(self, method, **params):
        return {}


class PollTest(unittest.TestCase):
    def setUp(self):
        self.readings = []
        self.orig = (gymbot.fetch_capacities, gymbot.keep_awake, gymbot.in_alert_hours)
        gymbot.fetch_capacities = lambda: self.readings.pop(0)
        gymbot.keep_awake = lambda awake: None
        gymbot.in_alert_hours = lambda now, hours: True
        state = {k: (list(v) if isinstance(v, list) else v) for k, v in gymbot.DEFAULT_STATE.items()}
        state.update(owner_chat_id=1, tracked=["a"], threshold=30)
        self.tg = FakeTelegram()
        self.bot = gymbot.GymBot(self.tg, state)

    def tearDown(self):
        gymbot.fetch_capacities, gymbot.keep_awake, gymbot.in_alert_hours = self.orig

    def test_alert_then_quiet(self):
        self.readings = [[gym("a", 25)], [gym("a", 24)]]
        self.bot.poll()
        self.bot.poll()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("a: 25%", self.tg.sent[0])

    def test_rearms_after_long_gap(self):
        self.readings = [[gym("a", 25)], [gym("a", 25)]]
        self.bot.poll()
        self.bot.state["last_poll_at"] -= (gymbot.REARM_AFTER_GAP_MINUTES + 1) * 60
        self.bot.poll()
        self.assertEqual(len(self.tg.sent), 2)

    def test_disabled_sends_nothing(self):
        self.bot.state["enabled"] = False
        self.readings = [[gym("a", 5)]]
        self.bot.poll()
        self.assertEqual(self.tg.sent, [])

    def test_failure_alert_once_then_recovery(self):
        def failing():
            raise gymbot.FetchError("blocked")

        gymbot.fetch_capacities = failing
        for _ in range(gymbot.FAIL_ALERT_AFTER + 2):
            self.bot.poll()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("can't read", self.tg.sent[0])
        gymbot.fetch_capacities = lambda: [gym("a", 50)]
        self.bot.poll()
        self.assertIn("readable again", self.tg.sent[-1])


if __name__ == "__main__":
    unittest.main()
