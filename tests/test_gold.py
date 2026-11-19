import datetime
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load

g = load("trader-gold", "gold")
D = datetime.datetime
FIVE = datetime.timedelta(minutes=5)


def bar(t, o, h, l, c):
    return (t, o, h, l, c, t)


def walk_yahoo(start=datetime.date(2026, 8, 24), end=datetime.date(2026, 10, 19), seed=7, price=2650.0):
    """A synthetic Yahoo v8 chart: 5-minute bars 06:00 to 19:00 UTC on weekdays, a seeded random walk."""
    rng = random.Random(seed)
    ts, o, h, l, c = [], [], [], [], []
    d = start
    p = price
    while d <= end:
        if d.weekday() < 5:
            t = D.combine(d, datetime.time(6, 0))
            while t < D.combine(d, datetime.time(19, 0)):
                op = p
                cl = op * (1 + rng.gauss(0, 0.0012))
                hi = max(op, cl) + abs(rng.gauss(0, 0.6))
                lo = min(op, cl) - abs(rng.gauss(0, 0.6))
                ts.append(int((t - D(1970, 1, 1)).total_seconds()))
                o.append(round(op, 2)); h.append(round(hi, 2)); l.append(round(lo, 2)); c.append(round(cl, 2))
                p = cl
                t += FIVE
        d += datetime.timedelta(days=1)
    return json.dumps({"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [
        {"open": o, "high": h, "low": l, "close": c}]}}], "error": None}})


class Time(unittest.TestCase):
    def test_london_summer_time(self):
        self.assertEqual(g.london_offset(D(2026, 3, 29, 0, 59)), 0)
        self.assertEqual(g.london_offset(D(2026, 3, 29, 1, 0)), 1)
        self.assertEqual(g.london_offset(D(2026, 10, 25, 0, 59)), 1)
        self.assertEqual(g.london_offset(D(2026, 10, 25, 1, 0)), 0)

    def test_new_york_summer_time(self):
        self.assertEqual(g.ny_offset(D(2026, 3, 8, 6, 59)), -5)
        self.assertEqual(g.ny_offset(D(2026, 3, 8, 7, 0)), -4)
        self.assertEqual(g.ny_offset(D(2026, 11, 1, 6, 0)), -5)

    def test_session_opens_in_utc(self):
        lon = g.session_cfg("london", g.DEFAULTS)
        ny = g.session_cfg("ny", g.DEFAULTS)
        self.assertEqual(g.bounds(datetime.date(2026, 10, 19), lon)[0], D(2026, 10, 19, 7, 0))
        self.assertEqual(g.bounds(datetime.date(2026, 11, 2), lon)[0], D(2026, 11, 2, 8, 0))
        self.assertEqual(g.bounds(datetime.date(2026, 10, 19), ny)[0], D(2026, 10, 19, 12, 20))
        # the week between the two changes: London is on winter time, New York still on summer time
        self.assertEqual(g.bounds(datetime.date(2026, 10, 27), ny)[0], D(2026, 10, 27, 12, 20))
        self.assertEqual(g.bounds(datetime.date(2026, 10, 27), lon)[0], D(2026, 10, 27, 8, 0))

    def test_parse_now_zones(self):
        self.assertEqual(g.parse_now("2026-10-19 08:20"), D(2026, 10, 19, 12, 20))
        self.assertEqual(g.parse_now("2026-10-19 08:15 london"), D(2026, 10, 19, 7, 15))
        self.assertEqual(g.parse_now("2026-10-19 07:15 utc"), D(2026, 10, 19, 7, 15))

    def test_gold_market_hours(self):
        self.assertFalse(g.market_open(g.parse_now("2026-10-17 12:00")))     # Saturday
        self.assertFalse(g.market_open(g.parse_now("2026-10-18 17:30")))     # Sunday before 18:00
        self.assertTrue(g.market_open(g.parse_now("2026-10-18 18:05")))
        self.assertFalse(g.market_open(g.parse_now("2026-10-20 17:30")))     # daily break
        self.assertFalse(g.market_open(g.parse_now("2026-10-23 17:00")))     # Friday close


