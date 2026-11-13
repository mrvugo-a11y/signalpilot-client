"""1.3.17: pips for followed channels that are not copied, tracked on paper with simulated MT5 prices.
No test (and no code path in the tracker) ever sends an order."""
import json, os, random, sys, tempfile, threading, types, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SIGNALPILOT_HOME", tempfile.mkdtemp())
from copier import tracker as TR, pips as P, share as SH, results as R
from copier.parser import parse, Signal, Command

T0 = 1_790_000_000           # server time of the signal


def bar(t, o, h, l, c, spread=0):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "spread": spread}


class FakeMarket:
    """Read only market made of simulated one minute bars. order_send does not exist: any attempt fails loudly."""
    def __init__(self, sym="XAUUSDm", bid=2000.0, ask=2000.3, t=T0, point=0.01):
        self.sym, self.point_, self.bars_ = sym, point, []
        self.tick_ = types.SimpleNamespace(bid=bid, ask=ask, time=t)
    def resolve(self, s, st): return self.sym if s in ("XAUUSD", "GOLD", self.sym) else None
    def tick(self, s): return self.tick_
    def point(self, s): return self.point_
    def bars(self, s, n): return list(self.bars_)


def sig(side="BUY", sl=1990.0, tps=(2005.0, 2010.0, 2020.0), entry=None, kind="MARKET", hi=None):
    return Signal(side=side, symbol="XAUUSD", order_type=kind, entry=entry, entry_high=hi, sl=sl, tps=list(tps))


class PipTable(unittest.TestCase):
    def test_same_as_site(self):
        cases = {"XAUUSDm": 0.1, "XAGUSDm": 0.01, "USOILm": 0.01, "UKOILm": 0.01, "US30m": 1.0, "DE30m": 1.0, "JP225m": 1.0,
                 "US500m": 0.1, "BTCUSDm": 1.0, "ETHUSDm": 0.1, "USDJPYm": 0.01, "EURUSDm": 0.0001, "GBPUSD.r": 0.0001,
                 "#XAUUSD": 0.1, "NAS100": 1.0, "SPX500": 0.1, "TSLAm": None, "ETHBTCm": None}
        for s, size in cases.items():
            self.assertEqual(P.pip_size(s), size, s)

    def test_pips_sign(self):
        self.assertEqual(P.pips("XAUUSDm", "BUY", 2000.0, 2005.0), 50.0)
        self.assertEqual(P.pips("XAUUSDm", "SELL", 2000.0, 2005.0), -50.0)
        self.assertEqual(P.pips("EURUSDm", 1, 1.1000, 1.1020), 20.0)


