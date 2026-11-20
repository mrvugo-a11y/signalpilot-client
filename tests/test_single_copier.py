"""Bug 3: saving the channel list twice quickly started two copiers. Overlapping restarts and starts
must give exactly one running copier. Calls the real App.start / App.restart / App.stop methods."""
import os, sys, types, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _import_main():
    try:
        import tkinter  # noqa: F401  (real Tk on Windows CI)
        import main; return main
    except ImportError:
        pass
    class D:                                   # minimal stand in so main.py imports without Tk
        def __init__(s, *a, **k): pass
        def __getattr__(s, n): return D()
        def __call__(s, *a, **k): return D()
    names = ["tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.simpledialog", "tkinter.scrolledtext", "tkinter.filedialog"]
    saved = {n: sys.modules.get(n) for n in names}
    try:
        for n in names:
            m = types.ModuleType(n); m.__getattr__ = lambda a: D; sys.modules[n] = m
        t = sys.modules["tkinter"]; t.TclError = Exception
        for n in names[1:]: setattr(t, n.split(".")[1], sys.modules[n])
        import main; return main
    finally:                                   # do not leak the stand in into other tests
        for n, v in saved.items():
            if v is None: sys.modules.pop(n, None)
            else: sys.modules[n] = v


main = _import_main()


class FakeEngine:
    made = []
    def __init__(self, *a):
        self.alive = False; FakeEngine.made.append(self)
    def start(self): self.alive = True
    def stop(self): self.stopping = True
    def running(self): return self.alive


class FakeApp:
    """Only the attributes App.start / restart / stop touch; timers and the licence check are queued."""
    def __init__(self):
        self.engine = None; self._starting = False; self._restart_pending = False
        self.btn_start = {}; self.btn_stop = {}; self.logs = []; self.jobs = []
        self.root = types.SimpleNamespace(after=lambda ms, fn: self.jobs.append(fn))
    def log(self, m): self.logs.append(m)
    def ask(self, *a): return None
    def set_status(self, *a): pass
    def set_running(self, on): pass
    def check_licence_async(self, then=None, fresh=False): self.jobs.append(lambda: then(True, "ok"))
    start = main.App.start; stop = main.App.stop; restart = main.App.restart

    def pump(self, n=50):
        for _ in range(n):
            for e in FakeEngine.made:                       # a stopped copier finishes after a moment
                if getattr(e, "stopping", False): e.alive = False
            if not self.jobs: return
            jobs, self.jobs = self.jobs, []
            for j in jobs: j()


class SingleCopier(unittest.TestCase):
    def setUp(self):
        FakeEngine.made = []
        self.p = [mock.patch("copier.engine.Engine", FakeEngine), mock.patch.object(main.C, "load", lambda: {}),
                  mock.patch.object(main.W, "needs_setup", lambda cfg: False)]
        for p in self.p: p.start()
        self.app = FakeApp()
        self.app.start(); self.app.pump()
        self.assertEqual(len(FakeEngine.made), 1)
        self.first = FakeEngine.made[0]

    def tearDown(self):
        for p in self.p: p.stop()

    def running(self): return [e for e in FakeEngine.made if e.running()]

    def test_two_rapid_restarts_give_one_copier(self):
        self.app.restart(); self.app.restart()           # channel list saved twice quickly
        self.app.pump()
        self.assertEqual(len(FakeEngine.made), 2)        # the old one plus exactly one new one
        self.assertEqual(len(self.running()), 1)
        self.assertFalse(self.first.running())

    def test_restart_while_start_in_progress(self):
        self.app.restart(); self.app.pump(1); self.app.pump(1)   # old copier stopped, licence check pending
        self.app.restart(); self.app.pump()
        self.assertEqual(len(self.running()), 1)

    def test_double_start_click(self):
        self.app.stop(); self.app.pump()
        self.app.start(); self.app.start(); self.app.pump()
        self.assertEqual(len(self.running()), 1)

    def test_stop_cancels_pending_restart(self):
        self.app.restart(); self.app.stop(); self.app.pump()
        self.assertEqual(self.running(), [])


if __name__ == "__main__":
    unittest.main()
