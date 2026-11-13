"""Paper tracking of channels the customer follows but does not copy.

For a followed channel with copying off, every signal is read by the same parser the copier uses and followed
on paper: NO order is ever sent to MetaTrader 5. Prices come from read only MT5 calls (symbol_info_tick and
copy_rates_from_pos, one minute bars), so a tracker also catches up after the computer was off.

How one signal is followed:
  entry    market signal: the price when the signal arrived (ask for a buy, bid for a sell).
           limit or stop signal: filled when the price reaches the entry; it expires after PENDING_HOURS if it never fills.
  outcome  the first of: take profit TARGET_TP (TP1 by default) reached, stop loss reached, the channel says close,
           or MAX_OPEN_DAYS passed (closed at the market price then). If one bar touches both the stop loss and the
           take profit, the stop loss is counted (the cautious reading).
  extra    after the result is fixed at TP1, the tracker keeps watching and records the pips at TP2, TP3 and so on,
           until the stop loss or the last take profit is reached.
Buys are closed on the bid and sells on the ask (MT5 bars are bid prices; the ask is the bid plus the bar's spread).
Pips use the website's pip table (copier.pips). Trackers are saved in tracked.json so they survive restarts."""
import json, os, threading, time
from datetime import datetime, timezone
from . import config as C, pips as P

PENDING_HOURS = 24
MAX_OPEN_DAYS = 7
TARGET_TP = 1
MAX_BARS = 20000
KEEP_DAYS = 90
MAX_KEEP = 4000
COUNTED = ("tp", "sl", "be", "closed", "timeout")      # outcomes that carry pips
OUTCOME_TEXT = {"tp": "take profit", "sl": "stop loss", "be": "stopped at entry", "closed": "closed by the channel",
                "timeout": f"closed after {MAX_OPEN_DAYS} days", "expired": "entry never reached", "cancelled": "cancelled by the channel",
                "invalid": "stop loss or take profits already passed", "no_symbol": "symbol not at your broker"}


def _g(o, k, default=None):
    if isinstance(o, dict): return o.get(k, default)
    if hasattr(o, "dtype") and getattr(o.dtype, "names", None) and k in o.dtype.names: return o[k]   # numpy row
    return getattr(o, k, default)


def utc_text(ts):
    if ts is None: return None
    try: return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError): return None


def target_tp(rec):
    """(index starting at 1, price) of the take profit that fixes the result, or (None, None)."""
    tps = rec.get("tps") or []
    want = max(1, int(rec.get("target_tp") or TARGET_TP))
    if len(tps) >= want and tps[want - 1] is not None: return want, float(tps[want - 1])
    for i, v in enumerate(tps):
        if v is not None: return i + 1, float(v)
    return None, None


def _close(rec, price, srv, outcome):
    rec["exit"] = float(price); rec["exit_srv"] = int(srv)
    rec["pips"] = P.pips(rec.get("broker") or rec["symbol"], rec["side"], rec["fill"], price)
    rec["outcome"] = outcome; rec["rev"] = int(rec.get("rev", 0)) + 1