class Advance(unittest.TestCase):
    def tracker(self):
        return TR.Tracker(os.path.join(tempfile.mkdtemp(), "tracked.json"))

    def test_market_buy_tp1_then_tp2(self):
        m = FakeMarket(); tr = self.tracker()
        r = tr.add(m, -100, 11, sig(), now=T0)
        self.assertEqual((r["state"], r["fill"]), ("open", 2000.3))       # buy at the ask
        m.bars_ = [bar(T0 + 60, 2000, 2003, 1999, 2002), bar(T0 + 120, 2002, 2006, 2001, 2005),
                   bar(T0 + 180, 2005, 2011, 2004, 2010), bar(T0 + 240, 2010, 2010, 1989, 1990)]
        tr.update(m, now=T0 + 300)
        r = tr.get(-100, 11)
        self.assertEqual((r["outcome"], r["exit"], r["pips"]), ("tp", 2005.0, 47.0))   # result fixed at TP1
        self.assertEqual(r["tp_pips"], {"1": 47.0, "2": 97.0})                       # TP2 recorded too, TP3 never
        self.assertEqual(r["state"], "done")                                          # stop loss ended the watch

    def test_stop_loss_and_same_bar_is_counted_as_loss(self):
        m = FakeMarket(); tr = self.tracker()
        tr.add(m, -100, 12, sig(), now=T0)
        m.bars_ = [bar(T0 + 60, 2000, 2006, 1989, 1995)]                 # touches TP1 and SL in one bar
        tr.update(m, now=T0 + 120)
        r = tr.get(-100, 12)
        self.assertEqual((r["outcome"], r["pips"]), ("sl", -103.0))

    def test_sell_uses_ask_with_spread(self):
        m = FakeMarket(); tr = self.tracker()
        tr.add(m, -100, 13, sig("SELL", sl=2010.0, tps=(1995.0,)), now=T0)
        m.bars_ = [bar(T0 + 60, 2000, 2000, 1994.9, 1995, spread=30)]    # bid 1994.9 but ask 1995.2: not hit
        tr.update(m, now=T0 + 90)
        self.assertEqual(tr.get(-100, 13)["state"], "open")
        m.bars_.append(bar(T0 + 120, 1995, 1995, 1994.6, 1994.7, spread=30))  # ask 1994.9: hit
        tr.update(m, now=T0 + 150)
        r = tr.get(-100, 13)
        self.assertEqual((r["outcome"], r["fill"], r["pips"]), ("tp", 2000.0, 50.0))

    def test_limit_fills_then_expires_if_not(self):
        m = FakeMarket(); tr = self.tracker()
        r = tr.add(m, -100, 14, sig(entry=1995.0, kind="LIMIT", sl=1985.0), now=T0)
        self.assertEqual((r["state"], r["pending_price"]), ("pending", 1995.0))
        m.bars_ = [bar(T0 + 60, 2000, 2000, 1994.5, 1996), bar(T0 + 120, 1996, 2006, 1996, 2005)]
        tr.update(m, now=T0 + 180)
        r = tr.get(-100, 14)
        self.assertEqual((r["fill"], r["outcome"], r["pips"]), (1995.0, "tp", 100.0))
        r2 = tr.add(m, -100, 15, sig(entry=1950.0, kind="LIMIT", sl=1940.0), now=T0)
        m.bars_ = [bar(T0 + 25 * 3600, 2000, 2001, 1999, 2000)]
        tr.update(m, now=T0 + 25 * 3600)
        self.assertEqual(tr.get(-100, 15)["outcome"], "expired")
        self.assertNotIn(tr.get(-100, 15), tr.counted())

    def test_timeout_closes_at_market(self):
        m = FakeMarket(); tr = self.tracker()
        tr.add(m, -100, 16, sig(), now=T0)
        m.bars_ = [bar(T0 + 7 * 86400, 2001, 2002, 2000, 2001.3)]
        tr.update(m, now=T0 + 7 * 86400 + 60)
        r = tr.get(-100, 16)
        self.assertEqual((r["outcome"], r["exit"], r["pips"]), ("timeout", 2001.3, 10.0))

    def test_invalid_and_unknown_symbol_not_counted(self):
        m = FakeMarket(); tr = self.tracker()
        self.assertEqual(tr.add(m, -100, 17, sig(sl=2000.1), now=T0)["outcome"], "invalid")       # SL above the bid
        self.assertEqual(tr.add(m, -100, 18, sig(tps=(1999.0,)), now=T0)["outcome"], "invalid")   # every TP passed
        s = Signal(side="BUY", symbol="TSLA", sl=1, tps=[3]); self.assertEqual(tr.add(m, -100, 19, s, now=T0)["outcome"], "no_symbol")
        self.assertEqual(tr.counted(), [])

    def test_channel_commands(self):
        m = FakeMarket(); tr = self.tracker()
        tr.add(m, -100, 20, sig(), now=T0)
        tr.apply(m, -100, 20, Command("MOVE_SL_BE"), now=T0 + 30)
        self.assertEqual(tr.get(-100, 20)["sl"], 2000.3)
        m.bars_ = [bar(T0 + 60, 2000, 2001, 2000.2, 2000.5)]
        tr.update(m, now=T0 + 90)
        self.assertEqual(tr.get(-100, 20)["outcome"], "be")
        tr.add(m, -100, 21, sig(), now=T0)
        m.tick_ = types.SimpleNamespace(bid=2003.0, ask=2003.3, time=T0 + 600); m.bars_ = []
        tr.apply(m, -100, 21, Command("CLOSE_ALL"), now=T0 + 600)
        r = tr.get(-100, 21)
        self.assertEqual((r["outcome"], r["exit"], r["pips"]), ("closed", 2003.0, 27.0))
        tr.add(m, -100, 22, sig(entry=1990.0, kind="LIMIT", sl=1980.0), now=T0)
        tr.apply(m, -100, 22, Command("CANCEL"), now=T0 + 700)
        self.assertEqual(tr.get(-100, 22)["outcome"], "cancelled")
        tr.add(m, -100, 23, sig(), now=T0)
        tr.apply(m, -100, 23, Command("MODIFY", sl=1995.0, tps=[2002.0]), now=T0 + 700)
        self.assertEqual((tr.get(-100, 23)["sl"], tr.get(-100, 23)["tps"]), (1995.0, [2002.0]))

    def test_survives_restart_and_catches_up(self):
        m = FakeMarket(); path = os.path.join(tempfile.mkdtemp(), "tracked.json")
        TR.Tracker(path).add(m, -100, 30, sig(), now=T0)
        tr = TR.Tracker(path)                                   # app restarted, computer was off for two hours
        self.assertEqual(tr.get(-100, 30)["state"], "open")
        m.bars_ = [bar(T0 + 60 * i, 2000, 2000.5, 1999.5, 2000) for i in range(1, 100)] + [bar(T0 + 6000, 2000, 2005.5, 2000, 2005)]
        tr.update(m, now=T0 + 7200)
        self.assertEqual(TR.Tracker(path).get(-100, 30)["outcome"], "tp")

    def test_upload_rows_and_mark(self):
        m = FakeMarket(); tr = self.tracker()
        tr.add(m, -100, 40, sig(), now=T0)
        m.bars_ = [bar(T0 + 60, 2000, 2006, 2000, 2005)]
        tr.update(m, now=T0 + 120)
        rows = tr.upload_rows()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["channel"], r["signal_id"], r["source"], r["outcome"], r["pips"], r["side"]),
                         ("-100", "-100:40", "tracked", "tp", 47.0, "BUY"))
        for k in ("symbol", "entry", "exit", "signal_time", "open_time", "close_time", "msg_id"): self.assertIn(k, r)