class Data(unittest.TestCase):
    CSV = ("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>\n"
           "2026.10.19\t10:00:00\t2650.10\t2651.00\t2649.90\t2650.50\t120\t0\t25\n"
           "2026.10.19\t10:01:00\t2650.50\t2652.20\t2650.40\t2651.80\t130\t0\t25\n"
           "2026.10.19\t10:02:00\t2651.80\t2651.90\t2648.70\t2649.00\t110\t0\t25\n"
           "2026.10.19\t10:03:00\t2649.00\t2649.50\t2648.80\t2649.20\t90\t0\t25\n"
           "2026.10.19\t10:04:00\t2649.20\t2650.00\t2649.10\t2649.90\t80\t0\t25\n"
           "2026.10.19\t10:05:00\t2649.90\t2650.30\t2649.60\t2650.00\t70\t0\t25\n")

    def test_mt5_csv_server_time_and_one_minute_bars(self):
        idx = g.parse_mt5_csv(self.CSV, "ny+7")
        # server 10:00 = New York 03:00 (EDT) = 07:00 UTC = London 08:00
        b = idx[D(2026, 10, 19, 7, 0)]
        self.assertEqual(b[1:5], (2650.10, 2652.20, 2648.70, 2649.90))
        self.assertIn(D(2026, 10, 19, 7, 5), idx)
        self.assertEqual(len(idx), 2)

    def test_fixed_offsets_and_comma_files(self):
        idx = g.parse_mt5_csv("2026.10.19,10:00,1,2,0.5,1.5,10\n", "+2")
        self.assertIn(D(2026, 10, 19, 8, 0), idx)
        idx = g.parse_mt5_csv("2026-10-19 10:00;1;2;0.5;1.5\n", "0")
        self.assertIn(D(2026, 10, 19, 10, 0), idx)
        with self.assertRaises(g.GoldError):
            g.parse_mt5_csv("nada aqui\n")
        with self.assertRaises(g.GoldError):
            g.parse_server_tz("madrid")

    def test_yahoo_basis_and_live_tick(self):
        text = json.dumps({"chart": {"result": [{"timestamp": [1792393200, 1792393212], "indicators": {"quote": [
            {"open": [2670.0, 2671.0], "high": [2672.0, 2671.5], "low": [2669.0, 2670.5], "close": [2671.0, 2671.2]}]}}]}})
        idx = g.parse_yahoo(text, basis=-20)
        self.assertEqual(len(idx), 1)   # the second timestamp is a live tick, not a bar
        self.assertEqual(list(idx.values())[0][1:5], (2650.0, 2652.0, 2649.0, 2651.0))


def session_idx(day, sess, closes, rng_bars):
    """Bars for one session: the opening-range bars as given, then one bar per close."""
    o, _, _ = g.bounds(day, sess)
    idx = {}
    for i, (op, hi, lo, cl) in enumerate(rng_bars):
        t = o + i * FIVE
        idx[t] = bar(t, op, hi, lo, cl)
    p = rng_bars[-1][3]
    for j, c in enumerate(closes):
        t = o + (len(rng_bars) + j) * FIVE
        idx[t] = bar(t, p, max(p, c) + 0.2, min(p, c) - 0.2, c)
        p = c
    return idx


RANGE = [(2650.0, 2654.0, 2649.0, 2652.0), (2652.0, 2655.0, 2651.0, 2653.0), (2653.0, 2654.5, 2648.0, 2650.0)]
DAY = datetime.date(2026, 10, 19)


