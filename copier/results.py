"""Channel results: profit and loss of closed trades, grouped by the Telegram channel that sent the signal."""
import time
from . import config as C

PERIODS = (("Today", "today"), ("7 days", 7), ("30 days", 30), ("All", None))
OUT_ENTRIES = (1, 2, 3)      # DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY
UNKNOWN = "Not tagged"


def unknown_name(chat):
    return f"Unknown channel ({chat})"


def known_names(cfg, store_data):
    """Titles from the trade store (also removed channels) and the current channel list, plus the
    same id written with or without the -100 prefix."""
    names = {}
    for k, v in ((store_data or {}).get("names") or {}).items():
        if v: names[str(k)] = v
    for k, v in ((cfg or {}).get("channels") or {}).items():
        n = (v or {}).get("name")
        if n and n != str(k): names[str(k)] = n
    for k, v in list(names.items()):
        for alt in id_variants(k): names.setdefault(alt, v)
    return names


def id_variants(chat):
    s = str(chat)
    if s.startswith("-100"): return [s[4:], "-" + s[4:]]
    if s.startswith("-"): return ["-100" + s[1:]]
    if s.isdigit(): return ["-100" + s, "-" + s]
    return []


def missing_names(rows):
    """Channel ids of rows still showing a fallback instead of a title."""
    return [r["channel"] for r in rows if r["channel"] != UNKNOWN and r.get("unresolved")]


def apply_names(rows, names):
    for r in rows:
        if r["channel"] == UNKNOWN: continue
        n = names.get(str(r["channel"]))
        if not n:
            for alt in id_variants(r["channel"]):
                if names.get(alt): n = names[alt]; break
        r["name"] = n or unknown_name(r["channel"]); r["unresolved"] = not n
    return rows


def period_start(period, now=None):
    now = time.time() if now is None else now
    if period is None: return 0
    if period == "today":
        t = time.localtime(now); return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return now - int(period) * 86400


def ticket_map(store_data):
    """{ticket: chat_id} from the trade store: the permanent map plus every remembered signal."""
    out = {int(t): str(c) for t, c in (store_data.get("tickets") or {}).items()}
    for key, rec in (store_data.get("signals") or {}).items():
        chat = key.split(":", 1)[0]
        for t in rec.get("tickets") or []: out.setdefault(int(t), chat)
    return out


def backfill_names(cfg, rows, resolver, store=None):
    """Looks up titles for rows that still show a fallback, remembers them, and updates the rows.
    resolver(ids) -> {id: title}. Returns True if any row changed."""
    ids = missing_names(rows)
    if not ids: return False
    try: found = resolver(ids) or {}
    except Exception: found = {}
    found = {str(k): v for k, v in found.items() if v}
    if not found: return False
    if store is not None:
        try: store.set_names(found)
        except Exception: pass
    apply_names(rows, found if not store else dict(known_names(cfg, store.data), **found))
    return True


def _g(d, k, default=0):
    return d.get(k, default) if isinstance(d, dict) else getattr(d, k, default)


def aggregate(deals, tickets, names=None, since=0, magic=None, include_untagged=False):
    """deals: MT5 history deals (objects or dicts). A trade is one position; its result is
    profit + swap + commission + fee over all its deals. A trade counts once it has a closing deal,
    in the period of that closing deal. Returns rows sorted by net, best first."""
    names = names or {}
    pos = {}
    for d in deals:
        pid = int(_g(d, "position_id") or _g(d, "order"))
        p = pos.setdefault(pid, {"money": 0.0, "closed": 0, "chat": None, "magic": None})
        p["money"] += float(_g(d, "profit")) + float(_g(d, "swap")) + float(_g(d, "commission")) + float(_g(d, "fee"))
        if p["chat"] is None:
            p["chat"] = tickets.get(pid) or tickets.get(int(_g(d, "order"))) or tickets.get(int(_g(d, "ticket")))
        if int(_g(d, "entry")) in OUT_ENTRIES: p["closed"] = max(p["closed"], int(_g(d, "time")))
        if int(_g(d, "entry")) == 0: p["magic"] = _g(d, "magic")
    rows = {}
    for p in pos.values():
        if not p["closed"] or p["closed"] < since: continue
        chat = p["chat"]
        if chat is None:
            if not include_untagged or (magic is not None and p["magic"] != magic): continue
            chat = UNKNOWN
        r = rows.setdefault(chat, {"channel": chat, "name": UNKNOWN,
                                   "profit": 0.0, "loss": 0.0, "net": 0.0, "trades": 0, "wins": 0})
        m = round(p["money"], 2)
        r["trades"] += 1; r["net"] += m
        if m > 0: r["profit"] += m; r["wins"] += 1
        elif m < 0: r["loss"] += m
    out = []
    for r in rows.values():
        r["net"] = round(r["net"], 2); r["profit"] = round(r["profit"], 2); r["loss"] = round(r["loss"], 2)
        r["win_rate"] = round(100.0 * r["wins"] / r["trades"], 1) if r["trades"] else 0.0
        out.append(r)
    apply_names(out, names)
    return sorted(out, key=lambda r: r["net"], reverse=True)


def fetch(cfg, period, mt5=None, store_data=None, now=None):
    """Reads MT5 history and returns (rows, currency). MT5 must already be initialised."""
    if mt5 is None:
        from .executor import mt5
    if mt5 is None: raise RuntimeError("MetaTrader 5 is not available on this computer.")
    if store_data is None:
        from .store import Store
        store_data = Store().data
    now = time.time() if now is None else now
    since = period_start(period, now)
    from datetime import datetime
    # opening deals can be older than the period, so read a wider window for matching
    from . import mt5hist as H
    tm = ticket_map(store_data)
    deals = H.deals(mt5, since - 90 * 86400 if since else None, now + 86400, positions=tm.keys())
    names = known_names(cfg, store_data)
    ai = mt5.account_info()
    magic = int(cfg.get("magic_number", 0) or 0) or None
    rows = aggregate(deals, tm, names, since, magic, include_untagged=magic is not None)
    from . import share as SH
    from .tracker import Tracker
    tr = Tracker()
    copied = SH.copied_signals(deals, SH.signal_map(store_data), int(tr.data.get("offset") or 0), since)
    add_pips(rows, copied, tr.channel_rows(since, names))
    return rows, getattr(ai, "currency", "") if ai else ""


def add_pips(rows, copied_signals, tracked_rows):
    """Adds a pips figure to every copied channel row (once per signal: a signal split over several trades counts
    once, at its average entry and exit), and appends channels tracked on paper (source 'tracked', no money)."""
    per = {}
    for s in copied_signals:
        if s.get("pips") is None: continue
        c = per.setdefault(str(s["channel"]), [0.0, 0]); c[0] += float(s["pips"]); c[1] += 1
    for r in rows:
        r.setdefault("source", "copied")
        p = per.get(str(r["channel"]))
        r["pips"] = round(p[0], 1) if p else None; r["signals"] = p[1] if p else 0
    have = {str(r["channel"]) for r in rows}
    for t in tracked_rows:
        if str(t["channel"]) in have: continue      # a channel copied in this period shows its real trades
        rows.append({"channel": t["channel"], "name": t["name"], "profit": None, "loss": None, "net": None,
                     "trades": t["trades"], "wins": t["wins"], "win_rate": t["win_rate"], "pips": t["pips"],
                     "signals": t["trades"], "source": "tracked"})
    return rows
