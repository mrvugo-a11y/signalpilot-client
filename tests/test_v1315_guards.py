"""Version 1.3.15: old signals are skipped, prices are checked before sending (no 10015/10016 from opening
orders), and a call without a stop loss (BUY XAUUSD NOW) waits for the same channel to send the numbers."""
import asyncio, datetime, os, sys, tempfile, time, types, unittest
from collections import namedtuple
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from copier import executor, engine as E, config as C, settings_schema as S
from copier.parser import parse, parse_bare_call, parse_update, Signal
from copier.store import Store

Info = namedtuple("Info", "name point digits volume_step volume_min volume_max trade_tick_size trade_tick_value "
                          "filling_mode trade_stops_level trade_freeze_level")
Tick = namedtuple("Tick", "bid ask")
Res = namedtuple("Res", "retcode order deal comment")


def fake(bid, ask, stops=0, freeze=0, sym="XAUUSDm", point=0.01, digits=2):
    m = types.SimpleNamespace()
    for i, n in enumerate("ORDER_TYPE_BUY ORDER_TYPE_SELL ORDER_TYPE_BUY_LIMIT ORDER_TYPE_SELL_LIMIT ORDER_TYPE_BUY_STOP ORDER_TYPE_SELL_STOP".split()):
        setattr(m, n, i)
    m.TRADE_ACTION_DEAL, m.TRADE_ACTION_PENDING = 1, 5
    m.ORDER_FILLING_FOK = m.ORDER_FILLING_IOC = m.ORDER_FILLING_RETURN = 0
    m.ORDER_TIME_GTC = 0; m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, m.TRADE_RETCODE_DONE_PARTIAL = 10009, 10008, 10010
    info = Info(sym, point, digits, 0.01, 0.01, 100, point, point, 1, stops, freeze)
    m.info = info
    m.symbol_info = lambda s: info if s == sym else None
    m.symbols_get = lambda: [info]; m.symbol_select = lambda s, b: True
    m.symbol_info_tick = lambda s: Tick(bid, ask)
    m.account_info = lambda: types.SimpleNamespace(balance=10000, currency="USD")
    m.order_calc_profit = lambda t, s, v, a, b: -(abs(a - b) * 100 * v)
    m.positions_get = lambda **k: []; m.orders_get = lambda **k: []
    m.sent = []
    def send(r):
        m.sent.append(r)
        return Res(_broker_check(m, r), 1000 + len(m.sent), 0, "check")
    m.order_send = send; m.last_error = lambda: None
    return m


def _broker_check(m, r):
    """What a real broker answers: 10015 Invalid price, 10016 Invalid stops, else done."""
    t = m.symbol_info_tick(r["symbol"]); need = max(m.info.trade_stops_level, m.info.trade_freeze_level) * m.info.point
    typ, p, sl, tp = r["type"], r["price"], r.get("sl"), r.get("tp")
    buy = typ in (m.ORDER_TYPE_BUY, m.ORDER_TYPE_BUY_LIMIT, m.ORDER_TYPE_BUY_STOP)
    if typ == m.ORDER_TYPE_BUY_LIMIT and not p < t.ask - need: return 10015
    if typ == m.ORDER_TYPE_BUY_STOP and not p > t.ask + need: return 10015
    if typ == m.ORDER_TYPE_SELL_LIMIT and not p > t.bid + need: return 10015
    if typ == m.ORDER_TYPE_SELL_STOP and not p < t.bid - need: return 10015
    if typ == m.ORDER_TYPE_BUY: p = t.ask
    if typ == m.ORDER_TYPE_SELL: p = t.bid
    if buy:
        lo = t.bid if typ == m.ORDER_TYPE_BUY else p
        if sl and not sl < lo - need: return 10016
        if tp and not tp > p + need: return 10016
    else:
        hi = t.ask if typ == m.ORDER_TYPE_SELL else p
        if sl and not sl > hi + need: return 10016
        if tp and not tp < p - need: return 10016
    return 10009


