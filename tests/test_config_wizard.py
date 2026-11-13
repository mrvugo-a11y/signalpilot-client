"""Config load/save, settings form conversions and setup wizard logic (headless, no Tk/Telegram/MT5)."""
import json, os, sys, tempfile, types, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from copier import config as C, setup_logic as W, settings_schema as S, app_credentials as AC


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.env = mock.patch.dict(os.environ, {"SIGNALPILOT_HOME": self.tmp.name})
        self.env.start()
    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()


class TestConfig(Home):
    def test_first_load_creates_defaults_in_data_dir(self):
        with mock.patch.object(C, "app_dir", return_value=self.tmp.name + "/nothing"):
            cfg = C.load()
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "config.json")))
        self.assertEqual(cfg["default_settings"]["fixed_lot"], 0.01)
        self.assertFalse(cfg["setup_done"])

    def test_save_roundtrip_and_missing_keys_filled(self):
        C.save({"licence_key": "ABC", "default_settings": {"fixed_lot": 0.05}})
        cfg = C.load()
        self.assertEqual(cfg["licence_key"], "ABC")
        self.assertEqual(cfg["default_settings"]["fixed_lot"], 0.05)
        self.assertEqual(cfg["default_settings"]["tp_hit_action"], "none")
        cfg["channels"] = {"-100": {"name": "X", "enabled": True, "settings": {}}}; C.save(cfg)
        self.assertEqual(C.load()["channels"]["-100"]["name"], "X")

    def test_symbol_map_replaced_not_merged(self):
        C.save({"default_settings": {"symbol_map": {"US30": "DJ30"}}})
        self.assertEqual(C.load()["default_settings"]["symbol_map"], {"US30": "DJ30"})

    def test_legacy_config_next_to_exe_is_imported(self):
        legacy = os.path.join(self.tmp.name, "exe"); os.makedirs(legacy)
        json.dump({"licence_key": "BAKED", "licence_url": "https://x/api/licence"}, open(os.path.join(legacy, "config.json"), "w"))
        with mock.patch.object(C, "app_dir", return_value=legacy):
            self.assertEqual(C.load()["licence_key"], "BAKED")

    def test_corrupt_file_falls_back_to_defaults(self):
        open(C.config_path(), "w").write("{oops")
        self.assertEqual(C.load()["default_settings"]["magic_number"], 777001)

    def test_channel_override_and_disabled(self):
        cfg = C.load(); cfg["channels"] = {"1": {"name": "A", "enabled": False, "settings": {"fixed_lot": 0.2, "symbol_map": {"NAS": "US100"}}}}
        s = C.channel_settings(cfg, 1)
        self.assertEqual(s["fixed_lot"], 0.2); self.assertFalse(s["enabled"])
        self.assertEqual(s["symbol_map"], {"GOLD": "XAUUSD", "NAS": "US100"})

    def test_api_credentials_builtin_first_then_customer(self):
        with mock.patch.object(AC, "TELEGRAM_API_ID", 0), mock.patch.object(AC, "TELEGRAM_API_HASH", ""):
            self.assertTrue(C.needs_api_credentials({}))
            self.assertEqual(C.api_credentials({"telegram_api_id": "5", "telegram_api_hash": "h"}), (5, "h"))
        with mock.patch.object(AC, "TELEGRAM_API_ID", 9), mock.patch.object(AC, "TELEGRAM_API_HASH", "b"):
            self.assertEqual(C.api_credentials({"telegram_api_id": 5, "telegram_api_hash": "h"}), (9, "b"))

    def test_licence_url_builtin_wins(self):
        with mock.patch.object(AC, "LICENCE_URL", "https://site/api/licence"):
            self.assertEqual(C.licence_url({"licence_url": "old"}), "https://site/api/licence")
        with mock.patch.object(AC, "LICENCE_URL", ""):
            self.assertEqual(C.licence_url({"licence_url": "old"}), "old")


class TestSchema(unittest.TestCase):
    def test_every_default_setting_has_a_field(self):
        keys = {f[0] for f in S.TRADE_FIELDS}
        self.assertEqual(keys, set(C.DEFAULT_SETTINGS))
        top = {f[0] for f in S.GENERAL_FIELDS + S.MT5_FIELDS + S.API_FIELDS}
        self.assertTrue(set(C.DEFAULTS) - {"channels", "default_settings", "setup_done", "licence_url"} <= top)

    def test_no_dashes_in_ui_copy(self):
        for t in S.all_ui_text():
            for d in "-\u2013\u2014":
                self.assertNotIn(d, t, t)

    def test_conversions(self):
        self.assertEqual(S.from_text("tps", "3, 1,2"), [1, 2, 3])
        self.assertEqual(S.to_text("tps", [1, 2]), "1,2")
        self.assertEqual(S.from_text("float", "0,05"), 0.05)
        self.assertEqual(S.from_text("symbolmap", "gold=XAUUSD, US30 = DJ30"), {"GOLD": "XAUUSD", "US30": "DJ30"})
        self.assertEqual(S.from_text("symbolmap", S.to_text("symbolmap", {"A": "B"})), {"A": "B"})
        for kind, bad in (("int", "abc"), ("tps", "x"), ("tps", "0"), ("symbolmap", "GOLD")):
            with self.assertRaises(S.FieldError): S.from_text(kind, bad)
        opts = dict((f[0], f[3]) for f in S.TRADE_FIELDS)["tp_hit_action"]
        self.assertEqual(S.choice_value(opts, S.choice_label(opts, "close_half")), "close_half")

    def test_validate(self):
        self.assertEqual(S.validate_trade_settings(dict(C.DEFAULT_SETTINGS)), [])
        self.assertTrue(S.validate_trade_settings(dict(C.DEFAULT_SETTINGS, fixed_lot=0)))
        self.assertTrue(S.validate_trade_settings(dict(C.DEFAULT_SETTINGS, lot_mode="risk", risk_percent=150)))


