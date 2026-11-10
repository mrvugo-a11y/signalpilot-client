"""MetaTrader 5 execution layer."""
import math
from .parser import Signal, Command

try:
    import MetaTrader5 as mt5
except ImportError:          # lets the parser/tests run on machines without MT5
    mt5 = None


class Executor:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log

    # ---------- connection ----------
    def connect(self):
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package not installed (pip install MetaTrader5)")
        kw = {}
        if self.cfg.get("mt5_path"): kw["path"] = self.cfg["mt5_path"]
        if self.cfg.get("mt5_login"):
            kw.update(login=int(self.cfg["mt5_login"]), password=self.cfg.get("mt5_password", ""),
                      server=self.cfg.get("mt5_server", ""))
        if not mt5.initialize(**kw):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        ai = mt5.account_info()
        ti = mt5.terminal_info()
        self.log(f"Connected to MetaTrader 5: account {ai.login} at {ai.server}, balance {ai.balance} {ai.currency}")
        if ti and not ti.trade_allowed:
            self.log("WARNING: Algo Trading is switched off in MetaTrader 5. Click the Algo Trading button so it turns green, otherwise trades are rejected.")

    def shutdown(self):
        if mt5: mt5.shutdown()

    # ---------- symbols ----------
    def resolve_symbol(self, sym, st):
        smap = {k.upper(): v for k, v in st.get("symbol_map", {}).items()}
        sym = smap.get(sym.upper(), sym).upper()
        suffix = st.get("symbol_suffix", "")
        for cand in ([sym + suffix] if suffix else []) + [sym]:
            if mt5.symbol_info(cand) is not None:
                mt5.symbol_select(cand, True); return cand
        for s in mt5.symbols_get() or []:            # auto-detect broker suffix, e.g. XAUUSDm / XAUUSD.r
            n = s.name.upper()
            if n.startswith(sym) and len(n) - len(sym) <= 4:
                mt5.symbol_select(s.name, True); return s.name
        return None

    # ---------- helpers ----------
    @staticmethod
    def _filling(info):
        fm = info.filling_mode
        if fm & 1: return mt5.ORDER_FILLING_FOK
        if fm & 2: return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _lot(self, symbol, info, side, entry, sl, n_parts, st):
        """Volume of each part (one per take profit). Returns a list of n_parts volumes.
        Risk mode: the money at risk is in the account currency (ZAR, USD, ...) and so is the loss
        MetaTrader 5 reports, so no conversion is needed. The total is worked out in volume steps and
        spread over the parts (0.05 over 3 parts gives 0.02, 0.02, 0.01) instead of rounding every
        part down to the minimum lot, which turned most risk trades into 0.01 lots."""
        step = float(info.volume_step or 0.01)
        vmin = float(info.volume_min or step); vmax = float(info.volume_max or 0) or float("inf")
        mode = str(st.get("lot_mode", "fixed") or "fixed").strip().lower()
        if mode in ("risk", "percent", "risk_percent", "percent of my balance"):
            mode = "risk"
        n_parts = max(1, int(n_parts))
        steps = lambda v: int(math.floor(float(v) / step + 1e-6))
        if mode == "risk" and not sl:
            self.log(f"{symbol}: the signal has no stop loss, so the percent of balance lot size cannot be worked out. "
                     f"Using the fixed lot size {st.get('fixed_lot', 0.01)} instead. Set a stop loss for signals without one under Settings.")
        if mode == "risk" and sl:
            acc = mt5.account_info()
            cur = getattr(acc, "currency", "") or ""
            risk_money = float(acc.balance) * float(st.get("risk_percent", 1) or 0) / 100.0
            otype = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
            loss1 = None
            try: loss1 = mt5.order_calc_profit(otype, symbol, 1.0, entry, sl)
            except Exception: loss1 = None
            if not loss1:
                tv = getattr(info, "trade_tick_value_loss", 0) or info.trade_tick_value
                loss1 = abs(entry - sl) / info.trade_tick_size * tv if info.trade_tick_size else 0
            loss1 = abs(float(loss1 or 0))
            if not loss1:
                self.log(f"{symbol}: could not work out the loss per lot, using the smallest lot {vmin}.")
                return [round(vmin, 8)] * n_parts
            want = risk_money / loss1
            total = steps(want)
            min_steps = max(1, int(round(vmin / step)))
            if total < min_steps * n_parts:
                self.log(f"{symbol}: risking {st.get('risk_percent')} percent ({risk_money:.2f} {cur}) with this stop loss "
                         f"allows {want:.4f} lots, but each of the {n_parts} trades needs at least {vmin} lots "
                         f"({vmin * loss1:.2f} {cur} at risk per trade). Opening the smallest lot size.")
                return [round(vmin, 8)] * n_parts
            base, extra = divmod(total, n_parts)
            parts = [(base + (1 if i < extra else 0)) * step for i in range(n_parts)]
            self.log(f"{symbol}: lot size from {st.get('risk_percent')} percent of {acc.balance} {cur} = {risk_money:.2f} {cur}, "
                     f"{loss1:.2f} {cur} loss per lot, {total * step:.2f} lots in total.")
        else:
            lot = float(st.get("fixed_lot", 0.01) or 0.01)
            if st.get("fixed_lot_is_total"): lot /= n_parts
            parts = [steps(lot) * step] * n_parts
        return [round(max(vmin, min(vmax, v)), 8) for v in parts]

    def _send(self, req):
        r = mt5.order_send(req)
        if r is None:
            self.log(f"MetaTrader 5 did not accept the order: {mt5.last_error()}"); return None
        if r.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL):
            self.log(f"Order rejected by the broker: {r.comment} (code {r.retcode})" + (". Algo Trading is off in MetaTrader 5." if r.retcode == 10027 else "")); return None
        return r

    def _my_count(self, st):
        mg = int(st.get("magic_number", 0))
        return sum(1 for p in (mt5.positions_get() or []) if p.magic == mg) + \
               sum(1 for o in (mt5.orders_get() or []) if o.magic == mg)

    # ---------- open ----------
    def open_signal(self, sig: Signal, st):
        symbol = self.resolve_symbol(sig.symbol, st)
        if not symbol:
            self.log(f"Skipped: your broker has no symbol called {sig.symbol}. Add it under Settings, Rename symbols."); return []
        info = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
        pt, digits = info.point, info.digits
        side = sig.side
        if st.get("reverse_signals"):
            side = "SELL" if side == "BUY" else "BUY"
        spread = (tick.ask - tick.bid) / pt
        if st.get("max_spread_points") and spread > float(st["max_spread_points"]):
            self.log(f"Skipped {symbol}: the spread is {spread:.0f} points, your limit is {st['max_spread_points']}."); return []
        if self._my_count(st) >= int(st.get("max_open_trades", 999)):
            self.log("Skipped: the most trades open at once limit is reached."); return []
        market = tick.ask if side == "BUY" else tick.bid

        # decide order type and entry price
        otype, price = sig.order_type, sig.entry
        if sig.entry_high is not None:                      # entry range
            lo, hi = sig.entry, sig.entry_high
            mode = st.get("range_entry", "market")
            inside = lo <= market <= hi or (side == "BUY" and market < lo) or (side == "SELL" and market > hi)
            if otype == "MARKET" and (mode == "market" or inside):
                price = None
            else:
                price = (lo + hi) / 2 if mode == "mid" else (hi if side == "BUY" else lo)
                if otype == "MARKET": otype = "LIMIT"
        elif otype == "MARKET" and price is not None:
            maxd = float(st.get("max_entry_distance_points", 0) or 0)
            if maxd and abs(market - price) / pt > maxd:
                if not st.get("pending_orders", True):
                    self.log(f"Skipped {symbol}: the price moved too far from the entry {price}."); return []
                better = (side == "BUY" and price < market) or (side == "SELL" and price > market)
                otype = "LIMIT" if better else "STOP"
            else:
                price = None
        if otype in ("LIMIT", "STOP") and not st.get("pending_orders", True):
            self.log("Skipped a limit or stop order, because pending orders are switched off in Settings."); return []
        if otype == "MARKET" or price is None:          # a limit or stop order without a price cannot be placed
            otype, price = "MARKET", None

        # the broker refuses stops and pending prices closer to the market than its stop level (codes 10015, 10016)
        need = max(getattr(info, "trade_stops_level", 0) or 0, getattr(info, "trade_freeze_level", 0) or 0) * pt
        bid, ask = tick.bid, tick.ask
        label = f"{side} {symbol}"
        if otype in ("LIMIT", "STOP") and price is not None:
            ok = {("BUY", "LIMIT"): price < ask - need, ("BUY", "STOP"): price > ask + need,
                  ("SELL", "LIMIT"): price > bid + need, ("SELL", "STOP"): price < bid - need}[(side, otype)]
            if not ok:
                better = (side == "BUY" and ask <= price) or (side == "SELL" and bid >= price)
                maxd = float(st.get("max_entry_distance_points", 0) or 0)
                dist = abs(market - price) / pt
                if better or not maxd or dist <= maxd:
                    self.log(f"{label}: the {otype.lower()} price {price} is already reached or too close to the market "
                             f"({bid} / {ask}), entering at the market price instead.")
                    otype, price = "MARKET", None
                else:
                    self.log(f"Skipped {label}: the {otype.lower()} price {price} is on the wrong side of the market "
                             f"({bid} / {ask}) and the price is {dist:.0f} points away, more than your limit of {maxd:.0f}.")
                    return []
        ref = price if price is not None else market

        sl = sig.sl
        if st.get("reverse_signals"):
            sl, tps = (sig.tps[0] if sig.tps else None), [sig.sl]
        else:
            tps = list(sig.tps) or [None]
        if not sl and st.get("default_sl_points"):
            d = float(st["default_sl_points"]) * pt
            sl = ref - d if side == "BUY" else ref + d
        # a market order is checked against bid and ask, a pending order against its own price
        lo_ref = bid if price is None else price          # where a BUY stop loss must stay below
        hi_ref = ask if price is None else price          # where a SELL stop loss must stay above
        if sl:
            if side == "BUY" and not sl < lo_ref - need:
                self.log(f"Skipped {label}: the stop loss {sl} is already hit or too close to the price {lo_ref}."); return []
            if side == "SELL" and not sl > hi_ref + need:
                self.log(f"Skipped {label}: the stop loss {sl} is already hit or too close to the price {hi_ref}."); return []
        use = st.get("use_tps", [1, 2, 3, 4, 5])
        chosen = [tp for i, tp in enumerate(tps, 1) if i in use] or tps[:1]
        tp_ok = lambda tp: tp is None or (tp > (ask if price is None else price) + need if side == "BUY"
                                          else tp < (bid if price is None else price) - need)
        passed = [tp for tp in chosen if not tp_ok(tp)]
        if passed:
            chosen = [tp for tp in chosen if tp_ok(tp)]
            if not chosen:
                self.log(f"Skipped {label}: the price {market} is already past every take profit "
                         f"({', '.join(map(str, passed))})."); return []
            self.log(f"{label}: take profit {', '.join(map(str, passed))} already reached, "
                     f"copying the remaining take profits {', '.join(str(t) for t in chosen if t is not None) or 'none'}.")
        if not st.get("split_tps", True):
            chosen = chosen[:1]
        lots = self._lot(symbol, info, side, ref, sl, len(chosen), st)

        type_map = {("BUY", "MARKET"): mt5.ORDER_TYPE_BUY, ("SELL", "MARKET"): mt5.ORDER_TYPE_SELL,
                    ("BUY", "LIMIT"): mt5.ORDER_TYPE_BUY_LIMIT, ("SELL", "LIMIT"): mt5.ORDER_TYPE_SELL_LIMIT,
                    ("BUY", "STOP"): mt5.ORDER_TYPE_BUY_STOP, ("SELL", "STOP"): mt5.ORDER_TYPE_SELL_STOP}
        self.last_tp1 = tps[0] if tps and tps[0] else None   # TP1 price, used by the break even watcher
        tickets = []
        for i, tp in enumerate(chosen, 1):
            lot = lots[i - 1]
            req = {"action": mt5.TRADE_ACTION_DEAL if otype == "MARKET" else mt5.TRADE_ACTION_PENDING,
                   "symbol": symbol, "volume": lot, "type": type_map[(side, otype)],
                   "price": round(ref, digits), "deviation": int(st.get("slippage_points", 20)),
                   "magic": int(st.get("magic_number", 0)), "comment": f"{st.get('comment','TGC')} TP{i}"[:31],
                   "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(info)}
            if sl: req["sl"] = round(self._norm(sl, info), digits)
            if tp: req["tp"] = round(self._norm(tp, info), digits)
            r = self._send(req)
            if r:
                t = r.order or r.deal
                tickets.append(t)
                self.log(f"TRADE opened: {side} {symbol} {lot} lots at {req['price']}, stop loss {req.get('sl') or 'none'}, take profit {req.get('tp') or 'none'} (ticket {t})")
        return tickets

    # ---------- manage ----------
    def _position(self, t):
        p = mt5.positions_get(ticket=t)
        return p[0] if p else None

    def _order(self, t):
        o = mt5.orders_get(ticket=t)
        return o[0] if o else None

    def _modify(self, p, sl=None, tp=None):
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
               "sl": sl if sl is not None else p.sl, "tp": tp if tp is not None else p.tp}
        if self._send(req): self.log(f"Trade {p.ticket} updated: stop loss {req['sl']}, take profit {req['tp']}")

    def _modify_order(self, o, sl=None, tp=None):
        req = {"action": mt5.TRADE_ACTION_MODIFY, "order": o.ticket, "price": o.price_open,
               "sl": sl if sl is not None else o.sl, "tp": tp if tp is not None else o.tp,
               "type_time": o.type_time}
        if self._send(req): self.log(f"Pending order {o.ticket} updated: stop loss {req['sl']}, take profit {req['tp']}")

    def _close(self, p, fraction=1.0):
        info = mt5.symbol_info(p.symbol); tick = mt5.symbol_info_tick(p.symbol)
        vol = p.volume * fraction
        step = info.volume_step or 0.01
        vol = round(math.floor(vol / step + 1e-9) * step, 8)
        if vol < info.volume_min:
            if fraction < 1: self.log(f"Trade {p.ticket} is too small to close only part of it, left open."); return
            vol = p.volume
        vol = min(vol, p.volume)
        buy = p.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol, "volume": vol,
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
               "price": tick.bid if buy else tick.ask, "deviation": 30, "magic": p.magic,
               "type_filling": self._filling(info)}
        if self._send(req): self.log(f"TRADE closed: {vol} lots of trade {p.ticket} ({p.symbol})")

    def _cancel(self, o):
        if self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket}):
            self.log(f"Pending order {o.ticket} cancelled")

    # ---------- break even after TP1 ----------
    def _closed_at_tp(self, ticket, tp1):
        """True if this position is closed and its closing deal was a take profit (or at the TP1 price)."""
        from . import mt5hist as H
        deals = H.position_deals(mt5, ticket)
        reason_tp = getattr(mt5, "DEAL_REASON_TP", 5)
        entry_out = getattr(mt5, "DEAL_ENTRY_OUT", 1)
        for d in deals:
            if getattr(d, "entry", None) != entry_out: continue
            if getattr(d, "reason", None) == reason_tp: return True
            if tp1:
                info = mt5.symbol_info(d.symbol); pt = info.point if info else 0
                if abs(d.price - tp1) <= max(pt * 2, 1e-9): return True
        return False

    @staticmethod
    def _norm(price, info):
        """Rounds a price to the symbol's tick size and digits (brokers reject unrounded stops)."""
        ts = getattr(info, "trade_tick_size", 0) or info.point
        return round(round(price / ts) * ts, info.digits)

    def _be_blocker(self, p, info, tick):
        """None when a break even stop is allowed right now, otherwise a plain reason.
        A BUY stop must be below the bid and a SELL stop above the ask, at least the broker's stop level
        away; inside the freeze level the broker refuses any change (code 10016, Invalid stops)."""
        pt = info.point
        buy = p.type == mt5.POSITION_TYPE_BUY
        be = self._norm(p.price_open, info)
        now = tick.bid if buy else tick.ask
        gap = (now - be) if buy else (be - now)
        need = max(getattr(info, "trade_stops_level", 0) or 0, getattr(info, "trade_freeze_level", 0) or 0) * pt
        if gap <= 0:
            return "price is " + ("below" if buy else "above") + " entry, the trade is not in profit yet"
        if gap < need:
            return "price is too close to entry for your broker's minimum stop distance"
        if p.tp:                                        # the existing take profit must not sit inside the freeze level
            fl = (getattr(info, "trade_freeze_level", 0) or 0) * pt
            if fl and abs(p.tp - now) <= fl:
                return "price is too close to the take profit for your broker to accept a change"
        return None

    def _move_to_be(self, p, warned):
        """Moves one position's stop loss to its entry price, respecting the broker's minimum stop distance.
        Returns True when done (or nothing to do), False to try again later."""
        entry = p.price_open
        buy = p.type == mt5.POSITION_TYPE_BUY
        if p.sl and ((buy and p.sl >= entry) or (not buy and p.sl <= entry)):
            return True                                       # already at break even or better
        info = mt5.symbol_info(p.symbol); tick = mt5.symbol_info_tick(p.symbol)
        why = self._be_blocker(p, info, tick)
        if why:
            if p.ticket not in warned:
                warned.add(p.ticket)
                self.log(f"Could not move the stop loss of trade {p.ticket} ({p.symbol}) to break even: {why}. "
                         "Will retry when possible.")
            return False
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
               "sl": self._norm(entry, info), "tp": p.tp}
        r = mt5.order_send(req)
        if r is None or r.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            if p.ticket not in warned:
                warned.add(p.ticket)
                why = r.comment if r is not None else "no answer from MetaTrader 5"
                self.log(f"Your broker refused to move the stop loss of trade {p.ticket} ({p.symbol}) to break even: {why}. "
                         "Will retry when possible.")
            return False
        warned.discard(p.ticket)
        self.log(f"Stop loss of trade {p.ticket} ({p.symbol}) was moved to break even at {req['sl']}")
        return True

    def move_to_be_now(self, positions, warned):
        """For a channel 'move SL to BE' instruction. Returns the tickets that still need a retry."""
        return [p.ticket for p in positions if not self._move_to_be(p, warned)]

    def check_break_even(self, rec, st, warned=None):
        """Called regularly for each copied signal. Returns True when this signal needs no more checking.
        TP1 counts as hit when the TP1 trade closed at its take profit, or the price reached TP1."""
        if warned is None: warned = set()
        tickets = rec.get("tickets") or []
        tp1 = rec.get("tp1")
        if "tp1" not in rec and rec.get("tps") and not st.get("reverse_signals"):
            tp1 = rec["tps"][0]                                 # signals copied before version 1.3.1
        if not tickets: return True
        positions = [p for p in (self._position(t) for t in tickets) if p]
        first_open = bool(positions and positions[0].ticket == tickets[0])
        hit = False
        if not first_open and not self._order(tickets[0]):
            if self._closed_at_tp(tickets[0], tp1): hit = True
            elif not positions: return True                    # everything closed, nothing to protect
            elif not tp1: return True                          # TP1 trade closed another way and no price to watch
        if not hit and tp1 and positions:
            p = positions[0]; tick = mt5.symbol_info_tick(p.symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            hit = (tick.bid >= tp1) if buy else (tick.ask <= tp1)
        if not hit: return not positions and not any(self._order(t) for t in tickets)
        rest = [p for p in positions if p.ticket != tickets[0] or len(tickets) == 1]
        if not rest: return True
        return all([self._move_to_be(p, warned) for p in rest])

    def apply(self, cmd: Command, tickets, st, warned=None):
        """Returns a list of position tickets whose break even move must be retried later (may be empty)."""
        if warned is None: warned = set()
        positions = [p for p in (self._position(t) for t in tickets) if p]
        orders = [o for o in (self._order(t) for t in tickets) if o]
        if not positions and not orders:
            self.log(f"{cmd.action.replace('_', ' ').lower()}: no open trades left for this signal"); return []
        a = cmd.action
        if a == "CLOSE_ALL":
            for p in positions: self._close(p)
            for o in orders: self._cancel(o)
        elif a == "CANCEL":
            for o in orders: self._cancel(o)
        elif a in ("CLOSE_HALF", "CLOSE_PARTIAL"):
            frac = 0.5 if a == "CLOSE_HALF" else float(cmd.value)
            if len(positions) > 1:          # split trade: close that share of the trades (furthest TP last)
                n = max(1, round(len(positions) * frac))
                for p in positions[:n]: self._close(p)
            else:
                for p in positions: self._close(p, frac)
        elif a == "MOVE_SL_BE":
            if orders:   # a stop loss equal to the open price is never valid on a pending order
                self.log(f"Break even skipped for {len(orders)} pending order(s): they have not opened yet.")
            return self.move_to_be_now(positions, warned)
        elif a == "MOVE_SL":
            for p in positions: self._modify(p, sl=cmd.value)
            for o in orders: self._modify_order(o, sl=cmd.value)
        elif a == "TP_HIT":
            act = st.get("tp_hit_action", "none")
            if act == "move_sl_be": act = "none"            # old choice, the tick box decides now
            be = st.get("be_after_tp1", False)
            retry = []
            if be and act == "none":
                retry = self.move_to_be_now(positions, warned)
            elif not be and act == "none":
                self.log("The channel says a take profit was hit. Stop loss left as it is, because the break even tick box in Settings is off.")
            if act == "close_half":
                for p in positions: self._close(p, 0.5)
            elif act == "close_all":
                for p in positions: self._close(p)
            if act != "none" or be:
                for o in orders: self._cancel(o)   # TP already reached -> pending entries are stale
            return retry
        elif a == "SL_HIT":
            for p in positions: self._close(p)
            for o in orders: self._cancel(o)
        elif a == "MODIFY":
            items = positions + orders
            for i, it in enumerate(items):
                tp = None
                if cmd.tps:
                    tp = cmd.tps[min(i, len(cmd.tps) - 1)] if len(items) > 1 else cmd.tps[0]
                fn = self._modify if it in positions else self._modify_order
                fn(it, sl=cmd.sl, tp=tp)
        return []