ST = dict(C.DEFAULT_SETTINGS, lot_mode="fixed", fixed_lot=0.01, max_spread_points=0, symbol_suffix="")
ATIF = "SELL GOLD 4138 - 4142\nSL 4148\nTP 4134\nTP 4130\nTP 4125\nTP 4120\nTP 4110"


class PriceGuard(unittest.TestCase):
    def go(self, text, bid, ask, st=None, **kw):
        self.m = executor.mt5 = fake(bid, ask, **kw); self.logs = []
        t = executor.Executor({}, self.logs.append).open_signal(parse(text), dict(ST, **(st or {})))
        self.assertEqual([r for r in self.m.sent if _broker_check(self.m, r) != 10009], [], self.logs)
        return t

    def test_atif_late_sell_drops_passed_tps(self):
        # real case: gold already at 4127, below TP 4134 and 4130
        t = self.go(ATIF, 4127.00, 4127.20)
        self.assertEqual([r["tp"] for r in self.m.sent], [4125, 4120, 4110])
        self.assertEqual(len(t), 3)
        self.assertTrue(any("already reached" in l for l in self.logs), self.logs)

    def test_all_tps_passed_skips(self):
        self.assertEqual(self.go(ATIF, 4105.00, 4105.20), [])
        self.assertEqual(self.m.sent, [])
        self.assertTrue(any("past every take profit" in l for l in self.logs), self.logs)

    def test_sl_already_hit_skips(self):
        self.assertEqual(self.go(ATIF, 4150.00, 4150.20), [])
        self.assertTrue(any("stop loss 4148" in l and "already hit" in l for l in self.logs), self.logs)

    def test_sl_inside_stops_level_skips(self):
        # stop level 300 points = 3.00: a BUY stop loss 2.00 under the bid is refused
        self.assertEqual(self.go("XAUUSD BUY SL 1948 TP 1960", 1950.00, 1950.20, stops=300), [])
        self.assertEqual(self.m.sent, [])

    def test_tp_inside_stops_level_dropped(self):
        self.go("XAUUSD BUY SL 1940 TP1 1951 TP2 1960", 1950.00, 1950.20, stops=200)
        self.assertEqual([r["tp"] for r in self.m.sent], [1960])

    def test_buy_limit_above_market_enters_at_market(self):
        self.go("XAUUSD BUY LIMIT 1955 SL 1940 TP 1970", 1950.00, 1950.20)
        self.assertEqual(self.m.sent[0]["type"], self.m.ORDER_TYPE_BUY)

    def test_buy_stop_already_passed_far_skips_with_limit(self):
        self.assertEqual(self.go("XAUUSD BUY STOP 1940 SL 1930 TP 1990", 1950.00, 1950.20,
                                 st={"max_entry_distance_points": 300}), [])
        self.assertTrue(any("wrong side of the market" in l for l in self.logs), self.logs)

    def test_buy_stop_already_passed_close_goes_market(self):
        self.go("XAUUSD BUY STOP 1949 SL 1930 TP 1990", 1950.00, 1950.20, st={"max_entry_distance_points": 300})
        self.assertEqual(self.m.sent[0]["type"], self.m.ORDER_TYPE_BUY)

    def test_sell_limit_below_bid_and_sell_stop_above_bid(self):
        self.go("XAUUSD SELL LIMIT 1945 SL 1965 TP 1930", 1950.00, 1950.20)
        self.assertEqual(self.m.sent[0]["type"], self.m.ORDER_TYPE_SELL)
        self.go("XAUUSD SELL STOP 1955 SL 1965 TP 1930", 1950.00, 1950.20)
        self.assertEqual(self.m.sent[0]["type"], self.m.ORDER_TYPE_SELL)

    def test_valid_pending_orders_untouched(self):
        self.go("XAUUSD BUY LIMIT 1940 SL 1930 TP 1960", 1950.00, 1950.20)
        self.assertEqual((self.m.sent[0]["type"], self.m.sent[0]["price"]), (self.m.ORDER_TYPE_BUY_LIMIT, 1940))
        self.go("XAUUSD SELL STOP 1940 SL 1945 TP 1930", 1950.00, 1950.20, stops=100)
        self.assertEqual(self.m.sent[0]["type"], self.m.ORDER_TYPE_SELL_STOP)

    def test_pending_order_stops_checked_against_its_own_price(self):
        # BUY LIMIT 1940 with stop loss 1941 above the entry can never be accepted
        self.assertEqual(self.go("XAUUSD BUY LIMIT 1940 SL 1941 TP 1960", 1950.00, 1950.20), [])

    def test_eurusd_stale_sell_with_sl_too_close(self):
        # SELL EURUSD with stop 1.12107 while the ask is 1.12110: stop loss already hit
        self.assertEqual(self.go("EURUSD SELL 1.11807 SL 1.12107 TP 1.11500", 1.12105, 1.12110,
                                 sym="EURUSD", point=0.00001, digits=5), [])

    def test_log_text_has_no_dashes(self):
        self.go(ATIF, 4127.00, 4127.20); self.go(ATIF, 4150.00, 4150.20)
        for l in self.logs: self.assertNotIn(" - ", l)