class SimulatedPrices(unittest.TestCase):
    """Random walk prices over three days, 40 random signals: every tracker ends with a sane outcome and the
    pips always equal the move from fill to exit with gold's 0.1 pip."""
    def test_random_walk(self):
        rnd = random.Random(7); px = 2000.0; bars = []
        for i in range(1, 3 * 1440):
            o = px; px += rnd.gauss(0, 0.6); h = max(o, px) + abs(rnd.gauss(0, 0.3)); l = min(o, px) - abs(rnd.gauss(0, 0.3))
            bars.append(bar(T0 + 60 * i, o, h, l, px, spread=rnd.choice((15, 20, 30))))
        tr = TR.Tracker(os.path.join(tempfile.mkdtemp(), "tracked.json"))
        for n in range(40):
            k = rnd.randrange(0, len(bars) - 1500); b = bars[k]
            m = FakeMarket(bid=b["close"], ask=round(b["close"] + 0.2, 2), t=b["time"])
            side = rnd.choice(("BUY", "SELL")); d = 1 if side == "BUY" else -1; e = b["close"]
            tr.add(m, -100, 100 + n, sig(side, sl=e - d * 8, tps=(e + d * 5, e + d * 10, e + d * 15)), now=b["time"])
        m.bars_ = bars; tr.update(m, now=T0 + 3 * 86400)
        for r in tr.data["signals"].values():
            self.assertIn(r["outcome"], ("tp", "sl", "be", "invalid", "timeout", None))
            if r["outcome"] in ("tp", "sl"):
                self.assertAlmostEqual(r["pips"], round((1 if r["side"] == "BUY" else -1) * (r["exit"] - r["fill"]) / 0.1, 1), 6)
        self.assertGreater(len(tr.counted()), 30)