def advance(rec, bars, point):
    """Follows one tracker through closed one minute bars (oldest first; only bars newer than rec['srv_last']
    are used). Pure: no MT5 calls. Returns True when the record changed."""
    changed = False
    buy = rec["side"] == "BUY"
    for b in bars:
        t = int(_g(b, "time"))
        if t <= int(rec.get("srv_last") or 0): continue
        hi, lo, cl = float(_g(b, "high")), float(_g(b, "low")), float(_g(b, "close"))
        sp = float(_g(b, "spread", 0) or 0) * float(point or 0)
        st = rec["state"]
        if st == "pending":
            if t > int(rec["srv_start"]) + PENDING_HOURS * 3600:
                rec["state"], rec["outcome"] = "done", "expired"; rec["rev"] = int(rec.get("rev", 0)) + 1
                rec["srv_last"] = t; changed = True; break
            px, kind = float(rec["pending_price"]), rec["kind"]
            if buy: filled = (lo + sp <= px) if kind == "LIMIT" else (hi + sp >= px)
            else: filled = (hi >= px) if kind == "LIMIT" else (lo <= px)
            if filled:
                rec["state"], rec["fill"], rec["fill_srv"] = "open", px, t
            rec["srv_last"] = t; changed = True
            continue                                       # stop loss and take profits count from the next bar
        if st in ("open", "watching"):
            ex_hi, ex_lo = (hi, lo) if buy else (hi + sp, lo + sp)       # price a buy or a sell closes at
            sl = rec.get("sl")
            if sl is not None and ((buy and ex_lo <= sl) or (not buy and ex_hi >= sl)):
                if st == "open":
                    be = abs(float(sl) - float(rec["fill"])) <= float(point or 0) * 2
                    _close(rec, sl, t, "be" if be else "sl")
                rec["state"] = "done"; rec["srv_last"] = t; changed = True; break
            for i, tp in enumerate(rec.get("tps") or []):
                k = str(i + 1)
                if tp is None or k in rec.setdefault("tp_hits", {}): continue
                if (buy and ex_hi >= tp) or (not buy and ex_lo <= tp):
                    rec["tp_hits"][k] = t
                    rec.setdefault("tp_pips", {})[k] = P.pips(rec.get("broker") or rec["symbol"], rec["side"], rec["fill"], tp)
                    rec["rev"] = int(rec.get("rev", 0)) + 1
            idx, px = target_tp(rec)
            if rec["state"] == "open" and idx and str(idx) in rec.get("tp_hits", {}):
                _close(rec, px, t, "tp"); rec["state"] = "watching"
            numeric = [str(i + 1) for i, v in enumerate(rec.get("tps") or []) if v is not None]
            if rec["state"] == "watching" and all(k in rec.get("tp_hits", {}) for k in numeric):
                rec["state"] = "done"
            if rec["state"] in ("open", "watching") and t >= int(rec.get("fill_srv") or rec["srv_start"]) + MAX_OPEN_DAYS * 86400:
                if rec["state"] == "open": _close(rec, cl if buy else cl + sp, t, "timeout")
                rec["state"] = "done"
            rec["srv_last"] = t; changed = True
            if rec["state"] == "done": break
    return changed


class Market:
    """Read only MT5 prices. resolve(symbol, settings) maps a signal symbol to the broker's symbol
    (Executor.resolve_symbol). Never sends orders."""
    def __init__(self, mt5, resolve):
        self.mt5, self.resolve = mt5, resolve

    def tick(self, sym):
        return self.mt5.symbol_info_tick(sym)

    def point(self, sym):
        i = self.mt5.symbol_info(sym)
        return float(getattr(i, "point", 0) or 0) if i else 0.0

    def bars(self, sym, count):
        """Closed one minute bars, oldest first (the bar still forming is left out)."""
        r = self.mt5.copy_rates_from_pos(sym, self.mt5.TIMEFRAME_M1, 0, int(max(2, min(count, MAX_BARS))))
        if r is None: return []
        r = list(r)
        return r[:-1]


