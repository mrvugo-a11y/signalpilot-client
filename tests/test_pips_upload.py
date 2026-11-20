"""1.3.19: pips never reached the website because an older SignalPilot (1.3.15 or before) kept running in the tray.
New installs could not replace it (the old app asks before quitting while copying) and opening the new version only
said "already open", because it looked for a window titled with its own version. These tests cover:
  * the new version recognises an older running copy of any version and says which one and how to close it,
  * every upload carries app_version, and an Exness style MT5 history gives non empty pip_moves and signals,
  * the upload matches what the website's /api/channel-stats parser accepts (field names, symbol pattern, outcomes)."""
import os, re, sys, time, unittest
from types import SimpleNamespace as NS
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from copier import share as SH, __version__
sys.path.insert(0, os.path.dirname(__file__))
from test_single_copier import _import_main


def _deals():
    t = int(time.time()) - 3 * 86400
    D = lambda **k: NS(**{**dict(ticket=0, order=0, position_id=0, time=t, type=0, entry=0, price=0.0, volume=0.0,
                             profit=0.0, swap=0.0, commission=0.0, fee=0.0, symbol=""), **k})
    return [D(ticket=1, type=2, profit=10000.0),                                    # balance deposit, no position
            D(ticket=11, order=101, position_id=101, price=2650.10, volume=0.02, symbol="XAUUSDm"),
            D(ticket=12, order=102, position_id=101, type=1, entry=1, price=2655.30, volume=0.02, profit=104.0,
              symbol="XAUUSDm", time=t + 3600),
            D(ticket=21, order=201, position_id=201, type=1, price=1.05500, volume=0.1, symbol="EURUSDm", time=t + 100),
            D(ticket=22, order=202, position_id=201, type=0, entry=1, price=1.05650, volume=0.1, profit=-15.0,
              symbol="EURUSDm", time=t + 7200)]


INFO = {"XAUUSDm": NS(point=0.001, digits=3), "EURUSDm": NS(point=0.00001, digits=5)}
MT5 = NS(account_info=lambda: NS(balance=10000.0), symbol_info=lambda s: INFO.get(s),
         history_deals_get=lambda *a, **k: _deals(), last_error=lambda: (1, "ok"))
STORE = {"signals": {"-1001234:55": {"sl": 2640.0, "tickets": [101]}, "-1005678:77": {"sl": 1.0570, "tickets": [201]}},
         "tickets": {"101": "-1001234", "201": "-1005678"},
         "ticket_signals": {"101": "-1001234:55", "201": "-1005678:77"}}
CFG = {"licence_key": "TEST", "licence_url": "https://site.example/api/licence"}

# The website's rules (lib/stats.php, lib/pips.php, lib/signal_pips.php), mirrored here.
SERVER_SYMBOL = re.compile(r"^[A-Za-z0-9._#\-]{1,32}$")
SERVER_OUTCOMES = {"tp", "sl", "be", "closed", "timeout"}


class AlreadyOpen(unittest.TestCase):
    def setUp(self):
        self.M = _import_main()

    def test_reads_any_version_from_window_titles(self):
        self.assertEqual(self.M.running_versions(["SignalPilot  v1.3.15", "Other", "SignalPilot  v1.3.19", None]),
                         ["1.3.15", "1.3.19"])

    def test_same_version_keeps_the_old_message(self):
        older, text = self.M.already_open_message(["1.3.19"], mine="1.3.19")
        self.assertFalse(older); self.assertIn("already open", text)

    def test_older_copy_is_named_with_how_to_close_it(self):
        older, text = self.M.already_open_message(["1.3.15"], mine="1.3.19")
        self.assertTrue(older)
        for part in ("1.3.15", "1.3.19", "Stop", "Quit", "system tray"): self.assertIn(part, text)
        self.assertNotIn("-", text.replace("1.3.", ""))          # customer copy has no dashes

    def test_no_window_found_still_says_already_open(self):
        older, text = self.M.already_open_message([], mine="1.3.19")
        self.assertFalse(older); self.assertIn("already open", text)


class Endpoint(unittest.TestCase):
    def test_render_licence_address_sends_results_to_the_site(self):
        self.assertEqual(SH.endpoint({"licence_url": "https://signalcopier-u6bo.onrender.com/api/licence"}),
                         "https://signalpilot.vodsfire.co.za/api/channel-stats")

    def test_other_hosts_unchanged(self):
        self.assertEqual(SH.endpoint({"licence_url": "https://site.example/api/licence"}), "https://site.example/api/channel-stats")


class UploadCarriesPips(unittest.TestCase):
    def body(self):
        rows, sigs = SH.collect_all(CFG, MT5, STORE, offset=0)
        return SH.payload(CFG, rows, sigs)

    def test_app_version_on_every_upload(self):
        self.assertEqual(SH.payload(CFG, [])["app_version"], __version__)
        self.assertEqual(self.body()["app_version"], __version__)

    def test_exness_history_gives_pip_moves_and_signals(self):
        b = self.body()
        moves = {c["id"]: c["pip_moves"] for c in b["channels"]}
        self.assertEqual(moves["-1001234"], {"XAUUSDm": [5.2, 1]})
        self.assertEqual(moves["-1005678"], {"EURUSDm": [-0.0015, 1]})
        pips = {s["signal_id"]: s["pips"] for s in b["signals"]}
        self.assertEqual(pips, {"-1001234:55": 52.0, "-1005678:77": -15.0})

    def test_matches_the_website_parser(self):
        b = self.body()
        self.assertIsInstance(b["channels"], list)
        for c in b["channels"]:
            self.assertRegex(c["id"], r"^-?\d{1,20}$"); self.assertIsInstance(c["trades"], int)
            self.assertIsInstance(c["pip_moves"], dict); self.assertTrue(c["pip_moves"])
            for sym, pair in c["pip_moves"].items():
                self.assertRegex(sym, SERVER_SYMBOL); self.assertEqual(len(pair), 2); self.assertIsInstance(pair[1], int)
        self.assertIsInstance(b["signals"], list)
        for s in b["signals"]:
            m = re.match(r"^(-?\d{1,20}):(\d{1,12})$", s["signal_id"])
            self.assertTrue(m); self.assertEqual(m.group(1), s["channel"])
            self.assertRegex(s["symbol"], SERVER_SYMBOL)
            self.assertIn(s["side"], ("BUY", "SELL")); self.assertIn(s["outcome"], SERVER_OUTCOMES)
            self.assertIn(s["source"], ("copied", "tracked"))
            self.assertGreater(s["entry"], 0); self.assertGreater(s["exit"], 0)
            for k in ("signal_time", "open_time", "close_time"):
                self.assertRegex(s[k], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(b["app_version"], r"^\d{1,3}(\.\d{1,3}){1,3}$")


if __name__ == "__main__":
    unittest.main()
