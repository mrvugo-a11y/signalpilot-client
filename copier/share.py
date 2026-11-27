"""Crowd ranking: sends per channel results (never account details) to the SignalPilot server.
Only these fields leave the computer: channel id, title, username, trades, wins, losses, net R (and how many
trades had a stop loss so R is known), net pips, profit as a percent, first and last trade date, and per symbol the summed price move and trade count
(so the server can work out pips with its own pip table).
From 1.3.17 the upload also holds one record per signal ("signals"): channel id, signal id (channel id and Telegram
message number), symbol, buy or sell, entry and exit price, pips, outcome and times, marked source "copied" (real trades
from MT5 history) or "tracked" (followed on paper for channels that are not copied). The server uses the signal id to
count every signal once across all customers. Older servers ignore the extra key.
From 1.3.20 every signal record also carries its original stop loss ("sl") and its result in R ("r": the price move
divided by the distance from entry to the stop loss), so the website ranks channels in R instead of pips."""
import json, os, time, urllib.request
from datetime import datetime
from . import config as C, results as R, mt5hist as H, pips as P, __version__
from .tracker import signal_row

INTERVAL = 3600
FIRST_DELAY = 120     # first upload two minutes after copying starts, not an hour later
FIELDS = ("id", "title", "username", "trades", "wins", "losses", "net_r", "r_trades", "net_pips",
          "profit_pct", "first_trade", "last_trade", "pip_moves")


SITE_STATS = "https://signalpilot.vodsfire.co.za/api/channel-stats"


def endpoint(cfg):
    """Results upload address, next to the licence address. The built in licence address still points at the old
    Render app, whose results handler dropped pip_moves and per signal records (fixed there on 20 Nov 2026 by
    forwarding); from 1.3.19 an onrender.com licence address sends results straight to the SignalPilot site."""
    u = C.licence_url(cfg)
    if not u: return ""
    base = u.rsplit("/api/", 1)[0] if "/api/" in u else u.rstrip("/")
    if ".onrender.com" in base.lower(): return SITE_STATS
    return base + "/api/channel-stats"


def enabled(cfg):
    return bool(cfg.get("share_results", True)) and bool((cfg.get("licence_key") or "").strip()) and bool(endpoint(cfg))


def _g(d, k, default=0):
    return d.get(k, default) if isinstance(d, dict) else getattr(d, k, default)


def sl_map(store_data):
    """{ticket: signal stop loss} from remembered signals."""
    out = {}
    for rec in (store_data.get("signals") or {}).values():
        if rec.get("sl"):
            for t in rec.get("tickets") or []: out[int(t)] = float(rec["sl"])
    return out


def _pip(info):
    if not info: return None
    pt, dg = _g(info, "point", 0), _g(info, "digits", 0)
    return pt * 10 if dg in (3, 5) else pt


def channel_stats(deals, tickets, sls, names, usernames, balance, symbol_info):
    """Per channel aggregates over all closed copied trades."""
    pos = {}
    for d in deals:
        pid = int(_g(d, "position_id") or _g(d, "order"))
        p = pos.setdefault(pid, {"money": 0.0, "chat": None, "entry": None, "side": None, "symbol": None,
                                 "out_vol": 0.0, "out_px": 0.0, "first": None, "closed": 0})
        p["money"] += float(_g(d, "profit")) + float(_g(d, "swap")) + float(_g(d, "commission")) + float(_g(d, "fee"))
        if p["chat"] is None:
            p["chat"] = tickets.get(pid) or tickets.get(int(_g(d, "order"))) or tickets.get(int(_g(d, "ticket")))
        e = int(_g(d, "entry"))
        if e == 0:
            p["entry"] = float(_g(d, "price")); p["side"] = 1 if int(_g(d, "type")) == 0 else -1
            p["symbol"] = _g(d, "symbol", None); p["first"] = int(_g(d, "time"))
            p["tickets"] = {pid, int(_g(d, "order"))}
        elif e in R.OUT_ENTRIES:
            v = float(_g(d, "volume", 0) or 0)
            p["out_vol"] += v; p["out_px"] += float(_g(d, "price")) * v
            p["closed"] = max(p["closed"], int(_g(d, "time")))
    rows = {}
    for pid, p in pos.items():
        if not p["closed"] or p["chat"] is None: continue
        c = rows.setdefault(str(p["chat"]), {"id": str(p["chat"]), "trades": 0, "wins": 0, "losses": 0, "net_r": 0.0,
                                            "r_trades": 0, "net_pips": 0.0, "money": 0.0, "first": None, "last": 0, "moves": {}})
        m = round(p["money"], 2)
        c["trades"] += 1; c["money"] += m
        if m > 0: c["wins"] += 1
        elif m < 0: c["losses"] += 1
        c["last"] = max(c["last"], p["closed"])
        f = p["first"] or p["closed"]; c["first"] = f if c["first"] is None else min(c["first"], f)
        if p["entry"] is not None and p["out_vol"] > 0:
            move = p["side"] * (p["out_px"] / p["out_vol"] - p["entry"])
            pip = _pip(symbol_info(p["symbol"])) if p["symbol"] else None
            if pip: c["net_pips"] += move / pip
            if p["symbol"]:
                mv = c["moves"].setdefault(str(p["symbol"]), [0.0, 0]); mv[0] += move; mv[1] += 1
            sl = next((sls[t] for t in p.get("tickets", ()) if t in sls), None)
            if sl and abs(p["entry"] - sl) > 0:
                c["net_r"] += move / abs(p["entry"] - sl); c["r_trades"] += 1
    out = []
    day = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else None
    for c in rows.values():
        n = R.apply_names([{"channel": c["id"]}], names)[0]
        out.append({"id": c["id"], "title": None if n["unresolved"] else n["name"],
                    "username": usernames.get(c["id"]), "trades": c["trades"], "wins": c["wins"], "losses": c["losses"],
                    "net_r": round(c["net_r"], 2), "r_trades": c["r_trades"], "net_pips": round(c["net_pips"], 1),
                    "profit_pct": round(100.0 * c["money"] / balance, 2) if balance else None,
                    "first_trade": day(c["first"]), "last_trade": day(c["last"]),
                    "pip_moves": {k: [round(v[0], 6), v[1]] for k, v in c["moves"].items()}})
    return out