class Tracker:
    def __init__(self, path=None, log=None):
        self.path = path or C.path("tracked.json")
        self.lock = threading.RLock(); self.log = log or (lambda m: None)
        try:
            with open(self.path, encoding="utf-8") as f: self.data = json.load(f)
        except Exception:
            self.data = {}
        for k, v in (("signals", {}), ("last", {}), ("aliases", {}), ("trade_ids", {})): self.data.setdefault(k, v)

    # -- storage --
    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def save(self):
        with self.lock: self._save()

    def _key(self, chat, msg):
        k = f"{chat}:{msg}"
        return self.data["aliases"].get(k, k)

    def get(self, chat, msg):
        return self.data["signals"].get(self._key(chat, msg)) if msg is not None else None

    def last_id(self, chat):
        return self.data["last"].get(str(chat))

    def alias(self, chat, msg, real):
        with self.lock:
            k = f"{chat}:{msg}"
            if k not in self.data["signals"]: self.data["aliases"][k] = f"{chat}:{real}"; self._save()

    def has_trade(self, chat, trade_id):
        return bool(trade_id) and f"{chat}:{trade_id}" in self.data["trade_ids"]

    def mark_trade(self, chat, trade_id, msg):
        if not trade_id: return
        with self.lock: self.data["trade_ids"][f"{chat}:{trade_id}"] = msg; self._save()

    def _offset(self, tick_time, now):
        """Broker server clock minus UTC (MT5 bar times are server time), rounded to 15 minutes.
        Only trusted from a fresh tick; otherwise the last good value."""
        if tick_time:
            off = round((float(tick_time) - now) / 900.0) * 900
            if abs(float(tick_time) - now - off) <= 420:
                self.data["offset"] = int(off); return int(off)
        return int(self.data.get("offset", 0))

    # -- signals --
    def add(self, market, chat, msg_id, sig, st=None, name=None, posted=None, now=None):
        """Starts a paper tracker for a parsed Signal. Returns the record (also for skipped ones) or None if it is a repost."""
        now = time.time() if now is None else now
        chat = str(chat); key = f"{chat}:{msg_id}"
        tid = getattr(sig, "trade_id", None)
        if tid and self.has_trade(chat, tid): return None
        side = sig.side.upper()                          # followed as the channel posted it (never reversed)
        rec = {"key": key, "chat": chat, "msg_id": int(msg_id), "channel_name": name, "symbol": sig.symbol, "broker": None,
               "side": side, "order_type": (sig.order_type or "MARKET").upper(), "entry": sig.entry, "entry_high": sig.entry_high,
               "sl": sig.sl, "sl0": sig.sl, "tps": list(sig.tps or []), "target_tp": TARGET_TP, "created": float(posted or now),
               "state": "done", "outcome": None, "fill": None, "exit": None, "pips": None, "tp_hits": {}, "tp_pips": {},
               "source": "tracked", "rev": 1, "uploaded_rev": 0}
        broker = market.resolve(sig.symbol, st or {}) if market else None
        tick = market.tick(broker) if broker else None
        if not broker or tick is None or not (getattr(tick, "bid", 0) and getattr(tick, "ask", 0)):
            rec["outcome"] = "no_symbol"
        else:
            rec["broker"] = broker
            bid, ask = float(tick.bid), float(tick.ask)
            srv = int(getattr(tick, "time", 0) or 0) or int(now + self._offset(None, now))
            rec["offset"] = self._offset(getattr(tick, "time", None), now)
            rec["srv_start"] = rec["srv_last"] = srv
            buy = side == "BUY"; mkt = ask if buy else bid; close_px = bid if buy else ask
            kind, price = rec["order_type"], sig.entry
            if sig.entry_high is not None:                              # entry zone, as the copier's default "market"
                lo, hi = float(sig.entry), float(sig.entry_high)
                inside = lo <= mkt <= hi or (buy and mkt < lo) or (not buy and mkt > hi)
                if kind == "MARKET" or inside: kind, price = "MARKET", None
                else: price = hi if buy else lo
            if kind in ("LIMIT", "STOP") and price is not None:
                p = float(price)
                crossed = (buy and ((kind == "LIMIT" and p >= ask) or (kind == "STOP" and p <= ask))) or \
                          (not buy and ((kind == "LIMIT" and p <= bid) or (kind == "STOP" and p >= bid)))
                if crossed: kind = "MARKET"
            else:
                kind = "MARKET"
            ref = mkt if kind == "MARKET" else float(price)
            sl = rec["sl"]
            if sl is not None and ((buy and (sl >= ref or (kind == "MARKET" and sl >= close_px))) or
                                   (not buy and (sl <= ref or (kind == "MARKET" and sl <= close_px)))):
                rec["outcome"] = "invalid"
            else:
                if kind == "MARKET":             # take profits already passed are dropped, as the copier does
                    keep = [tp for tp in rec["tps"] if tp is None or (buy and tp > close_px) or (not buy and tp < close_px)]
                    if rec["tps"] and not [tp for tp in keep if tp is not None] and [tp for tp in rec["tps"] if tp is not None]:
                        rec["outcome"] = "invalid"
                    rec["tps"] = keep
                if rec["outcome"] is None:
                    if kind == "MARKET":
                        rec.update(state="open", fill=mkt, fill_srv=srv, kind="MARKET")
                    else:
                        rec.update(state="pending", kind=kind, pending_price=float(price))
        with self.lock:
            self.data["signals"][key] = rec
            self.data["last"][chat] = int(msg_id)
            if tid: self.data["trade_ids"][f"{chat}:{tid}"] = int(msg_id)
            self._prune(now); self._save()
        return rec

    def apply(self, market, chat, msg_id, cmd, now=None):
        """A channel instruction for a tracked signal (edit, reply or command). Returns True if it changed something."""
        rec = self.get(chat, msg_id)
        if not rec or rec["state"] == "done": return False
        now = time.time() if now is None else now
        a = cmd.action; ch = False
        if market and rec.get("broker"):
            self.update(market, now, keys=[rec["key"]])          # bring prices up to now before acting
            rec = self.get(chat, msg_id)
            if rec["state"] == "done": return True
        with self.lock:
            if a == "MODIFY":
                if cmd.sl is not None and cmd.sl != rec.get("sl"): rec["sl"] = cmd.sl; ch = True
                if cmd.tps and list(cmd.tps) != rec.get("tps"): rec["tps"] = list(cmd.tps); ch = True
            elif a == "MOVE_SL" and cmd.value is not None:
                rec["sl"] = float(cmd.value); ch = True
            elif a == "MOVE_SL_BE" and rec["state"] in ("open", "watching"):
                rec["sl"] = rec["fill"]; ch = True
            elif a == "CANCEL" and rec["state"] == "pending":
                rec["state"], rec["outcome"] = "done", "cancelled"; ch = True
            elif a == "CLOSE_ALL" and rec["state"] in ("open", "watching"):
                if rec["state"] == "open":
                    tick = market.tick(rec["broker"]) if market else None
                    if tick is not None:
                        px = float(tick.bid if rec["side"] == "BUY" else tick.ask)
                        _close(rec, px, int(getattr(tick, "time", 0) or rec.get("srv_last") or 0), "closed")
                rec["state"] = "done"; ch = True
            elif a == "CLOSE_ALL" and rec["state"] == "pending":
                rec["state"], rec["outcome"] = "done", "cancelled"; ch = True
            if ch:
                rec["rev"] = int(rec.get("rev", 0)) + 1; self._save()
        return ch

    def active(self):
        return [r for r in self.data["signals"].values() if r.get("state") in ("pending", "open", "watching")]

    def update(self, market, now=None, keys=None):
        """Reads new one minute bars for every active tracker and follows them. Catches up after downtime
        (up to MAX_BARS minutes back). Returns how many records changed."""
        now = time.time() if now is None else now
        recs = [r for r in self.active() if r.get("broker") and (keys is None or r["key"] in keys)]
        by = {}
        for r in recs: by.setdefault(r["broker"], []).append(r)
        n = 0
        for sym, rs in by.items():
            try:
                since = min(float(r.get("checked") or r["created"]) for r in rs)
                bars = market.bars(sym, int((now - since) / 60) + 5)
                point = market.point(sym)
            except Exception as e:
                self.log(f"Pip tracking could not read {sym} prices: {e}"); continue
            with self.lock:
                for r in rs:
                    if advance(r, bars, point): n += 1
                    r["checked"] = now
        with self.lock:
            for r in self.active():                      # wall clock limits, for symbols that stopped sending prices
                age = now - float(r["created"])
                if r["state"] == "pending" and age > (PENDING_HOURS + 1) * 3600:
                    r["state"], r["outcome"] = "done", "expired"; r["rev"] = int(r.get("rev", 0)) + 1; n += 1
                elif r["state"] in ("open", "watching") and age > (MAX_OPEN_DAYS + 1) * 86400:
                    if r["state"] == "open":
                        tick = None
                        try: tick = market.tick(r["broker"]) if market else None
                        except Exception: pass
                        if tick is not None and getattr(tick, "bid", 0):
                            _close(r, float(tick.bid if r["side"] == "BUY" else tick.ask), int(getattr(tick, "time", 0) or 0), "timeout")
                    r["state"] = "done"; n += 1
            if recs or n: self._save()
        return n

    def _prune(self, now):
        sig = self.data["signals"]
        old = [k for k, r in sig.items() if r.get("state") == "done" and now - float(r.get("created") or 0) > KEEP_DAYS * 86400]
        for k in old: del sig[k]
        if len(sig) > MAX_KEEP:
            done = sorted((k for k, r in sig.items() if r.get("state") == "done"), key=lambda k: sig[k].get("created") or 0)
            for k in done[:len(sig) - MAX_KEEP]: del sig[k]

    # -- results --
    def counted(self, since=0):
        """Records with a fixed result and pips, closed after `since` (UTC seconds)."""
        out = []
        for r in self.data["signals"].values():
            if r.get("outcome") not in COUNTED: continue
            if since and self.close_utc(r) and self.close_utc(r) < since: continue
            out.append(r)
        return out

    @staticmethod
    def to_utc(rec, srv):
        return None if srv is None else float(srv) - float(rec.get("offset") or 0)

    def close_utc(self, rec):
        return self.to_utc(rec, rec.get("exit_srv"))

    def upload_rows(self, limit=500):
        """Per signal records for the website, newest first. Includes results that changed since the last upload
        and every result of the last 30 days, so a server that missed an upload still gets them."""
        cut = time.time() - 30 * 86400
        rows = []
        for r in sorted(self.counted(), key=lambda r: r.get("created") or 0, reverse=True):
            if int(r.get("uploaded_rev", 0)) >= int(r.get("rev", 0)) and (self.close_utc(r) or 0) < cut: continue
            rows.append(signal_row(r, self.to_utc(r, r.get("fill_srv")), self.close_utc(r)))
            if len(rows) >= limit: break
        return rows

    def mark_uploaded(self, ids):
        ids = set(ids)
        with self.lock:
            for r in self.data["signals"].values():
                if r["key"] in ids: r["uploaded_rev"] = int(r.get("rev", 0))
            self._save()

    def channel_rows(self, since=0, names=None):
        """Per channel totals of tracked signals for the Channel results window."""
        names = names or {}; rows = {}
        for r in self.counted(since):
            c = rows.setdefault(r["chat"], {"channel": r["chat"], "name": names.get(r["chat"]) or r.get("channel_name") or r["chat"],
                                            "trades": 0, "wins": 0, "pips": 0.0, "pips_trades": 0, "source": "tracked"})
            c["trades"] += 1
            if r.get("pips") is not None:
                c["pips"] += float(r["pips"]); c["pips_trades"] += 1
                if r["pips"] > 0: c["wins"] += 1
        out = []
        for c in rows.values():
            c["pips"] = round(c["pips"], 1) if c["pips_trades"] else None
            c["win_rate"] = round(100.0 * c["wins"] / c["trades"], 1) if c["trades"] else 0.0
            out.append(c)
        return sorted(out, key=lambda c: (c["pips"] is None, -(c["pips"] or 0)))


def signal_row(r, open_utc, close_utc, source="tracked"):
    """One per signal record as sent to the website."""
    return {"channel": str(r["chat"]), "signal_id": f"{r['chat']}:{r['msg_id']}", "msg_id": int(r["msg_id"]),
            "symbol": r.get("broker") or r.get("symbol"), "side": r["side"], "entry": r.get("fill"), "exit": r.get("exit"),
            "pips": r.get("pips"), "outcome": r.get("outcome"), "signal_time": utc_text(r.get("created")),
            "open_time": utc_text(open_utc), "close_time": utc_text(close_utc), "source": source,
            "tp_pips": {k: v for k, v in (r.get("tp_pips") or {}).items() if v is not None}}
