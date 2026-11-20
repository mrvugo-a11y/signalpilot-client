import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from copier import updater as U


class TestUpdater(unittest.TestCase):
    def test_compare(self):
        self.assertGreater(U.vtuple("1.2.10"), U.vtuple("v1.2.9"))
        self.assertEqual(U.vtuple("v1.2.1"), (1, 2, 1))

    def test_site_then_fallback(self):
        with mock.patch.object(U, "_get_json", return_value={"version": "v9.0.0", "url": "https://x/download/SignalPilot-Setup.exe"}):
            self.assertEqual(U.newer_available({}, "1.2.1"), ("9.0.0", "https://x/download/SignalPilot-Setup.exe"))
            self.assertIsNone(U.newer_available({}, "9.0.0"))
        def fake(url):
            if "latest-version" in url: raise OSError("down")
            return {"tag_name": "v1.3.0"}
        with mock.patch.object(U, "_get_json", side_effect=fake), \
             mock.patch.object(U.C, "licence_url", return_value="https://site.example/api/licence"):
            v, url = U.newer_available({}, "1.2.1")
            self.assertEqual(v, "1.3.0"); self.assertEqual(url, "https://site.example/download/SignalPilot-Setup.exe")
        with mock.patch.object(U, "_get_json", side_effect=OSError):
            self.assertIsNone(U.newer_available({}, "1.2.1"))

    def test_no_render_fallback(self):
        self.assertFalse(hasattr(U, "DEFAULT_SITE"))
        src = Path if False else open(U.__file__, encoding="utf-8").read()
        self.assertNotIn("onrender.com", src)
        self.assertNotIn("signalcopier-u6bo", src)
        with mock.patch.object(U.C, "licence_url", return_value=""):
            self.assertEqual(U.site_base({}), "https://signalpilot.vodsfire.co.za")


class TestUpdateFlow(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.p = [mock.patch.object(U.C, "data_dir", return_value=self.d),
                  mock.patch.object(U.C, "path", side_effect=lambda n: os.path.join(self.d, n))]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()

    def test_failed_version_not_reoffered(self):
        U.write_state({"pending_version": "1.2.9"})
        r = U.pending_result("1.2.8")
        self.assertEqual(r[:2], ("failed", "1.2.9"))
        self.assertIsNone(U.pending_result("1.2.8"))          # reported once
        self.assertFalse(U.should_offer("1.2.9", "1.2.8"))
        self.assertTrue(U.should_offer("1.3.0", "1.2.8"))

    def test_success_clears(self):
        U.write_state({"pending_version": "1.2.9", "failed_version": "1.2.8"})
        self.assertIsNone(U.pending_result("1.2.9"))
        self.assertEqual(U.read_state().get("failed_version"), None)

    def test_args_and_script(self):
        a = U.installer_args(r"C:\T\SignalPilot-update.log", True)
        self.assertIn("/ALLUSERS", a); self.assertTrue(any(x.startswith("/LOG=") for x in a))
        self.assertIn("/CURRENTUSER", U.installer_args("x", False))
        s = U.helper_script(r"C:\T\it's.exe", "l", True, r"C:\P\SignalPilot.exe", 42, "1.2.9", r"C:\A\s.json")
        self.assertIn("-Verb RunAs", s); self.assertIn("it''s.exe", s); self.assertIn("ExitCode", s)
        self.assertNotIn("-Verb RunAs", U.helper_script("i", "l", False, "e", 1, "v", "s"))
