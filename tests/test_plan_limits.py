"""Per-plan channel and machine limits, including upgrade and downgrade."""
import os, sys, tempfile, time, unittest
from datetime import date, timedelta
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from copier import licence as L
from copier.engine import Engine

FUTURE = (date.today() + timedelta(days=30)).isoformat()
URL = "https://site.example/api/licence"
CHANS = [f"c{i}" for i in range(1, 21)]  # 20 distinct channels, stable order


class TestPlanLimits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SIGNALPILOT_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def limits(self, plan, max_channels=None, max_machines=None):
        return L.resolve_limits(plan, max_channels, max_machines)

    def copy(self, plan, enabled=None, max_channels=None, max_machines=None):
        lim = self.limits(plan, max_channels, max_machines)
        return L.copyable_channel_ids(enabled if enabled is not None else CHANS, lim)

    def test_trial_limits(self):
        lim = self.limits("trial")
        self.assertEqual(lim, {"plan": "trial", "max_channels": 3, "max_machines": 1, "copier": True})
        allowed, skipped, _ = self.copy("trial")
        self.assertEqual(allowed, CHANS[:3]); self.assertEqual(skipped, CHANS[3:])

    def test_free_limits(self):
        lim = self.limits("free")
        self.assertEqual(lim, {"plan": "free", "max_channels": 3, "max_machines": 1, "copier": True})
        allowed, skipped, _ = self.copy("free")
        self.assertEqual(allowed, CHANS[:3]); self.assertEqual(len(skipped), 17)

    def test_starter_limits(self):
        lim = self.limits("starter")
        self.assertEqual(lim, {"plan": "starter", "max_channels": 3, "max_machines": 1, "copier": True})
        allowed, skipped, _ = self.copy("starter")
        self.assertEqual(allowed, ["c1", "c2", "c3"])
        self.assertEqual(skipped[0], "c4"); self.assertEqual(len(allowed) + len(skipped), 20)

    def test_pro_limits(self):
        lim = self.limits("pro")
        self.assertEqual(lim, {"plan": "pro", "max_channels": 10, "max_machines": 2, "copier": True})
        allowed, skipped, _ = self.copy("pro")
        self.assertEqual(allowed, CHANS[:10]); self.assertEqual(skipped, CHANS[10:])

    def test_unlimited_no_channel_cap(self):
        lim = self.limits("unlimited")
        self.assertEqual(lim, {"plan": "unlimited", "max_channels": 0, "max_machines": 5, "copier": True})
        allowed, skipped, _ = self.copy("unlimited")
        self.assertEqual(allowed, CHANS); self.assertEqual(skipped, [])

    def test_markets_no_copier(self):
        for plan in ("markets", "markets_only", "markets_year"):
            lim = self.limits(plan)
            self.assertEqual(lim["copier"], False)
            self.assertEqual(lim["max_channels"], 0)
            self.assertEqual(lim["max_machines"], 0)
            allowed, skipped, _ = self.copy(plan)
            self.assertEqual(allowed, [])
            self.assertEqual(skipped, CHANS)
            # even if a server mistakenly reports starter-like caps, markets has no copier
            allowed2, skipped2, lim2 = self.copy(plan, max_channels=3, max_machines=1)
            self.assertFalse(lim2["copier"])
            self.assertEqual(allowed2, [])
            self.assertEqual(skipped2, CHANS)

    def test_unknown_plan_uses_starter_fallback(self):
        lim = self.limits("gold")
        self.assertEqual(lim["max_channels"], 3)
        self.assertEqual(lim["max_machines"], 1)
        self.assertTrue(lim["copier"])

    def test_server_values_override_fallback(self):
        lim = self.limits("starter", max_channels=3, max_machines=1)
        self.assertEqual(lim["max_channels"], 3)
        # a newer server can raise a known plan's cap without a client update
        lim = self.limits("pro", max_channels=12, max_machines=3)
        self.assertEqual((lim["max_channels"], lim["max_machines"]), (12, 3))
        allowed, skipped, _ = self.copy("pro", max_channels=12)
        self.assertEqual(len(allowed), 12); self.assertEqual(len(skipped), 8)

    def test_zero_from_server_is_uncapped_only_on_copier_plans(self):
        allowed, skipped, lim = self.copy("unlimited", max_channels=0, max_machines=5)
        self.assertTrue(lim["copier"]); self.assertEqual(allowed, CHANS); self.assertEqual(skipped, [])
        allowed, skipped, lim = self.copy("markets", max_channels=0, max_machines=0)
        self.assertFalse(lim["copier"]); self.assertEqual(allowed, [])

    def test_distinct_stable_order(self):
        ids = ["a", "b", "a", "c", "b", "d"]
        allowed, skipped, _ = self.copy("starter", enabled=ids)
        self.assertEqual(allowed, ["a", "b", "c"])
        self.assertEqual(skipped, ["d"])

    def test_under_the_cap_copies_all(self):
        allowed, skipped, _ = self.copy("starter", enabled=["x", "y"])
        self.assertEqual(allowed, ["x", "y"]); self.assertEqual(skipped, [])
        self.assertEqual(L.limit_message(allowed, skipped, self.limits("starter")), "")

    def test_downgrade_unlimited_to_starter(self):
        allowed, skipped, _ = self.copy("unlimited", enabled=CHANS[:8])
        self.assertEqual(allowed, CHANS[:8]); self.assertEqual(skipped, [])
        allowed, skipped, lim = self.copy("starter", enabled=CHANS[:8])
        self.assertEqual(allowed, CHANS[:3]); self.assertEqual(skipped, CHANS[3:8])
        self.assertIn("Starter", L.limit_message(allowed, skipped, lim))
        self.assertIn("3", L.limit_message(allowed, skipped, lim))

    def test_downgrade_pro_to_starter(self):
        allowed, _, _ = self.copy("pro", enabled=CHANS[:10])
        self.assertEqual(len(allowed), 10)
        allowed, skipped, _ = self.copy("starter", enabled=CHANS[:10])
        self.assertEqual(allowed, CHANS[:3]); self.assertEqual(len(skipped), 7)

    def test_upgrade_starter_to_pro(self):
        allowed, skipped, _ = self.copy("starter", enabled=CHANS[:8])
        self.assertEqual(allowed, CHANS[:3]); self.assertEqual(len(skipped), 5)
        allowed, skipped, _ = self.copy("pro", enabled=CHANS[:8])
        self.assertEqual(allowed, CHANS[:8]); self.assertEqual(skipped, [])

    def test_upgrade_starter_to_unlimited(self):
        allowed, skipped, _ = self.copy("starter", enabled=CHANS)
        self.assertEqual(len(allowed), 3)
        allowed, skipped, _ = self.copy("unlimited", enabled=CHANS)
        self.assertEqual(allowed, CHANS); self.assertEqual(skipped, [])

    def test_upgrade_pro_to_unlimited(self):
        allowed, skipped, _ = self.copy("pro", enabled=CHANS)
        self.assertEqual(len(allowed), 10)
        allowed, skipped, _ = self.copy("unlimited", enabled=CHANS)
        self.assertEqual(allowed, CHANS); self.assertEqual(skipped, [])

    def test_downgrade_unlimited_to_pro(self):
        allowed, skipped, _ = self.copy("unlimited", enabled=CHANS)
        self.assertEqual(len(allowed), 20)
        allowed, skipped, _ = self.copy("pro", enabled=CHANS)
        self.assertEqual(allowed, CHANS[:10]); self.assertEqual(skipped, CHANS[10:])

    def test_cache_stores_limits_and_plan_change_applies(self):
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        lim = L.cached_limits()
        self.assertEqual((lim["plan"], lim["max_channels"], lim["max_machines"]), ("starter", 3, 1))
        allowed, skipped, _ = L.copyable_channel_ids(CHANS)
        self.assertEqual(allowed, CHANS[:3])
        # upgrade on the next successful licence check
        L.save_cache("KEY", FUTURE, "unlimited", max_channels=0, max_machines=5)
        lim = L.cached_limits()
        self.assertEqual((lim["plan"], lim["max_channels"], lim["max_machines"], lim["copier"]),
                         ("unlimited", 0, 5, True))
        allowed, skipped, _ = L.copyable_channel_ids(CHANS)
        self.assertEqual(allowed, CHANS); self.assertEqual(skipped, [])
        # downgrade on the next successful licence check
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        allowed, skipped, _ = L.copyable_channel_ids(CHANS)
        self.assertEqual(allowed, CHANS[:3]); self.assertEqual(skipped, CHANS[3:])

    def test_tampered_limit_in_cache_is_ignored(self):
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        st = L._state()
        st["cache"]["max_channels"] = 0
        L._write(st)
        self.assertIsNone(L.load_cache())  # HMAC fails
        # without a usable cache, fallback is unknown/starter, not unlimited
        lim = L.cached_limits()
        self.assertEqual(lim["max_channels"], 3)
        self.assertTrue(lim["copier"])

    def test_fresh_start_asks_server_so_plan_change_applies(self):
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        ans = {"valid": True, "expires": FUTURE, "plan": "unlimited", "max_channels": 0, "max_machines": 5}
        with mock.patch.object(L, "_ask", return_value=ans) as m:
            ok, msg, follow = L.check_full("KEY", URL, fresh=True)
        self.assertTrue(ok); self.assertIsNone(follow); m.assert_called_once()
        lim = L.cached_limits()
        self.assertEqual(lim["plan"], "unlimited")
        self.assertEqual(lim["max_channels"], 0)
        self.assertEqual(lim["max_machines"], 5)
        allowed, skipped, _ = L.copyable_channel_ids(CHANS)
        self.assertEqual(allowed, CHANS)

    def test_fresh_start_keeps_cache_when_server_is_down(self):
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        with mock.patch.object(L, "_ask", side_effect=OSError("connection refused")):
            ok, msg, follow = L.check_full("KEY", URL, fresh=True, sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(L.cached_limits()["plan"], "starter")
        self.assertEqual(L.copyable_channel_ids(CHANS)[0], CHANS[:3])

    def test_cached_check_without_fresh_does_not_hit_the_server(self):
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        with mock.patch.object(L, "_ask", side_effect=AssertionError("should not ask")):
            ok, msg, follow = L.check_full("KEY", URL)
        self.assertTrue(ok); self.assertEqual(follow, L.REFRESH)

    def test_server_check_writes_limits_into_the_signed_cache(self):
        ans = {"valid": True, "expires": FUTURE, "plan": "pro", "max_channels": 10, "max_machines": 2}
        with mock.patch.object(L, "_ask", return_value=ans):
            status, _ = L.server_check("KEY", URL)
        self.assertEqual(status, L.OK)
        c = L.load_cache()
        self.assertEqual(c["plan"], "pro")
        self.assertEqual(c["max_channels"], 10)
        self.assertEqual(c["max_machines"], 2)

    def test_engine_channel_ids_honours_starter_and_upgrade(self):
        cfg = {"channels": {k: {"name": k, "enabled": True} for k in CHANS[:8]}, "track_followed": False}
        L.save_cache("KEY", FUTURE, "starter", max_channels=3, max_machines=1)
        eng = Engine(log=lambda m: None, ask=None)
        eng.cfg = cfg
        copied = [x for x in eng.channel_ids() if not x.startswith(("track:", "notrack:", "track_followed"))]
        self.assertEqual(copied, CHANS[:3])
        L.save_cache("KEY", FUTURE, "unlimited", max_channels=0, max_machines=5)
        copied = [x for x in eng.channel_ids() if not x.startswith(("track:", "notrack:", "track_followed"))]
        self.assertEqual(copied, CHANS[:8])

    def test_picker_captions(self):
        self.assertIn("3 Telegram channels", L.picker_caption(self.limits("starter")))
        self.assertIn("10 Telegram channels", L.picker_caption(self.limits("pro")))
        self.assertIn("no limit", L.picker_caption(self.limits("unlimited")))
        self.assertIn("does not include the trade copier", L.picker_caption(self.limits("markets")))


if __name__ == "__main__":
    unittest.main()