def deal(pos, entry, t, price, typ=0, vol=0.1, sym="XAUUSDm"):
    return {"position_id": pos, "order": pos, "ticket": pos * 10 + entry, "entry": entry, "time": t, "price": price,
            "type": typ, "volume": vol, "symbol": sym, "profit": 0, "swap": 0, "commission": 0, "fee": 0, "magic": 0}


class CopiedSignals(unittest.TestCase):
    def test_split_trades_count_once(self):
        # one gold buy split in three trades (TP1, TP2, TP3) plus one still running signal
        deals = [deal(1, 0, T0, 2000.0), deal(1, 1, T0 + 60, 2005.0, 1), deal(2, 0, T0, 2000.0), deal(2, 1, T0 + 120, 2010.0, 1),
                 deal(3, 0, T0, 2000.0), deal(3, 1, T0 + 180, 1990.0, 1), deal(4, 0, T0, 2000.0)]
        out = SH.copied_signals(deals, {1: "-100:5", 2: "-100:5", 3: "-100:5", 4: "-100:6"})
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["signal_id"], out[0]["source"], out[0]["pips"], out[0]["entry"]), ("-100:5", "copied", 16.7, 2000.0))

    def test_payload_backwards_compatible(self):
        cfg = {"licence_key": "K", "licence_url": "https://x.example/api/licence", "share_results": True}
        p = SH.payload(cfg, [], [{"signal_id": "-1:2"}])
        self.assertEqual(p["channels"], []); self.assertEqual(p["signals"], [{"signal_id": "-1:2"}]); self.assertIn("app_version", p)
        self.assertNotIn("signals", SH.payload(cfg, []))      # unchanged body when there is nothing per signal

    def test_results_window_rows(self):
        rows = [{"channel": "-100", "name": "Gold", "net": 5.0, "trades": 3, "wins": 2, "win_rate": 66.7, "profit": 9, "loss": -4}]
        R.add_pips(rows, [{"channel": "-100", "pips": 16.7}], [{"channel": "-200", "name": "Other", "trades": 2, "wins": 1,
                                                               "win_rate": 50.0, "pips": 12.0}])
        self.assertEqual((rows[0]["source"], rows[0]["pips"]), ("copied", 16.7))
        self.assertEqual((rows[1]["source"], rows[1]["pips"], rows[1]["net"]), ("tracked", 12.0, None))


class EngineRouting(unittest.TestCase):
    """A tracked channel's signal goes to the paper tracker. The executor (which sends orders) is never called."""
    def test_tracked_signal_never_opens(self):
        from copier.engine import Engine
        from copier.store import Store
        d = tempfile.mkdtemp(); m = FakeMarket(); logs = []
        def boom(*a, **k): raise AssertionError("an order was sent for a tracked channel")
        eng = Engine.__new__(Engine)
        eng.log = logs.append; eng.mt5_lock = threading.Lock(); eng.cfg = {"channels": {}, "default_settings": {}}
        eng.store = Store(os.path.join(d, "t.json")); eng.tracker = TR.Tracker(os.path.join(d, "tr.json"))
        eng.tracked = {"-100": "Gold Room"}
        eng.ex = types.SimpleNamespace(open_signal=boom, apply=boom, resolve_symbol=m.resolve)
        eng._market = lambda: m
        eng._open(-100, 50, sig(), {"wait_for_sl_minutes": 15})
        self.assertEqual(eng.tracker.get(-100, 50)["state"], "open")
        self.assertEqual(eng.store.data["signals"], {})
        eng._apply(-100, 50, Command("MOVE_SL", value=1999.0), {})
        self.assertEqual(eng.tracker.get(-100, 50)["sl"], 1999.0)
        self.assertTrue(any("no trade is opened" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
