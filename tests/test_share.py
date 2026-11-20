"""Crowd ranking: what is sent, and nothing is sent when sharing is off."""
import io, json, os, sys, tempfile, types, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from copier import share as SH, config as C, settings_schema as S


def deal(pos, entry, t, price, typ=0, profit=0, vol=0.1, sym="EURUSD"):
    return {"position_id": pos, "order": pos, "ticket": pos * 10 + entry, "entry": entry, "time": t, "price": price,
            "type": typ, "volume": vol, "symbol": sym, "profit": profit, "swap": 0, "commission": 0, "fee": 0, "magic": 0}

INFO = lambda s: types.SimpleNamespace(point=0.00001, digits=5)
CFG = {"licence_key": "KEY1", "licence_url": "https://site.example/api/licence", "share_results": True,
       "mt5_login": 123, "mt5_password": "secret", "telegram_api_hash": "h", "telegram_phone": "+27"}


class Stats(unittest.TestCase):
    def rows(self):
        deals = [deal(1, 0, 1_790_000_000, 1.1000), deal(1, 1, 1_790_000_100, 1.1020, typ=1, profit=20),   # buy +20 pips, SL 1.0990 -> +2R
                 deal(2, 0, 1_790_100_000, 1.1000, typ=1), deal(2, 1, 1_790_200_000, 1.1010, profit=-10),   # sell -10 pips, -1R
                 deal(3, 0, 1_790_000_000, 1.2), deal(3, 1, 1_790_000_500, 1.3, typ=1, profit=50),           # other channel, no SL
                 deal(4, 0, 1_790_000_000, 1.2)]                                                              # still open
        return SH.channel_stats(deals, {1: "-1001", 2: "-1001", 3: "-1002", 4: "-1001"}, {1: 1.0990, 2: 1.1010},
                                {"-1001": "Gold VIP"}, {"-1001": "goldvip"}, 1000.0, INFO)

    def test_aggregates(self):
        a = {r["id"]: r for r in self.rows()}
        g = a["-1001"]
        self.assertEqual((g["title"], g["username"], g["trades"], g["wins"], g["losses"]), ("Gold VIP", "goldvip", 2, 1, 1))
        self.assertEqual((g["net_r"], g["r_trades"], g["net_pips"], g["profit_pct"]), (1.0, 2, 10.0, 1.0))
        self.assertTrue(g["first_trade"] <= g["last_trade"])
        self.assertEqual((a["-1002"]["title"], a["-1002"]["r_trades"]), (None, 0))

    def test_pip_moves_per_symbol(self):
        # 1.3.16: summed price move and trade count per symbol, so the server can use its own pip table
        a = {r["id"]: r for r in self.rows()}
        self.assertEqual(a["-1001"]["pip_moves"], {"EURUSD": [0.001, 2]})
        self.assertEqual(a["-1002"]["pip_moves"], {"EURUSD": [0.1, 1]})
        self.assertEqual(SH.payload(CFG, self.rows())["channels"][0]["pip_moves"], a[SH.payload(CFG, self.rows())["channels"][0]["id"]]["pip_moves"])

    def test_payload_has_no_account_details(self):
        body = json.dumps(SH.payload(CFG, self.rows()))
        for bad in ("secret", "123", "balance", "mt5", "+27", "telegram"):
            self.assertNotIn(bad, body.replace("1790", ""))
        self.assertEqual(set(SH.payload(CFG, self.rows())["channels"][0]), set(SH.FIELDS))

    def test_send_posts_to_site(self):
        got = {}
        class R:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): pass
        def opener(req, timeout): got["url"] = req.full_url; got["body"] = json.loads(req.data); return R()
        self.assertTrue(SH.send(CFG, self.rows(), opener))
        self.assertEqual(got["url"], SH.endpoint(CFG))          # the built in site when the build bakes one in
        self.assertTrue(got["url"].endswith("/api/channel-stats"))
        self.assertEqual(got["body"]["key"], "KEY1")

    def test_endpoint_from_licence_url(self):
        from unittest import mock
        with mock.patch.object(C.AC, "LICENCE_URL", ""):
            self.assertEqual(SH.endpoint(CFG), "https://site.example/api/channel-stats")
        with mock.patch.object(C.AC, "LICENCE_URL", "https://built.in/api/licence"):
            self.assertEqual(SH.endpoint(CFG), "https://built.in/api/channel-stats")

    def test_nothing_sent_when_unticked(self):
        def opener(*a, **k): raise AssertionError("must not send")
        self.assertFalse(SH.send(dict(CFG, share_results=False), self.rows(), opener))
        self.assertFalse(SH.send(dict(CFG, licence_key=""), self.rows(), opener))

    def test_send_never_raises(self):
        def opener(*a, **k): raise OSError("offline")
        self.assertFalse(SH.send(CFG, self.rows(), opener))