def sl_by_signal(store_data):
    """{'chat:msg': signal stop loss} from remembered signals (used for R from 1.3.20)."""
    out = {}
    for key, rec in (store_data.get("signals") or {}).items():
        try:
            if rec.get("sl"): out[str(key)] = float(rec["sl"])
        except (TypeError, ValueError):
            pass
    return out


def signal_map(store_data):
    """{ticket: 'chat:msg'} for copied trades (permanent map plus every remembered signal)."""
    out = {int(t): str(k) for t, k in (store_data.get("ticket_signals") or {}).items()}
    for key, rec in (store_data.get("signals") or {}).items():
        for t in rec.get("tickets") or []: out.setdefault(int(t), key)
    return out


def copied_signals(deals, ticket_signals, offset=0, since=0, sl_by_key=None):
    """One record per copied signal whose trades are all closed. A signal split over several trades (one per take
    profit) counts once: entry and exit are the volume weighted averages of its trades, so its pips are what the
    copied signal made as a whole. Deal times are MT5 server time; offset (server minus UTC) turns them into UTC."""
    pos = {}
    for d in deals:
        pid = int(_g(d, "position_id") or _g(d, "order"))
        p = pos.setdefault(pid, {"key": None, "in_vol": 0.0, "in_px": 0.0, "out_vol": 0.0, "out_px": 0.0, "side": None,
                                 "symbol": None, "open": None, "closed": 0})
        if p["key"] is None:
            p["key"] = ticket_signals.get(pid) or ticket_signals.get(int(_g(d, "order"))) or ticket_signals.get(int(_g(d, "ticket")))
        e, v = int(_g(d, "entry")), float(_g(d, "volume", 0) or 0)
        if e == 0:
            p["in_vol"] += v; p["in_px"] += float(_g(d, "price")) * v; p["side"] = 1 if int(_g(d, "type")) == 0 else -1
            p["symbol"] = _g(d, "symbol", None); p["open"] = int(_g(d, "time"))
        elif e in R.OUT_ENTRIES:
            p["out_vol"] += v; p["out_px"] += float(_g(d, "price")) * v; p["closed"] = max(p["closed"], int(_g(d, "time")))
    sigs = {}
    for p in pos.values():
        if p["key"]: sigs.setdefault(p["key"], []).append(p)
    out = []
    for key, ps in sigs.items():
        if any(not p["closed"] or p["out_vol"] <= 0 or p["in_vol"] <= 0 for p in ps): continue     # still running
        if len({p["side"] for p in ps}) != 1 or len({p["symbol"] for p in ps}) != 1: continue
        iv, ov = sum(p["in_vol"] for p in ps), sum(p["out_vol"] for p in ps)
        entry, exit_ = sum(p["in_px"] for p in ps) / iv, sum(p["out_px"] for p in ps) / ov
        closed = max(p["closed"] for p in ps) - offset
        if since and closed < since: continue
        chat, msg = key.rsplit(":", 1)
        side = "BUY" if ps[0]["side"] == 1 else "SELL"
        pp = P.pips(ps[0]["symbol"], side, entry, exit_)
        rec = {"chat": chat, "msg_id": int(msg), "broker": ps[0]["symbol"], "side": side, "fill": round(entry, 6),
               "exit": round(exit_, 6), "pips": pp, "outcome": "closed", "created": min(p["open"] for p in ps) - offset,
               "sl": (sl_by_key or {}).get(key)}
        out.append(signal_row(rec, rec["created"], closed, source="copied"))
    return sorted(out, key=lambda r: r["close_time"] or "", reverse=True)


