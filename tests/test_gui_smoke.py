"""Opens the main window, wizard and settings window under a virtual display (skipped without Tk or a display)."""
import gc, os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import tkinter as tk; _r = tk.Tk(); _r.destroy(); HAVE_TK = True
except Exception:
    HAVE_TK = False


@unittest.skipUnless(HAVE_TK, "needs Tk and a display")
class TestGui(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.env = mock.patch.dict(os.environ, {"SIGNALPILOT_HOME": self.tmp.name}); self.env.start()
    def tearDown(self):
        # Free leftover Tk variables here, on the main thread. If the garbage collector frees them
        # later from a worker thread (e.g. the engine thread in test_session_lock), Tk can hang on Windows.
        gc.collect()
        self.env.stop(); self.tmp.cleanup()

    def test_windows_open_and_settings_save(self):
        import main
        from copier import config as C
        try:
            root = tk.Tk()
        except tk.TclError as e:      # the hosted Windows runner sometimes cannot find init.tcl for a second Tk
            self.skipTest(f"Tk not usable here: {e}")
        app = main.App(root)
        w = main.Wizard(app); self.assertEqual(w.step, "welcome"); w.next(); self.assertEqual(w.step, "licence")
        w.destroy()
        cfg = C.load(); cfg["channels"] = {"5": {"name": "Gold VIP", "enabled": True, "settings": {}}}; C.save(cfg)
        s = main.SettingsWindow(app); root.update()
        s.groups["trade"]["fixed_lot"].var.set("0.07")
        s.chrows["use_tps"].override.set(True); s.chrows["use_tps"].var.set("1,2")
        s.groups["trade"]["tp_hit_action"].var.set("Close half")
        s.save(); root.update()
        cfg = C.load()
        self.assertEqual(cfg["default_settings"]["fixed_lot"], 0.07)
        self.assertEqual(cfg["default_settings"]["tp_hit_action"], "close_half")
        self.assertEqual(cfg["channels"]["5"]["settings"], {"use_tps": [1, 2]})
        root.destroy()


if __name__ == "__main__":
    unittest.main()