class Setting(unittest.TestCase):
    def test_default_on_label_and_persistence(self):
        f = [x for x in S.GENERAL_FIELDS if x[0] == "share_results"][0]
        self.assertEqual(f[1], "Share my channel results to help rank signal providers")   # 1.3.17: customer copy without "best"
        self.assertEqual(f[4], "Shares only your per channel results, never your account details or logins. Untick to stop sharing.")
        self.assertIs(C.DEFAULTS["share_results"], True)
        d = tempfile.mkdtemp(); os.environ["SIGNALPILOT_HOME"] = d
        try:
            with open(os.path.join(d, "config.json"), "w") as fh: json.dump({"licence_key": "X"}, fh)   # older config
            cfg = C.load(); self.assertIs(cfg["share_results"], True)
            cfg["share_results"] = False; C.save(cfg)
            self.assertIs(C.load()["share_results"], False)          # stays unticked after an update
        finally:
            os.environ.pop("SIGNALPILOT_HOME", None)

    def test_results_note_text(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "main.py"), encoding="utf-8").read()
        self.assertIn("Your channel results (no account details) are shared with SignalPilot to rank signal providers.", src)


class FirstUploadAndLogging(unittest.TestCase):
    def test_first_upload_two_minutes_after_start(self):
        last = SH.first_due(10000.0)
        self.assertFalse(10000.0 + 119 - last >= SH.INTERVAL)
        self.assertTrue(10000.0 + SH.FIRST_DELAY - last >= SH.INTERVAL)

    def test_send_logs_success_and_failure(self):
        class R:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
        msgs = []
        rows = [{"id": "-100123", "trades": 1}]
        self.assertTrue(SH.send(CFG, rows, lambda req, timeout: R(), log=msgs.append))
        self.assertIn("Shared results for 1 channel(s)", msgs[-1])
        def boom(req, timeout): raise OSError("no network")
        self.assertFalse(SH.send(CFG, rows, boom, log=msgs.append))
        self.assertIn("failed: no network", msgs[-1])



class IncrementalAndPips(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["SIGNALPILOT_HOME"] = self.d

    def tearDown(self):
        os.environ.pop("SIGNALPILOT_HOME", None)

    def sig(self, n=1, pips=12.0):
        return {"channel": "-1001", "signal_id": f"-1001:{n}", "msg_id": n, "symbol": "EURUSD",
                "side": "BUY", "entry": 1.1, "exit": 1.102, "pips": pips, "outcome": "closed",
                "source": "copied"}

    def test_pips_present_on_every_signal_in_payload(self):
        signals = [self.sig(1, 20.0), self.sig(2, None), self.sig(3, 0.0)]
        body = SH.payload(CFG, [{"id": "-1001", "trades": 3}], signals)
        self.assertEqual(len(body["signals"]), 3)
        for s in body["signals"]:
            self.assertIn("pips", s)
        self.assertEqual(SH.assert_signals_carry_pips(body["signals"]), [])
        self.assertEqual(SH.assert_signals_carry_pips([{"signal_id": "x"}]), [0])

    def test_send_posts_pips_on_every_uploaded_signal(self):
        got = {}
        class R:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
        def opener(req, timeout):
            got["timeout"] = timeout
            got["body"] = json.loads(req.data)
            return R()
        self.assertTrue(SH.send(CFG, [{"id": "-1001", "trades": 2}], opener, signals=[self.sig(1), self.sig(2, None)]))
        self.assertEqual(got["timeout"], 180)
        for s in got["body"]["signals"]:
            self.assertIn("pips", s)

    def test_send_timeout_is_180_and_retries_once(self):
        calls = []
        def boom(req, timeout):
            calls.append(timeout)
            raise OSError("no network")
        self.assertFalse(SH.send(CFG, [{"id": "-1", "trades": 1}], boom, signals=[self.sig()]))
        self.assertEqual(calls, [180, 180])

    def test_only_new_or_changed_signals_are_resent(self):
        got = []
        class R:
            status = 200
            def __enter__(s): return s
            def __exit__(s, *a): return False
        def opener(req, timeout):
            got.append(json.loads(req.data))
            return R()
        s1, s2 = self.sig(1), self.sig(2)
        self.assertTrue(SH.send(CFG, [{"id": "-1001", "trades": 2}], opener, signals=[s1, s2]))
        self.assertEqual({s["signal_id"] for s in got[-1]["signals"]}, {"-1001:1", "-1001:2"})
        self.assertTrue(SH.send(CFG, [{"id": "-1001", "trades": 2}], opener, signals=[s1, s2]))
        self.assertNotIn("signals", got[-1])
        s1b = self.sig(1, pips=15.0)
        self.assertTrue(SH.send(CFG, [{"id": "-1001", "trades": 2}], opener, signals=[s1b, s2]))
        self.assertEqual([s["signal_id"] for s in got[-1]["signals"]], ["-1001:1"])
        self.assertEqual(got[-1]["signals"][0]["pips"], 15.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