class TestWizard(Home):
    def test_steps_skip_api_when_builtin(self):
        with mock.patch.object(AC, "TELEGRAM_API_ID", 1), mock.patch.object(AC, "TELEGRAM_API_HASH", "x"):
            self.assertNotIn("api", W.wizard_steps({}))
        with mock.patch.object(AC, "TELEGRAM_API_ID", 0), mock.patch.object(AC, "TELEGRAM_API_HASH", ""):
            self.assertEqual(W.wizard_steps({}), ["welcome", "licence", "api", "telegram", "channels", "mt5", "done"])

    def test_needs_setup(self):
        with mock.patch.object(AC, "TELEGRAM_API_ID", 1), mock.patch.object(AC, "TELEGRAM_API_HASH", "x"):
            self.assertTrue(W.needs_setup({"setup_done": False}))
            self.assertTrue(W.needs_setup({"setup_done": True, "channels": {}}))
            self.assertFalse(W.needs_setup({"setup_done": True, "channels": {"1": {}}}))

    def test_phone(self):
        self.assertEqual(W.normalise_phone("+27 82 123-4567"), ("+27821234567", None))
        self.assertEqual(W.normalise_phone("0027821234567")[0], "+27821234567")
        self.assertIsNone(W.normalise_phone("0821234567")[0])
        self.assertIsNone(W.normalise_phone("+27abc")[0])

    def test_api_check(self):
        self.assertIsNone(W.check_api_credentials("123", "a" * 32)[2])
        self.assertIsNotNone(W.check_api_credentials("x", "a" * 32)[2])
        self.assertIsNotNone(W.check_api_credentials("123", "short")[2])

    def test_licence_key(self):
        seen = []
        chk = lambda k, u: (seen.append((k, u)) or (True, "Licence valid"))
        with mock.patch.object(AC, "LICENCE_URL", "https://s/api/licence"):
            self.assertFalse(W.check_licence_key({}, "  ", chk)[0])
            self.assertTrue(W.check_licence_key({}, " KEY ", chk)[0])
        self.assertEqual(seen, [("KEY", "https://s/api/licence")])

    def test_trial_licence_runs_without_key(self):
        from copier import licence
        with mock.patch.object(AC, "LICENCE_URL", ""):
            ok, msg = W.check_licence_key({}, "", licence.check)
        self.assertTrue(ok); self.assertIn("Trial", msg)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "licence.dat")))

    def test_channel_selection_keeps_overrides(self):
        cfg = {"channels": {"1": {"name": "Old", "enabled": True, "settings": {"fixed_lot": 0.3}}}}
        W.apply_channel_selection(cfg, [(1, "Gold VIP"), (2, "Forex"), (3, "Chat")], [1, 2])
        self.assertEqual(cfg["channels"]["1"], {"name": "Gold VIP", "enabled": True, "settings": {"fixed_lot": 0.3}})
        self.assertEqual(set(cfg["channels"]), {"1", "2"})

    def _mt5(self, ok=True, account=True, algo=True):
        m = types.SimpleNamespace(shutdown=lambda: None)
        m.initialize = lambda **kw: (m.__dict__.setdefault("kw", kw), ok)[1]
        m.account_info = lambda: types.SimpleNamespace(login=123, server="Broker-Demo", company="Broker Ltd", name="Ann",
                                                       balance=1000.0, currency="USD") if account else None
        m.terminal_info = lambda: types.SimpleNamespace(trade_allowed=algo, path="C:\\MT5")
        return m

    def test_detect_mt5(self):
        r = W.detect_mt5({}, self._mt5())
        self.assertTrue(r["ok"]); self.assertEqual((r["login"], r["broker"]), (123, "Broker Ltd")); self.assertIn("Broker Ltd", r["message"])
        self.assertIn("Algo Trading is OFF", W.detect_mt5({}, self._mt5(algo=False))["message"])
        self.assertFalse(W.detect_mt5({}, self._mt5(ok=False))["ok"])
        self.assertFalse(W.detect_mt5({}, self._mt5(account=False))["ok"])
        m = self._mt5(); W.detect_mt5({"mt5_path": "D:\\t.exe"}, m); self.assertEqual(m.kw, {"path": "D:\\t.exe"})

    def test_finish(self):
        cfg = W.finish({"mt5_path": ""}, {"ok": True, "path": self.tmp.name})
        self.assertTrue(cfg["setup_done"]); self.assertEqual(cfg["mt5_path"], "")
        open(os.path.join(self.tmp.name, "terminal64.exe"), "w").close()
        self.assertTrue(W.finish({}, {"ok": True, "path": self.tmp.name})["mt5_path"].endswith("terminal64.exe"))


class TestEngine(Home):
    def test_reload_settings_reaches_executor(self):
        from copier.engine import Engine
        e = Engine(lambda m: None, None); e.ex = types.SimpleNamespace(cfg=None)
        cfg = {"channels": {"2": {"enabled": True}, "1": {"enabled": False}}}
        e.reload_settings(cfg)
        self.assertIs(e.ex.cfg, cfg); self.assertEqual(e.channel_ids(), ["2", "track:1", "track_followed"])   # 1.3.17: unticked channel 1 is tracked

    def test_make_client_without_credentials_is_friendly(self):
        from copier import engine
        with mock.patch.object(AC, "TELEGRAM_API_ID", 0), mock.patch.object(AC, "TELEGRAM_API_HASH", ""):
            with self.assertRaises(RuntimeError): engine.make_client({})


if __name__ == "__main__":
    unittest.main()
