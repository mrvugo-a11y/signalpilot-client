"""Crowd ranking: sends per channel results (never account details) to the SignalPilot server.
Only these fields leave the computer: channel id, title, username, trades, wins, losses, net R (and how many
trades had a stop loss so R is known), net pips, profit as a percent, first and last trade date, and per symbol the summed price move and trade count
(so the server can work out pips with its own pip table)."""
import json, time, urllib.request
from datetime import datetime
from . import config as C, results as R, mt5hist as H

INTERVAL = 3600
FIRST_DELAY = 120     # first upload two minutes after copying starts, not an hour later
FIELDS = ("id", "title", "username", "trades", "wins", "losses", "net_r", "r_trades", "net_pips",
          "profit_pct", "first_trade", "last_trade", "pip_moves")


def endpoint(cfg):
    u = C.licence_url(cfg)
    if not u: return ""
    base = u.rsplit("/api/", 1)[0] if "/api/" in u else u.rstrip("/")
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


def collect(cfg, mt5, store_data, log=None):
    """Reads MT5 history (MT5 already connected, caller holds the MT5 lock). Returns the list to send."""
    tickets = R.ticket_map(store_data)
    deals = H.deals(mt5, None, time.time() + 86400, positions=tickets.keys(), log=log)
    ai = mt5.account_info()
    balance = float(getattr(ai, "balance", 0) or 0)       # used only to work out the percent, never sent
    return channel_stats(deals, tickets, sl_map(store_data), R.known_names(cfg, store_data),
                         dict(store_data.get("usernames") or {}), balance, mt5.symbol_info)


def payload(cfg, channels):
    return {"key": (cfg.get("licence_key") or "").strip(), "product": "tg-copier",
            "channels": [{k: c.get(k) for k in FIELDS} for c in channels]}


def first_due(now, interval=INTERVAL, delay=FIRST_DELAY):
    """Value for the 'last shared' clock so the first upload happens `delay` seconds after start."""
    return now - interval + delay


def send(cfg, channels, opener=urllib.request.urlopen, timeout=60, log=None):
    """Posts the results. Returns True on success. Never raises. Sends nothing when sharing is off.
    log(msg), when given, receives one line saying whether the upload worked and why not."""
    say = log or (lambda m: None)
    if not enabled(cfg) or not channels: return False
    try:
        req = urllib.request.Request(endpoint(cfg), data=json.dumps(payload(cfg, channels)).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with opener(req, timeout=timeout) as r:
            ok = 200 <= getattr(r, "status", 200) < 300
        say(f"Shared results for {len(channels)} channel(s) with SignalPilot." if ok
            else f"Sharing channel results failed (server answered {getattr(r, 'status', '?')}).")
        return ok
    except Exception as e:
        say(f"Sharing channel results failed: {e}")
        return False