def utc(minutes_ago):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)


class Ev:
    def __init__(self, mid, text, reply_to=None, ago=0.1, edit_ago=None, fwd_ago=None, parent_text=None):
        self.id, self.raw_text, self.chat_id = mid, text, -100
        async def grm(): return types.SimpleNamespace(raw_text=parent_text) if parent_text else None
        self.message = types.SimpleNamespace(
            reply_to_msg_id=reply_to, get_reply_message=grm, reply_to=None, date=utc(ago),
            edit_date=utc(edit_ago) if edit_ago is not None else None,
            fwd_from=types.SimpleNamespace(date=utc(fwd_ago)) if fwd_ago is not None else None)


class EngineBase(unittest.TestCase):
    settings = {}

    def setUp(self):
        self.logs = []
        e = self.e = E.Engine(self.logs.append, None)
        e.cfg = {"channels": {"-100": {"name": "KOJOFOREX-EXNESS"}}}
        e._st = lambda c: dict(C.DEFAULT_SETTINGS, **self.settings)
        e.store = Store(os.path.join(tempfile.mkdtemp(), "t.json"))
        self.opened, self.applied = [], []
        e.ex = types.SimpleNamespace(open_signal=lambda s, st: self.opened.append(s) or [700 + len(self.opened)],
                                     apply=lambda c, t, st, w=None: self.applied.append((c.action, t)) or [],
                                     last_tp1=None)

    def new(self, *a, **k): asyncio.run(self.e._on_new(Ev(*a, **k)))
    def edit(self, *a, **k): asyncio.run(self.e._on_edit(Ev(*a, **k)))


