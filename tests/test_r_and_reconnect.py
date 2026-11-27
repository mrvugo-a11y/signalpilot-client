"""1.3.20: results in R and the lost MetaTrader 5 link that looked like a missing symbol."""
import types, unittest
from copier import pips as P, tracker as T, share as SH
from copier import executor as X


class RMultiple(unittest.TestCase):
    def test_values(self):
        self.assertEqual(P.r_multiple("BUY", 100, 104, 98), 2.0)
        self.assertEqual(P.r_multiple("SELL", 100, 101, 101), -1.0)
        self.assertEqual(P.r_multiple(-1, 100, 97, 101), 3.0)
        self.assertIsNone(P.r_multiple("BUY", 100, 104, None))
        self.assertIsNone(P.r_multiple("BUY", 100, 104, 100))

    def test_signal_row_uses_original_sl(self):
        r = {"chat": "-1", "msg_id": 5, "symbol": "XAUUSD", "side": "BUY", "fill": 100.0, "exit": 100.0, "sl": 100.0, "sl0": 95.0, "outcome": "be"}
        row = T.signal_row(r, None, None)
        self.assertEqual(row["sl"], 95.0); self.assertEqual(row["r"], 0.0)

    def test_copied_signal_has_sl_and_r(self):
        D = lambda **k: types.SimpleNamespace(**k)
        deals = [D(position_id=1, order=1, ticket=1, entry=0, volume=0.1, price=100.0, type=0, symbol="XAUUSDc", time=1000, profit=0, swap=0, commission=0, fee=0),
                 D(position_id=1, order=2, ticket=2, entry=1, volume=0.1, price=110.0, type=1, symbol="XAUUSDc", time=2000, profit=10, swap=0, commission=0, fee=0)]
        out = SH.copied_signals(deals, {1: "-100:7"}, sl_by_key={"-100:7": 95.0})
        self.assertEqual(out[0]["sl"], 95.0); self.assertEqual(out[0]["r"], 2.0)


class SaneTps(unittest.TestCase):
    def test_pip_style_tp_dropped(self):
        self.assertEqual(T.sane_tps([1.0, 3540.0, None], 3527.0), [None, 3540.0, None])
        self.assertEqual(T.sane_tps([3540.0], None), [3540.0])


class FakeMT5:
    def __init__(self): self.alive = False; self.inits = 0
    def terminal_info(self): return types.SimpleNamespace(trade_allowed=True) if self.alive else None
    def account_info(self): return types.SimpleNamespace(login=1, server="s", balance=1, currency="USD") if self.alive else None
    def symbol_info(self, s): return object() if self.alive and s == "XAUUSDc" else None
    def symbol_select(self, s, on): return self.alive
    def symbols_get(self): return [types.SimpleNamespace(name="XAUUSDc")] if self.alive else None
    def initialize(self, **kw): self.inits += 1; self.alive = True; return True
    def shutdown(self): pass
    def last_error(self): return (-10001, "IPC send failed")


class Reconnect(unittest.TestCase):
    def test_lost_link_reconnects_and_finds_symbol(self):
        fake = FakeMT5(); old = X.mt5; X.mt5 = fake
        try:
            logs = []; ex = X.Executor({}, logs.append)
            self.assertEqual(ex.resolve_symbol("XAUUSD", {"symbol_suffix": "c"}), "XAUUSDc")
            self.assertEqual(fake.inits, 1)
            self.assertTrue(any("Reconnect" in l for l in logs))
        finally:
            X.mt5 = old

    def test_real_missing_symbol_does_not_reconnect(self):
        fake = FakeMT5(); fake.alive = True; old = X.mt5; X.mt5 = fake
        try:
            ex = X.Executor({}, lambda m: None)
            self.assertIsNone(ex.resolve_symbol("ETHEREUM", {}))
            self.assertEqual(fake.inits, 0)
        finally:
            X.mt5 = old


if __name__ == "__main__":
    unittest.main()