class Orb(unittest.TestCase):
    def setUp(self):
        self.lon = g.session_cfg("london", g.DEFAULTS)

    def test_long_trigger_on_the_first_close_above(self):
        idx = session_idx(DAY, self.lon, [2653.0, 2655.2, 2657.0], RANGE)
        doc = g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 09:00 london"))
        self.assertEqual(doc["opening_range"], {"high": 2655.0, "low": 2648.0, "minutes": 15})
        self.assertEqual((doc["status"], doc["side"], doc["entry"], doc["entry_bar"], doc["entry_time"]),
                         ("triggered", "long", 2655.2, 4, "08:25"))

    def test_short_trigger_and_side_filter(self):
        idx = session_idx(DAY, self.lon, [2650.0, 2647.5], RANGE)
        now = g.parse_now("2026-10-19 09:00 london")
        doc = g.orb(idx, self.lon, DAY, now)
        self.assertEqual((doc["status"], doc["side"], doc["entry"]), ("triggered", "short", 2647.5))
        self.assertEqual(g.orb(idx, self.lon, DAY, now, side="long")["status"], "waiting")

    def test_no_entry_limit_by_default_late_breakout_still_counts(self):
        # velas dentro del rango hasta las 11:35 y luego una ruptura a las 11:40: sin limite, cuenta
        idx = session_idx(DAY, self.lon, [2653.0] * 40 + [2660.0], RANGE)
        doc = g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 12:00 london"))
        self.assertEqual((doc["status"], doc["side"], doc["entry_time"]), ("triggered", "long", "11:40"))
        self.assertEqual(self.lon["no_entries_after"], "15:55")

    def test_a_limit_in_the_config_still_works(self):
        cfg = dict(g.DEFAULTS, sessions={"london": {"no_entries_after": "11:00"}})
        lon = g.session_cfg("london", cfg)
        idx = session_idx(DAY, lon, [2653.0] * 40 + [2660.0], RANGE)
        self.assertEqual(g.orb(idx, lon, DAY, g.parse_now("2026-10-19 12:00 london"))["status"], "expired")

    def test_a_bar_counts_only_once_closed(self):
        idx = session_idx(DAY, self.lon, [2653.0, 2656.0], RANGE)
        self.assertEqual(g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 08:24 london"))["status"], "waiting")
        self.assertEqual(g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 08:25 london"))["status"], "triggered")

    def test_states_before_and_after(self):
        idx = session_idx(DAY, self.lon, [2653.0] * 60, RANGE)
        self.assertEqual(g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 07:59 london"))["status"], "not_open")
        self.assertEqual(g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 08:10 london"))["status"], "forming")
        self.assertEqual(g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 12:00 london"))["status"], "waiting")
        self.assertEqual(g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 16:00 london"))["status"], "expired")
        sat = datetime.date(2026, 10, 17)
        self.assertEqual(g.orb(idx, self.lon, sat, g.parse_now("2026-10-17 09:00 london"))["status"], "closed")
        self.assertEqual(g.orb({}, self.lon, DAY, g.parse_now("2026-10-19 09:00 london"))["status"], "no_data")

    def test_report_is_plain_spanish(self):
        idx = session_idx(DAY, self.lon, [2653.0, 2655.2], RANGE)
        doc = g.orb(idx, self.lon, DAY, g.parse_now("2026-10-19 09:00 london"))
        text = g.orb_report(doc, "test")
        self.assertIn("ACTIVADO LARGO (compra) a las 08:25 (Londres) en 2655.20", text)
        self.assertIn("No se ha colocado nada", text)


ENTRY = {"status": "triggered", "side": "long", "entry": 2655.2, "entry_bar": 4, "entry_time": "08:25",
         "session": "london", "session_name": "Londres", "tz": "london", "date": "2026-10-19",
         "or_minutes": 15, "flat_by": "16:00", "opening_range": {"high": 2655.0, "low": 2648.0, "minutes": 15}}


class Plan(unittest.TestCase):
    def test_midpoint_stop_and_2r(self):
        p = g.make_plan(ENTRY)
        self.assertEqual((p["stop"], p["risk_per_oz"], p["target"], p["target_r"]), (2651.5, 3.7, 2662.6, 2.0))

    def test_short_rounds_the_stop_up(self):
        e = dict(ENTRY, side="short", entry=2647.33)
        p = g.make_plan(e, "or-mid", "1R")
        self.assertEqual((p["stop"], p["risk_per_oz"], p["target"]), (2651.5, 4.17, 2643.16))
        p = g.make_plan(e, "or-opposite", "1.5R")
        self.assertEqual(p["stop"], 2655.0)

    def test_refusals(self):
        with self.assertRaises(g.GoldError):
            g.make_plan(dict(ENTRY, status="waiting"))
        with self.assertRaises(g.GoldError):
            g.make_plan(ENTRY, "usd:0.5")          # under 3 x the spread
        with self.assertRaises(g.GoldError):
            g.make_plan(ENTRY, "price:2660")       # stop above a long entry
        with self.assertRaises(g.GoldError):
            g.make_plan(ENTRY, "or-mid", "price:2650")


class Size(unittest.TestCase):
    PLAN = dict(ENTRY, entry=2650.0, stop=2645.0, target=2660.0, target_r=2.0, reward_risk=2.0, risk_per_oz=5.0,
                symbol="XAUUSD")

    def test_lots_from_the_max_loss(self):
        o = g.size_order(self.PLAN, dict(g.DEFAULTS))
        # (5.00 risk + 0.30 spread) x 100 oz = 530 $ a lot; 50 / 530 = 0.094 -> 0.09
        self.assertEqual((o["volume"], o["decided_by"], o["max_loss_usd"], o["type"]), (0.09, "perdida maxima", 47.7, "buy"))
        self.assertEqual(o["margin_usd"], 238.5)

    def test_margin_can_decide(self):
        cfg = dict(g.DEFAULTS, max_loss_usd=5000, leverage=10, balance=5000)
        o = g.size_order(self.PLAN, cfg)
        self.assertEqual((o["decided_by"], o["volume"]), ("margen", 0.16))

    def test_too_small_for_the_minimum_lot(self):
        with self.assertRaises(g.GoldError) as cm:
            g.size_order(self.PLAN, dict(g.DEFAULTS, max_loss_usd=5))
        self.assertIn("lote minimo", str(cm.exception))

    def test_reward_risk_minimum(self):
        with self.assertRaises(g.GoldError):
            g.size_order(dict(self.PLAN, reward_risk=0.8), dict(g.DEFAULTS))

    def test_ticket_says_nothing_was_sent(self):
        text = g.ticket_report(g.size_order(self.PLAN, dict(g.DEFAULTS)))
        self.assertIn("nada se ha enviado", text)
        self.assertIn("Volumen      0.09 lotes", text)
        self.assertIn("Stop Loss    2645.00", text)


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx = g.parse_yahoo(walk_yahoo())

    def plan(self):
        lon = g.session_cfg("london", g.DEFAULTS)
        doc = None
        for d in sorted({g.to_local(t, "london").date() for t in self.idx}, reverse=True):
            o, cut, _ = g.bounds(d, lon)
            doc = g.orb(self.idx, lon, d, cut)
            if doc["status"] == "triggered":
                break
        doc.update(flat_by="16:00")
        return g.make_plan(doc, "or-mid", "1R")

    def test_same_seed_same_numbers(self):
        p = self.plan()
        a = g.run_replay(self.idx, p, g.DEFAULTS, replays=2000)
        b = g.run_replay(self.idx, p, g.DEFAULTS, replays=2000)
        self.assertEqual(a["mc"], b["mc"])
        mc = a["mc"]
        self.assertAlmostEqual(mc["target"] + mc["stop"] + mc["neither"], 100.0)
        self.assertGreaterEqual(a["history"]["sessions"], 20)
        self.assertLessEqual(mc["p5"], mc["median"])
        self.assertLessEqual(mc["median"], mc["p95"])
        self.assertIn("semilla 19", g.replay_report(a))

    def test_too_few_sessions(self):
        p = dict(self.plan(), date="2026-09-01")
        with self.assertRaises(g.GoldError):
            g.run_replay(self.idx, p, g.DEFAULTS, replays=100)

    def test_verdict_words(self):
        self.assertTrue(g.verdict({"target": 30, "stop": 28}, 1.0).startswith("Casi como lanzar una moneda"))
        self.assertTrue(g.verdict({"target": 20, "stop": 40}, 2.0).startswith("Cerca del punto de equilibrio"))
        self.assertTrue(g.verdict({"target": 40, "stop": 25}, 1.0).startswith("Se inclina hacia el objetivo"))
        self.assertTrue(g.verdict({"target": 10, "stop": 50}, 1.0).startswith("Claramente hacia el stop"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        with open("chart.json", "w") as fh:
            fh.write(walk_yahoo())

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = g.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_whole_flow_writes_its_files(self):
        lon = g.session_cfg("london", g.DEFAULTS)
        idx = g.parse_yahoo(walk_yahoo())
        day = None
        for d in sorted({g.to_local(t, "london").date() for t in idx}, reverse=True):
            if g.orb(idx, lon, d, g.bounds(d, lon)[1])["status"] == "triggered":
                day = d
                break
        now = "%s 11:00 london" % day.isoformat()
        code, out, err = self.run_cli("orb", "--session", "london", "--offline", "chart.json", "--now", now, "--write")
        self.assertEqual(code, 0, err)
        self.assertIn("ACTIVADO", out)
        self.assertEqual(self.run_cli("plan", "--target", "1R", "--write")[0], 0)
        code, out, err = self.run_cli("replay", "--offline", "chart.json", "--replays", "500", "--write")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("size", "--write")
        self.assertEqual(code, 0, err)
        code, out, _ = self.run_cli("ticket")
        self.assertIn("MetaTrader 5", out)
        for f in ("gold-entry.json", "gold-plan.json", "gold-replay.json", "gold-order.json"):
            self.assertTrue(os.path.isfile(os.path.join("trader", f)), f)

    def test_errors_are_one_plain_line(self):
        code, _, err = self.run_cli("plan")
        self.assertEqual(code, 2)
        self.assertIn("No se pudo", err)

    def test_clock(self):
        code, out, _ = self.run_cli("clock", "--now", "2026-10-19 09:00 london")
        self.assertEqual(code, 0)
        self.assertIn("Oro (XAUUSD): ABIERTO", out)
        self.assertIn("Londres", out)


if __name__ == "__main__":
    unittest.main()


class Tarde(unittest.TestCase):
    def test_afternoon_session_is_spain_15_30_to_20_00_in_winter(self):
        s = g.session_cfg("tarde", g.DEFAULTS)
        o, cut, flat = g.bounds(datetime.date(2026, 11, 16), s)
        self.assertEqual((o, cut, flat), (D(2026, 11, 16, 14, 30), D(2026, 11, 16, 18, 50), D(2026, 11, 16, 18, 55)))
        self.assertEqual(s["target"], "1R")


class Journal(unittest.TestCase):
    PLAN = {"date": "2026-10-19", "session": "london", "session_name": "Londres", "tz": "london", "side": "long",
            "entry": 2655.2, "entry_time": "08:25", "entry_utc": "2026-10-19T07:25:00Z", "stop": 2651.5,
            "target": 2659.0, "target_r": 1.0, "risk_per_oz": 3.8}

    def run_check(self, closes, now="2026-10-19 16:30 london", lots=0.1):
        idx = session_idx(DAY, g.session_cfg("london", g.DEFAULTS), closes, RANGE)
        doc = {"trades": []}
        order = {"sl": 2651.5, "tp": 2659.0, "volume": lots}
        g.journal_add(doc, dict(self.PLAN), order, g.DEFAULTS)
        g.journal_check(doc, idx, g.parse_now(now), g.DEFAULTS)
        return doc["trades"][0]

    def test_target(self):
        t = self.run_check([2653.0, 2655.2, 2656.0, 2659.5])
        self.assertEqual((t["status"], t["exit"], t["r"], t["exit_time"]), ("objetivo", 2659.0, 1.0, "08:35"))
        self.assertEqual(t["usd"], 35.0)   # 3.80 x 0.1 x 100 = 38, minus 0.30 x 0.1 x 100 spread

    def test_stop_first_when_a_bar_touches_both(self):
        t = self.run_check([2653.0, 2655.2, 2651.0])
        self.assertEqual((t["status"], t["r"]), ("stop", -1.0))

    def test_open_until_flat_by_then_closed_at_the_last_close(self):
        self.assertEqual(self.run_check([2653.0, 2655.2] + [2656.0] * 3, now="2026-10-19 09:00 london")["status"], "abierta")
        t = self.run_check([2653.0, 2655.2] + [2656.9] * 92)
        self.assertEqual((t["status"], t["exit"]), ("cierre", 2656.9))
        self.assertAlmostEqual(t["r"], 0.447, places=3)

    def test_no_duplicates_and_report(self):
        doc = {"trades": []}
        g.journal_add(doc, dict(self.PLAN), None, g.DEFAULTS)
        with self.assertRaises(g.GoldError):
            g.journal_add(doc, dict(self.PLAN), None, g.DEFAULTS)
        self.assertIn("abierta", g.journal_report(doc))
        with self.assertRaises(g.GoldError):
            g.journal_add({"trades": []}, dict(self.PLAN, entry_utc=None), None, g.DEFAULTS)


class Filters(unittest.TestCase):
    def ramp(self, step):
        idx = {}
        t = D(2026, 10, 1, 0, 0)
        p = 2600.0
        for _ in range(12 * 24 * 6):
            idx[t] = bar(t, p, p + 0.5, p - 0.5, p + step)
            p += step
            t += FIVE
        return idx

    def test_trend_follows_the_hourly_averages(self):
        self.assertEqual(g.Trend(self.ramp(0.05)).at(D(2026, 10, 6, 12, 0)), "long")
        self.assertEqual(g.Trend(self.ramp(-0.05)).at(D(2026, 10, 6, 12, 0)), "short")
        self.assertIsNone(g.Trend(self.ramp(0.05)).at(D(2026, 10, 1, 5, 0)))   # not enough hours yet

    def test_width_band(self):
        prior = [10.0] * 20
        self.assertEqual(g.width_ok(10, prior), (True, 10.0))
        self.assertFalse(g.width_ok(4.9, prior)[0])
        self.assertFalse(g.width_ok(20.1, prior)[0])
        self.assertTrue(g.width_ok(100, [10.0] * 5)[0])   # too little history: no filter

    def test_trade_session_both_sides(self):
        lon = g.session_cfg("london", g.DEFAULTS)
        idx = session_idx(DAY, lon, [2653.0, 2647.5, 2640.0] + [2640.0] * 90, RANGE)
        o, _, _ = g.bounds(DAY, lon)
        bars = [idx[o + i * FIVE] for i in range(g.n_slots(lon))]
        side, e, risk, out, r = g.trade_session(bars, 3, 36, "both", "or-mid", 1.0)
        self.assertEqual((side, e, out, r), ("short", 2647.5, "target", 1.0))
        self.assertIsNone(g.trade_session(bars, 3, 36, "long", "or-mid", 1.0))

    def test_backtest_and_compare_on_the_walk(self):
        idx = g.parse_yahoo(walk_yahoo())
        tarde = g.session_cfg("tarde", g.DEFAULTS)
        b = g.backtest(idx, tarde, g.DEFAULTS, 1.0)
        self.assertGreater(b["sessions"], 30)
        self.assertEqual(b["fired"], b["target"] + b["stop"] + b["neither"])
        self.assertEqual(len(b["trades"]), b["fired"])
        rows = g.compare(idx, tarde, g.DEFAULTS)
        self.assertEqual(len(rows), 12)
        self.assertIn("tendencia+anchura", g.compare_report(rows, tarde))


class Prop(unittest.TestCase):
    """Reglas de FTMO 2-Step con el diario: limite diario desde el saldo a medianoche (hora de Espana)."""

    def cfg(self, **kw):
        pr = {"initial_balance": 10000.0, "phase": 1, "start_date": "2026-11-19"}
        pr.update(kw)
        return dict(g.DEFAULTS, prop=pr)

    def trade(self, day, hh, usd, status="stop", lots=0.05, risk=10.0):
        return {"date": day, "entry_utc": "%sT%s:00Z" % (day, hh), "status": status, "usd": usd,
                "lots": lots, "risk_per_oz": risk}

    def test_no_prop_means_no_limit(self):
        self.assertIsNone(g.prop_status([], g.DEFAULTS, g.parse_now("2026-11-19 10:00 utc")))

    def test_floors_target_and_days(self):
        trades = [self.trade("2026-11-18", "13:00", -500.0),          # antes de start_date: no cuenta
                  self.trade("2026-11-19", "09:00", 100.0, "objetivo"),
                  self.trade("2026-11-20", "09:00", -50.0)]
        st = g.prop_status(trades, self.cfg(), g.parse_now("2026-11-20 12:00 utc"))
        self.assertEqual((st["balance"], st["midnight_balance"]), (10050.0, 10100.0))
        self.assertEqual((st["daily_floor"], st["total_floor"], st["target"]), (9600.0, 9000.0, 11000.0))
        self.assertEqual((st["trading_days"], st["losses_today"], st["today_pnl"]), (2, 1, -50.0))
        self.assertEqual(st["room_today"], 100.0)                     # tu freno de 150 $ menos los 50 perdidos
        self.assertEqual(st["blocks"], [])

    def test_ftmo_day_starts_at_spain_midnight(self):
        # 23:30 UTC del 19 es ya el dia 20 en Espana (UTC+1 en invierno)
        trades = [self.trade("2026-11-19", "23:30", -60.0)]
        st = g.prop_status(trades, self.cfg(), g.parse_now("2026-11-20 08:00 utc"))
        self.assertEqual((st["today"], st["today_pnl"]), ("2026-11-20", -60.0))

    def test_two_stops_block_the_day_and_size_refuses(self):
        trades = [self.trade("2026-11-19", "09:00", -50.0), self.trade("2026-11-19", "14:00", -50.0)]
        st = g.prop_status(trades, self.cfg(), g.parse_now("2026-11-19 15:00 utc"))
        self.assertEqual(len(st["blocks"]), 1)
        self.assertIn("2 stops", st["blocks"][0])

    def test_open_trade_risk_counts_and_caps_the_lots(self):
        open_t = self.trade("2026-11-19", "09:00", 0.0, "abierta", lots=0.10, risk=9.7)
        del open_t["usd"]
        st = g.prop_status([open_t], self.cfg(), g.parse_now("2026-11-19 10:00 utc"))
        self.assertEqual(st["open_risk"], 100.0)                      # 0,10 lotes x (9,70 + 0,30) x 100
        self.assertEqual(st["room_today"], 50.0)
        plan = {"entry": 3800.0, "stop": 3810.0, "target": 3790.0, "side": "short", "reward_risk": 1.0}
        o = g.size_order(plan, dict(g.DEFAULTS, max_loss_usd=100.0), room=st["room_today"])
        self.assertEqual((o["volume"], o["decided_by"]), (0.04, "limite FTMO hoy"))

    def test_target_reached_stops_risk(self):
        st = g.prop_status([self.trade("2026-11-19", "09:00", 1000.0, "objetivo")], self.cfg(),
                           g.parse_now("2026-11-20 09:00 utc"))
        self.assertIn("objetivo de la fase cumplido", st["blocks"][0])
        st2 = g.prop_status([self.trade("2026-11-19", "09:00", 520.0, "objetivo")], self.cfg(phase=2),
                            g.parse_now("2026-11-20 09:00 utc"))
        self.assertEqual(st2["target"], 10500.0)


class PropRules(unittest.TestCase):
    def cfg(self, **kw):
        pr = {"initial_balance": 10000.0, "phase": 1, "start_date": "2026-11-19", "risk_pct": 1.0, "daily_stop_usd": 200}
        pr.update(kw)
        return dict(g.DEFAULTS, prop=pr)

    def t(self, day, usd, status="stop"):
        return {"date": day, "entry_utc": day + "T09:00:00Z", "status": status, "usd": usd, "lots": 0.1, "risk_per_oz": 9.7}

    def test_one_percent_and_halved_after_five_percent_down(self):
        st = g.prop_status([], self.cfg(), g.parse_now("2026-11-19 08:00 utc"))
        self.assertEqual((st["risk_usd"], st["halved"]), (100.0, False))
        down = [self.t("2026-11-19", -250.0), self.t("2026-11-20", -250.0)]
        st = g.prop_status(down, self.cfg(), g.parse_now("2026-11-23 08:00 utc"))
        self.assertEqual((st["balance"], st["risk_usd"], st["halved"]), (9500.0, 50.0, True))

    def test_one_open_trade_at_a_time(self):
        st = g.prop_status([self.t("2026-11-19", 0.0, "abierta")], self.cfg(), g.parse_now("2026-11-19 10:00 utc"))
        self.assertIn("abierta", st["blocks"][0])

    def test_no_new_entries_late_friday(self):
        st = g.prop_status([], self.cfg(), g.parse_now("2026-11-20 18:45 utc"))   # viernes 19:45 en Espana
        self.assertIn("viernes", st["blocks"][0])
        st = g.prop_status([], self.cfg(), g.parse_now("2026-11-20 17:00 utc"))
        self.assertEqual(st["blocks"], [])

    def test_commission_counts_in_the_lots(self):
        plan = {"entry": 3800.0, "stop": 3810.0, "target": 3790.0, "side": "short", "reward_risk": 1.0}
        o = g.size_order(plan, dict(g.DEFAULTS, max_loss_usd=100.0, commission_per_lot=7.0))
        self.assertEqual(o["volume"], 0.09)          # 100 / (10,30 x 100 + 7) = 0,0964
        self.assertEqual(o["max_loss_usd"], 93.33)


class Forex(unittest.TestCase):
    def tearDown(self):
        g.apply_symbol(dict(g.DEFAULTS, _raw={}), "XAUUSD")

    def test_eurusd_pips_lots_and_commission(self):
        cfg = g.apply_symbol(dict(g.DEFAULTS, _raw={}), "EURUSD")
        self.assertEqual((g.DIG, g.PREFIX, cfg["contract_size"], cfg["leverage"]), (5, "eurusd", 100000, 100))
        self.assertEqual(g.floor_c(1.085437), 1.08543)
        entry = {"status": "triggered", "side": "short", "entry": 1.08500,
                 "opening_range": {"high": 1.08700, "low": 1.08510}}
        p = g.make_plan(entry, "or-mid", "1R", cfg)
        self.assertEqual((p["stop"], p["target"]), (1.08605, 1.08395))   # 10,5 pips
        o = g.size_order(p, dict(cfg, max_loss_usd=100.0))
        # 100 / (0,00108 x 100.000 + 5 $) = 0,88 lotes
        self.assertEqual(o["volume"], 0.88)
        self.assertIn("1.08605", g.ticket_report(o))

    def test_gold_keeps_its_own_config(self):
        cfg = g.apply_symbol(dict(g.DEFAULTS, leverage=50, spread=0.3, _raw={"leverage": 50, "spread": 0.3}), "XAUUSD")
        self.assertEqual((g.DIG, cfg["contract_size"], cfg["leverage"]), (2, 100, 50))