class OldSignals(EngineBase):
    def test_settings_defaults(self):
        self.assertEqual(C.DEFAULT_SETTINGS["max_signal_age_minutes"], 5)
        self.assertEqual(C.DEFAULT_SETTINGS["wait_for_sl_minutes"], 15)
        keys = [f[0] for f in S.TRADE_FIELDS]
        self.assertIn("max_signal_age_minutes", keys); self.assertIn("wait_for_sl_minutes", keys)

    def test_old_signal_is_skipped_and_logged(self):
        self.new(1, "XAUUSD SELL 4138 SL 4148 TP 4120", ago=47)
        self.assertEqual(self.opened, [])
        self.assertTrue(any("Skipped an old signal from KOJOFOREX-EXNESS sent 47 minutes ago" in l for l in self.logs), self.logs)

    def test_fresh_signal_opens(self):
        self.new(1, "XAUUSD SELL 4138 SL 4148 TP 4120", ago=1)
        self.assertEqual(len(self.opened), 1)

    def test_reconnect_replay_of_many_signals_opens_nothing(self):
        for i in range(50):
            self.new(i, f"EURUSD SELL 1.118{i:02d} SL 1.121 TP 1.115", ago=600 - i)
        self.assertEqual(self.opened, [])

    def test_forwarded_message_uses_channel_time(self):
        # posted in the watched channel a moment ago, originally written hours earlier elsewhere
        self.new(1, "BUY XAUUSD @ 4136\nTP1 4151\nSL 4121", ago=0.2, fwd_ago=300)
        self.assertEqual(len(self.opened), 1)

    def test_old_commands_do_nothing(self):
        self.e.store.put(-100, 5, {"symbol": "XAUUSD", "side": "BUY", "tickets": [9]})
        self.new(6, "close all now", ago=120)
        self.new(7, "SL 4100", reply_to=5, ago=120)
        self.new(8, "move SL to break even", reply_to=5, ago=120)
        self.assertEqual(self.applied, [])
        self.new(9, "close all now", ago=1)
        self.assertEqual(self.applied, [("CLOSE_ALL", [9])])

    def test_old_edit_does_not_open(self):
        self.edit(1, "XAUUSD SELL 4138 SL 4148 TP 4120", ago=200, edit_ago=100)
        self.assertEqual(self.opened, [])
        self.edit(2, "XAUUSD SELL 4138 SL 4148 TP 4120", ago=20, edit_ago=0.5)   # edited just now: current
        self.assertEqual(len(self.opened), 1)

    def test_zero_turns_the_check_off(self):
        self.settings = {"max_signal_age_minutes": 0}
        self.new(1, "XAUUSD SELL 4138 SL 4148 TP 4120", ago=500)
        self.assertEqual(len(self.opened), 1)