def collect(cfg, mt5, store_data, log=None):
    """Reads MT5 history (MT5 already connected, caller holds the MT5 lock). Returns the list to send."""
    return collect_all(cfg, mt5, store_data, log)[0]


def collect_all(cfg, mt5, store_data, log=None, offset=0):
    """(channel rows, copied per signal records), from one MT5 history read."""
    tickets = R.ticket_map(store_data)
    deals = H.deals(mt5, None, time.time() + 86400, positions=tickets.keys(), log=log)
    ai = mt5.account_info()
    balance = float(getattr(ai, "balance", 0) or 0)       # used only to work out the percent, never sent
    rows = channel_stats(deals, tickets, sl_map(store_data), R.known_names(cfg, store_data),
                         dict(store_data.get("usernames") or {}), balance, mt5.symbol_info)
    sigs = copied_signals(deals, signal_map(store_data), offset, since=time.time() - 30 * 86400, sl_by_key=sl_by_signal(store_data))[:500]
    return rows, sigs


def payload(cfg, channels, signals=None):
    # app_version is sent on every upload from 1.3.19 (before it was only sent with signals), so the server can
    # tell an upload from an old copy that is still running (no pip_moves, no signals) from a current one.
    out = {"key": (cfg.get("licence_key") or "").strip(), "product": "tg-copier", "app_version": __version__,
           "channels": [{k: c.get(k) for k in FIELDS} for c in channels]}
    if signals:
        out["signals"] = list(signals)
    return out



SEND_TIMEOUT = 180
SHARE_STATE = "share_state.json"
SEND_RETRIES = 1   # one retry after the first attempt


def _share_state_path():
    return C.path(SHARE_STATE)


def read_share_state():
    try:
        with open(_share_state_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_share_state(d):
    try:
        os.makedirs(C.data_dir(), exist_ok=True)
        path = _share_state_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except Exception:
        pass


def signal_digest(sig):
    return json.dumps(sig, sort_keys=True, default=str)


def new_or_changed(signals, state=None):
    """Signals whose id is new or whose payload changed since the last successful upload."""
    sent = (state or {}).get("sent") or {}
    out = []
    for s in signals or []:
        sid = str((s or {}).get("signal_id") or "")
        if not sid or sent.get(sid) != signal_digest(s):
            out.append(s)
    return out


def remember_sent(signals, state=None):
    state = dict(state or {})
    sent = dict(state.get("sent") or {})
    for s in signals or []:
        sid = str((s or {}).get("signal_id") or "")
        if not sid:
            continue
        sent.pop(sid, None)
        sent[sid] = signal_digest(s)
    while len(sent) > 6000:
        sent.pop(next(iter(sent)))
    state["sent"] = sent
    state["last_ok"] = int(time.time())
    return state


def assert_signals_carry_pips(signals):
    """Every per-signal record in an upload must include a pips field (a number, or null when the pip size is unknown)."""
    missing = []
    for i, s in enumerate(signals or []):
        if not isinstance(s, dict) or "pips" not in s:
            missing.append(i)
    return missing


def first_due(now, interval=INTERVAL, delay=FIRST_DELAY):
    """Value for the 'last shared' clock so the first upload happens `delay` seconds after start."""
    return now - interval + delay


def send(cfg, channels, opener=urllib.request.urlopen, timeout=SEND_TIMEOUT, log=None, signals=None):
    """Posts the results. Returns True on success. Never raises. Sends nothing when sharing is off.
    From 1.3.18 the timeout is 180 seconds with one retry, and only new or changed signals are sent
    (channel totals are still sent every time). log(msg), when given, receives one line saying whether
    the upload worked and why not."""
    say = log or (lambda m: None)
    if not enabled(cfg) or not (channels or signals): return False
    state = read_share_state()
    to_send = new_or_changed(signals, state)
    if not (channels or to_send): return False
    body = payload(cfg, channels, to_send)
    last_err = None
    ok = False
    attempts = 1 + SEND_RETRIES
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(endpoint(cfg), data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with opener(req, timeout=timeout) as r:
                status = getattr(r, "status", 200)
                ok = 200 <= status < 300
                if not ok:
                    last_err = f"server answered {status}"
                    # retry once on a temporary server error; do not retry a 4xx answer
                    if 400 <= status < 500:
                        break
                    continue
                break
        except Exception as e:
            last_err = str(e)
            ok = False
            continue
    extra = f" and {len(to_send)} signal(s)" if to_send else ""
    if ok:
        write_share_state(remember_sent(to_send, state))
        say(f"Shared results for {len(channels)} channel(s){extra} with SignalPilot (app {__version__}).")
        return True
    say(f"Sharing channel results failed: {last_err}" if last_err and not last_err.startswith("server")
        else f"Sharing channel results failed ({last_err or 'unknown error'}).")
    return False