class WaitForNumbers(EngineBase):
    KOJO_FULL = "BUY XAUUSD @ 4136.00\nTP1. 4151.00\nTP2. 4181.00\nSL. 4121.00\n\nMANAGE RISK ⚠️"

    def test_parser_bare_calls(self):
        for t, side in (("BUY XAUUSD NOW", "BUY"), ("SELL XAUUSD NOW", "SELL"), ("sell gold now", "SELL"), ("🚀XAUUSD BUY NOW", "BUY")):
            b = parse_bare_call(t)
            self.assertEqual((b.side, b.symbol), (side, "XAUUSD"), t)
            self.assertIsNone(parse(t), t)
        for t in ("XAUUSD running 60 pips in Profits 🚀🚀", "TP1 hit", "Gold BUY hit SL", "Good morning traders",
                  "XAUUSD BUY 4136 SL 4121 TP 4151"):
            self.assertIsNone(parse_bare_call(t), t)

    def test_parser_dot_separators(self):
        s = parse(self.KOJO_FULL)
        self.assertEqual((s.side, s.symbol, s.entry, s.sl, s.tps), ("BUY", "XAUUSD", 4136.0, 4121.0, [4151.0, 4181.0]))
        self.assertEqual(parse("EURUSD BUY SL 1.0800 TP1.0850").tps, [1.085])   # no space: still a price

    def test_kojoforex_real_example(self):
        self.new(1, "BUY XAUUSD NOW")
        self.assertEqual(self.opened, [])
        self.assertTrue(any("Waiting for KOJOFOREX-EXNESS to send the stop loss and take profit for BUY XAUUSD" in l
                            for l in self.logs), self.logs)
        self.new(2, self.KOJO_FULL)                 # separate message (photo caption), not a reply
        self.assertEqual(len(self.opened), 1)
        s = self.opened[0]
        self.assertEqual((s.side, s.symbol, s.sl, s.tps), ("BUY", "XAUUSD", 4121.0, [4151.0, 4181.0]))
        self.new(3, "XAUUSD running 60 pips in Profits 🚀🚀")
        self.new(4, "SL 4121 TP 4151")              # waiting was used up: numbers alone open nothing now
        self.assertEqual(len(self.opened), 1)

    def test_numbers_only_follow_up_uses_remembered_side(self):
        self.new(1, "sell gold now")
        self.new(2, "SL 4150\nTP 4120\nTP 4100")
        self.assertEqual(len(self.opened), 1)
        s = self.opened[0]
        self.assertEqual((s.side, s.symbol, s.sl, s.tps), ("SELL", "XAUUSD", 4150.0, [4120.0, 4100.0]))

    def test_reply_to_call_with_stop_loss_only(self):
        self.new(10, "BUY XAUUSD NOW")
        self.new(11, "SL 4121", reply_to=10)
        self.assertEqual(len(self.opened), 1)
        self.assertEqual((self.opened[0].side, self.opened[0].sl), ("BUY", 4121.0))
        self.new(12, "TP1 hit ✅", reply_to=10)     # later replies to the call find the trade
        self.assertEqual(self.applied, [("TP_HIT", [701])])

    def test_take_profit_alone_is_not_enough(self):
        self.new(1, "BUY XAUUSD NOW")
        self.new(2, "TP 4151")
        self.assertEqual(self.opened, [])
        self.new(3, "SL 4121")
        self.assertEqual(len(self.opened), 1)

    def test_status_follow_ups_never_open(self):
        self.new(1, "BUY XAUUSD NOW")
        for i, t in enumerate(["Trade activated", "TP1 hit +50 pips", "SL hit", "running in profit SL 4121",
                               "XAUUSD running 60 pips in Profits 🚀🚀", "BUY hit SL -75 pips"]):
            self.new(2 + i, t)
        self.assertEqual(self.opened, [])

    def test_signal_with_tp_but_no_sl_waits(self):
        self.new(1, "GOLD BUY TP 4160")
        self.assertEqual(self.opened, [])
        self.new(2, "SL 4120")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual((self.opened[0].sl, self.opened[0].tps), (4120.0, [4160.0]))

    def test_waiting_expires(self):
        self.new(1, "BUY XAUUSD NOW")
        self.e._waiting["-100"]["at"] = time.time() - 16 * 60
        self.new(2, "SL 4121 TP 4151")
        self.assertEqual(self.opened, [])
        self.assertTrue(any("Stopped waiting" in l for l in self.logs), self.logs)

    def test_old_call_is_not_remembered(self):
        self.new(1, "BUY XAUUSD NOW", ago=30)
        self.new(2, "SL 4121 TP 4151")
        self.assertEqual(self.opened, [])

    def test_edit_of_call_adds_numbers(self):
        self.new(1, "BUY XAUUSD NOW")
        self.edit(1, "BUY XAUUSD NOW\nSL 4121\nTP 4151", ago=1, edit_ago=0.1)
        self.assertEqual(len(self.opened), 1)

    def test_other_channels_do_not_complete(self):
        self.new(1, "BUY XAUUSD NOW")
        ev = Ev(2, "SL 4121 TP 4151"); ev.chat_id = -200
        asyncio.run(self.e._on_new(ev))
        self.assertEqual(self.opened, [])

    def test_wait_zero_keeps_old_behaviour(self):
        self.settings = {"wait_for_sl_minutes": 0}
        self.new(1, "GOLD BUY TP 4160")
        self.assertEqual(len(self.opened), 1)
        self.new(2, "BUY XAUUSD NOW")
        self.assertEqual(len(self.opened), 1)

    def test_log_text_has_no_dashes(self):
        self.new(1, "BUY XAUUSD NOW", ago=30); self.new(2, "BUY XAUUSD NOW")
        self.e._waiting["-100"]["at"] = 0; self.e._expire_waiting()
        for l in self.logs: self.assertNotIn("-", l.replace("KOJOFOREX-EXNESS", ""), l)


if __name__ == "__main__":
    unittest.main()
