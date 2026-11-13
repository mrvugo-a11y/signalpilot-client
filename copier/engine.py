"""Telegram listener: ties the parser, the ticket store and the MT5 executor together."""
import asyncio, datetime, os, threading, time
from . import config as C
from .parser import (parse, parse_command, parse_update, parse_signal, parse_bare_call, is_status, is_ru_info,
                     normalise, SIDE_RE, Signal, Command)
from .store import Store
from .executor import Executor
from . import share as SH
from . import tracker as TR

TRACK_EVERY = 60          # seconds between paper tracker price checks


def session_path():
    return C.path("telegram")


# Exactly ONE TelegramClient per process, living on ONE asyncio loop in ONE dedicated thread
# (TelegramService). The setup wizard, the channel list and the copier all reuse it through
# run_coroutine_threadsafe. The login is kept as a StringSession in memory and saved to
# "telegram.string", so no SQLite file is held open at all (no more "database is locked").
# An existing "telegram.session" (SQLite) login is migrated once, read from a copy, without re-login.
LOCKED_RETRIES = (0.5, 1, 2, 3)

LOCKED_MESSAGE = ("Your Telegram login file is busy (database is locked). Close any other SignalPilot window, "
                  "wait a few seconds and try again. If it keeps happening, restart your computer.")


def is_db_locked(e):
    return "database is locked" in str(e).lower()


def friendly_error(e):
    return LOCKED_MESSAGE if is_db_locked(e) else str(e)


def string_path():
    return C.path("telegram.string")


def load_session_string():
    """The saved login as a StringSession string. Migrates an old telegram.session file once."""
    try:
        with open(string_path(), encoding="utf-8") as f:
            s = f.read().strip()
        if s: return s
    except OSError:
        pass
    s = migrate_sqlite_session(session_path() + ".session")
    if s:
        try: save_session_string(s)
        except OSError: pass
    return s


def save_session_string(s):
    if not s: return
    tmp = string_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: f.write(s)
    os.replace(tmp, string_path())


def migrate_sqlite_session(file):
    """Reads dc + auth key out of a Telethon .session (SQLite) file, from a temporary copy so a lock
    on the original cannot block us. Returns a StringSession string, or "" when there is no login."""
    if not os.path.exists(file): return ""
    import sqlite3, shutil, tempfile
    d = tempfile.mkdtemp()
    try:
        cp = os.path.join(d, "s.session"); shutil.copyfile(file, cp)
        con = sqlite3.connect(cp)
        try: row = con.execute("select dc_id, server_address, port, auth_key from sessions").fetchone()
        finally: con.close()
    except Exception:
        return ""
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if not row or not row[3]: return ""
    from telethon.sessions import StringSession
    from telethon.crypto import AuthKey
    ss = StringSession(); ss.set_dc(row[0], row[1], row[2]); ss.auth_key = AuthKey(data=row[3])
    return ss.save()


def make_client(cfg):
    """Only ever called by TelegramService, once per process."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id, api_hash = C.api_credentials(cfg)
    if not (api_id and api_hash):
        raise RuntimeError("Telegram app details are missing. Open Settings and run the setup again.")
    return TelegramClient(StringSession(load_session_string()), api_id, api_hash)


async def _retry_locked(fn):
    """Runs fn() (sync or async), retrying a few times with a short backoff on 'database is locked'."""
    for delay in LOCKED_RETRIES + (None,):
        try:
            r = fn()
            return (await r) if asyncio.iscoroutine(r) else r
        except Exception as e:
            if delay is None or not is_db_locked(e): raise
            await asyncio.sleep(delay)


class TelegramService:
    """Owns the single client and the single loop thread. Use service() to get it."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="telegram", daemon=True)
        self.thread.start()
        self.client = None; self.created = 0

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, timeout=None):
        """Run a coroutine on the Telegram loop from any other thread and wait for the result."""
        if threading.current_thread() is self.thread:
            raise RuntimeError("TelegramService.run called from the Telegram thread")
        return self.submit(coro).result(timeout)

    async def get_client(self, cfg):
        """The one client, created on first use and (re)connected if needed. Runs on the loop."""
        if self.client is None:
            self.client = make_client(cfg); self.created += 1
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    def save(self):
        try:
            if self.client is not None: save_session_string(self.client.session.save())
        except Exception:
            pass

    def shutdown(self):
        if self.client is not None:
            try: self.run(self.client.disconnect(), 10)
            except Exception: pass
            self.save()
        self.loop.call_soon_threadsafe(self.loop.stop)


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None: _SERVICE = TelegramService()
        return _SERVICE


def reset_service():
    """Tests only."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None: _SERVICE.loop.call_soon_threadsafe(_SERVICE.loop.stop)
        _SERVICE = None


async def login(client, cfg, ask, phone=None):
    """Logs in, asking for the code and the two step password in dialogs when needed."""
    await _retry_locked(lambda: client.start(phone=lambda: phone or cfg.get("telegram_phone") or ask("Your Telegram phone number, with country code (for example +27821234567):", False),
                       code_callback=lambda: ask("Telegram just sent you a login code inside the Telegram app.\nType it here:", False),
                       password=lambda: ask("Your Telegram account has a two step password.\nType it here:", True)))
    service().save()


async def _login_and_list(cfg, ask, phone=None):
    client = await service().get_client(cfg)
    await login(client, cfg, ask, phone)
    me = await client.get_me()
    out = []
    async for d in client.iter_dialogs():
        if d.is_channel or d.is_group:
            out.append((d.id, d.name))
    name = " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x)
    return name, out


def login_and_list(cfg, ask, phone=None, timeout=None):
    """Blocking (call from a worker thread). Logs in on the shared client and returns (my_name, [(id, name), ...])."""
    return service().run(_login_and_list(cfg, ask, phone), timeout)


def safe_list_channels(cfg, ask, log, timeout=300):
    """Channel list for the Channels button. Works while copying (shared client). Never raises: logs and returns None."""
    try:
        return login_and_list(cfg, ask, timeout=timeout)[1]
    except BaseException as e:
        try: log(f"Could not load channels: {friendly_error(e) or type(e).__name__}")
        except Exception: pass
        return None


def _title(ent):
    t = getattr(ent, "title", None)
    if not t:
        t = " ".join(x for x in (getattr(ent, "first_name", None), getattr(ent, "last_name", None)) if x)
    if t: return t
    u = getattr(ent, "username", None)
    return f"@{u}" if u else None


async def _resolve_titles(cfg, ids, usernames=None):
    client = await service().get_client(cfg)
    if not await client.is_user_authorized(): return {}
    out, missing = {}, []
    for i in ids:
        try:
            ent = await client.get_entity(int(i))
            if usernames is not None and getattr(ent, "username", None): usernames[str(i)] = ent.username
            t = _title(ent)
            if t: out[str(i)] = t
            else: missing.append(str(i))
        except Exception:
            missing.append(str(i))
    if missing:                                    # not in the session cache: look through the dialog list
        want = set(missing)
        async for d in client.iter_dialogs():
            k = str(d.id)
            if k in want:
                if usernames is not None and getattr(d.entity, "username", None): usernames[k] = d.entity.username
                t = d.name or _title(d.entity)
                if t: out[k] = t
                want.discard(k)
                if not want: break
    return out


def resolve_titles(cfg, ids, timeout=60, usernames=None):
    """Blocking (worker thread only). {chat_id: title} for the ids Telegram knows. Never asks to log in, never raises.
    Pass a dict as usernames to also collect channel usernames."""
    ids = [str(i) for i in ids if str(i).lstrip("-").isdigit()]
    if not ids: return {}
    try:
        return service().run(_resolve_titles(cfg, ids, usernames), timeout) or {}
    except BaseException:
        return {}


def list_channels(cfg, ask):
    return login_and_list(cfg, ask)[1]


def _cross_chat(msg):
    """Reply to a message in another chat (Telegram cross-chat reply / quote from another channel)."""
    r = getattr(msg, "reply_to", None)
    return bool(getattr(msg, "reply_to_peer_id", None) or (r is not None and getattr(r, "reply_to_peer_id", None)))


def _strip_quote(msg, text):
    """Drop the quoted part of the parent if the client rendered it into the text."""
    r = getattr(msg, "reply_to", None)
    q = getattr(r, "quote_text", None) if r is not None else None
    if q and q in text:
        text = text.replace(q, " ")
    return text


def _reply_may_open(msg, sig):
    """A reply opens a trade only when it is its own complete signal (entry price or explicit side,
    plus SL/TP), is not a status/result, and does not point at another chat."""
    if _cross_chat(msg): return False
    return sig.sl is not None or bool(sig.tps)


def message_time(msg, edited=False):
    """Unix time the message was posted in the watched chat (for an edit: when it was edited).
    For a forwarded message this is the time it was forwarded into the channel, not the original post time
    (Telegram keeps that one separately in fwd_from). None when unknown."""
    d = (getattr(msg, "edit_date", None) if edited else None) or getattr(msg, "date", None)
    if not isinstance(d, datetime.datetime): return None
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def _minutes(sec):
    m = int(sec // 60)
    return "1 minute" if m == 1 else f"{m} minutes"


def _tracked(eng, chat):
    """True for a followed channel that is tracked on paper instead of copied."""
    return str(chat) in (getattr(eng, "tracked", None) or {})


class Engine:
    def __init__(self, log, ask, status=lambda kind, ok, text: None):
        self.log, self.ask, self.status = log, ask, status
        self.loop = None; self.client = None; self.thread = None
        self.mt5_lock = threading.Lock()

    # -- lifecycle --
    def start(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True); self.thread.start()

    def stop(self):
        """Stops copying. The shared Telegram client stays connected so the next Start or channel list just works."""
        ev = getattr(self, "stop_event", None)
        if ev: ev.set()

    def reload_settings(self, cfg):
        """Apply new trade settings straight away (no restart). Channel list changes need a restart."""
        self.cfg = cfg
        if getattr(self, "ex", None): self.ex.cfg = cfg

    def channel_ids(self):
        """What the running copier listens to; a change means a restart. Copied channels, channels kept for
        tracking only, and whether every other followed channel is tracked."""
        if not getattr(self, "cfg", None): return []
        chans = self.cfg.get("channels", {})
        out = sorted(k for k, v in chans.items() if v.get("enabled", True))
        out += sorted("track:" + k for k, v in chans.items() if not v.get("enabled", True) and v.get("track", True))
        out += sorted("notrack:" + k for k, v in chans.items() if v.get("track", True) is False)
        if self.cfg.get("track_followed", True): out.append("track_followed")
        return out

    # -- paper tracking of followed channels that are not copied (never sends orders) --
    def _tracking(self, chat):
        return str(chat) in getattr(self, "tracked", {})

    def _rec(self, chat, msg):
        """The copied trades or the paper tracker that belongs to a message."""
        if msg is None: return None
        return self.tracker.get(chat, msg) if _tracked(self, chat) else self.store.get(chat, msg)

    def _last(self, chat):
        return self.tracker.last_id(chat) if _tracked(self, chat) else self.store.last_id(chat)

    def _market(self):
        from .executor import mt5
        if mt5 is None: return None
        return TR.Market(mt5, lambda sym, st: self.ex.resolve_symbol(sym, st))

    def _track_open(self, chat_id, msg_id, sig, st, parent_id=None, posted=None):
        tid = getattr(sig, "trade_id", None)
        if tid and self.tracker.has_trade(chat_id, tid):
            self.log(f"[{self._name(chat_id)}] trade #{tid} is already tracked, ignoring repost"); return
        try:
            with self.mt5_lock:
                rec = self.tracker.add(self._market(), chat_id, msg_id, sig, st, self._name(chat_id), posted)
        except Exception as e:
            self.log(f"[{self._name(chat_id)}] Could not start pip tracking: {e}"); return
        if not rec: return
        if parent_id: self.tracker.alias(chat_id, parent_id, msg_id)
        if rec["state"] == "done":
            self.log(f"[{self._name(chat_id)}] Not tracked: {TR.OUTCOME_TEXT.get(rec['outcome'], rec['outcome'])}.")
        else:
            how = f"from {rec['fill']}" if rec["state"] == "open" else f"once the price reaches {rec['pending_price']}"
            self.log(f"[{self._name(chat_id)}] Tracking pips for {rec['side']} {rec['broker']} {how}. "
                     f"Copying is off for this channel, so no trade is opened.")

    def _track_update(self):
        try:
            if not self.tracker.active(): return
            with self.mt5_lock:
                before = {r["key"]: r.get("outcome") for r in self.tracker.active()}
                self.tracker.update(self._market())
            for k, old in before.items():
                r = self.tracker.data["signals"].get(k)
                if r and r.get("outcome") in TR.COUNTED and old != r["outcome"]:
                    self.log(f"[{self._name(r['chat'])}] Tracked {r['side']} {r['broker']}: {TR.OUTCOME_TEXT[r['outcome']]}, "
                             f"{r['pips']:+.1f} pips (tracked, not copied)" if r.get("pips") is not None else
                             f"[{self._name(r['chat'])}] Tracked {r['side']} {r['broker']}: {TR.OUTCOME_TEXT[r['outcome']]}")
        except Exception as e:
            self.log(f"Pip tracking check failed: {e}")

    async def _followed_channels(self, client, copied):
        """Every channel and group the customer follows on Telegram that is not copied (and not switched off)."""
        out = {}
        chans = self.cfg.get("channels", {})
        for k, v in chans.items():
            if not v.get("enabled", True) and v.get("track", True): out[str(k)] = v.get("name") or str(k)
        if self.cfg.get("track_followed", True):
            try:
                async for d in client.iter_dialogs():
                    k = str(d.id)
                    if not getattr(d, "is_channel", False) or k in copied: continue
                    if chans.get(k, {}).get("track", True) is False: continue
                    out.setdefault(k, d.name or k)
            except Exception as e:
                self.log(f"Could not list your followed channels for pip tracking: {friendly_error(e)}")
        return out

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def _run(self):
        try:
            self._main()
        except Exception as e:
            self.log(f"Problem: {friendly_error(e)}")
        finally:
            try: self.ex.shutdown()
            except Exception: pass
            self.status("telegram", None, "Not connected"); self.status("mt5", None, "Not connected")
            self.log("Copying stopped.")

    def _main(self):
        self.cfg = C.load()
        chans = {k: v for k, v in self.cfg.get("channels", {}).items() if v.get("enabled", True)}
        some_tracked = any(not v.get("enabled", True) and v.get("track", True) for v in self.cfg.get("channels", {}).values())
        if not chans and not some_tracked and not self.cfg.get("track_followed", True):
            raise RuntimeError("No channels are ticked. Click Channels and tick at least one.")
        self.store = Store()
        self.tracker = TR.Tracker(log=self.log); self.tracked = {}
        try:                                       # remember every known channel title for Channel results
            self.store.set_names({k: v.get("name") for k, v in self.cfg.get("channels", {}).items()
                                  if v.get("name") and v.get("name") != str(k)})
        except Exception: pass
        self.ex = Executor(self.cfg, self.log)
        try:
            self.ex.connect()
        except Exception as e:
            self.status("mt5", False, "Not connected"); raise RuntimeError(f"Could not connect to MetaTrader 5. Make sure it is open and logged in. ({e})")
        self.status("mt5", True, "Connected")
        if self.stop_event.is_set(): return
        from telethon import events
        svc = service(); handlers = []

        async def attach():
            client = await svc.get_client(self.cfg)
            await login(client, self.cfg, self.ask)
            self.tracked = await self._followed_channels(client, set(chans))
            ids = [int(k) for k in chans] + [int(k) for k in self.tracked]
            if ids:
                handlers.extend(((self._on_new, events.NewMessage(chats=ids)), (self._on_edit, events.MessageEdited(chats=ids))))
            for h, e in handlers: client.add_event_handler(h, e)
            return client

        async def detach(client):
            for h, e in handlers: client.remove_event_handler(h, e)

        self.client = svc.run(attach()); self.loop = svc.loop
        try:
            if not chans and not self.tracked:
                raise RuntimeError("No channels are ticked. Click Channels and tick at least one.")
            try: self.store.set_names({k: v for k, v in self.tracked.items() if v and v != k})
            except Exception: pass
            self.status("telegram", True, "Connected")
            if chans: self.log("Copying is ON. Watching: " + ", ".join(v.get("name", k) for k, v in chans.items()))
            else: self.log("Copying is OFF for every channel.")
            if self.tracked:
                self.log(f"Tracking pips on paper for {len(self.tracked)} followed channel(s) that are not copied. "
                         "No trades are opened for them.")
            self._last_share = SH.first_due(time.monotonic()); self._last_track = 0.0
            self._track_update()                       # catch up on trackers from before a restart
            while not self.stop_event.wait(5):
                self._check_break_even()
                self._expire_waiting()
                if time.monotonic() - self._last_track >= TRACK_EVERY:
                    self._last_track = time.monotonic(); self._track_update()
                if time.monotonic() - self._last_share >= SH.INTERVAL:
                    self._last_share = time.monotonic(); self._share_results()
                if not self.client.is_connected():      # network dropped: reconnect the same client
                    try: svc.run(svc.get_client(self.cfg), 60)
                    except Exception as e: self.log(f"Telegram reconnect failed, retrying: {friendly_error(e)}")
        finally:
            self._share_results(wait=True)         # on Stop as well (wait, the app may be closing)
            try: svc.run(detach(self.client), 10)
            except Exception: pass
            svc.save(); self.client = None

    # -- handlers --
    def _st(self, chat_id):
        return C.channel_settings(self.cfg, chat_id)

    def _name(self, chat_id):
        n = self.cfg["channels"].get(str(chat_id), {}).get("name")
        return n or getattr(self, "tracked", {}).get(str(chat_id)) or str(chat_id)

    # -- signals without a stop loss: wait for the same channel to send the numbers --
    def _wait_minutes(self, st):
        try: return max(0, float(st.get("wait_for_sl_minutes", 15) or 0))
        except (TypeError, ValueError): return 15

    def _remember(self, chat_id, msg_id, sig, st, posted=None):
        if not hasattr(self, "_waiting"): self._waiting = {}
        self._waiting[str(chat_id)] = {"sig": sig, "msg_id": msg_id, "at": posted or time.time(),
                                       "minutes": self._wait_minutes(st)}
        self.log(f"[{self._name(chat_id)}] Waiting for {self._name(chat_id)} to send the stop loss and take profit for "
                 f"{sig.side} {sig.symbol}. Nothing is {'tracked' if _tracked(self, chat_id) else 'opened'} until then "
                 f"(up to {self._wait_minutes(st):.0f} minutes).")

    def _waiting_for(self, chat_id):
        w = getattr(self, "_waiting", {}).get(str(chat_id))
        if w and time.time() - w["at"] > w["minutes"] * 60:
            self._waiting.pop(str(chat_id), None)
            self.log(f"[{self._name(chat_id)}] Stopped waiting: no stop loss came for {w['sig'].side} {w['sig'].symbol} "
                     f"within {w['minutes']:.0f} minutes, so it was not {'tracked' if _tracked(self, chat_id) else 'copied'}.")
            return None
        return w

    def _expire_waiting(self):
        for chat in list(getattr(self, "_waiting", {})): self._waiting_for(chat)

    def _complete(self, w, text):
        """The follow up message holds the numbers for a remembered call. Returns a Signal or None.
        At least a stop loss is needed; status and result messages never complete a call."""
        if not text or is_status(text): return None
        upd = parse_update(text)
        if not upd or upd.sl is None: return None
        ws = w["sig"]
        own_side = SIDE_RE.search(normalise(text))
        sig = parse_signal(f"{ws.symbol}\n{'' if own_side else ws.side}\n{text}")
        if not sig or sig.sl is None:
            sig = Signal(side=ws.side, symbol=ws.symbol, order_type=ws.order_type, entry=ws.entry,
                         entry_high=ws.entry_high, sl=upd.sl, tps=list(upd.tps))
        if not sig.tps and ws.tps: sig.tps = list(ws.tps)
        if sig.entry is None and ws.entry is not None: sig.entry, sig.entry_high = ws.entry, ws.entry_high
        return sig

    def _too_old(self, chat_id, msg, st, what, edited=False):
        """True (and logged) when the message was sent longer ago than the customer's limit."""
        try: lim = float(st.get("max_signal_age_minutes", 5) or 0)
        except (TypeError, ValueError): lim = 5
        t = message_time(msg, edited)
        if not lim or t is None: return False
        age = time.time() - t
        if age <= lim * 60: return False
        self.log(f"[{self._name(chat_id)}] Skipped an old {what} from {self._name(chat_id)} sent {_minutes(age)} ago. "
                 f"Your limit is {lim:.0f} minutes.")
        return True

    def _open(self, chat_id, msg_id, sig, st, parent_id=None, posted=None):
        if sig.sl is None and self._wait_minutes(st):
            self._remember(chat_id, msg_id, sig, st, posted); return
        getattr(self, "_waiting", {}).pop(str(chat_id), None)   # a full signal replaces any call we were waiting on
        if _tracked(self, chat_id):
            return self._track_open(chat_id, msg_id, sig, st, parent_id, posted)
        tid = getattr(sig, "trade_id", None)
        if tid and self.store.has_trade(chat_id, tid):
            self.log(f"[{self._name(chat_id)}] trade #{tid} was already copied, ignoring repost"); return
        with self.mt5_lock:
            tickets = self.ex.open_signal(sig, st)
        if tickets:
            self.store.put(chat_id, msg_id, {"symbol": sig.symbol, "side": sig.side, "sl": sig.sl,
                                             "tps": sig.tps, "tickets": tickets,
                                             "tp1": getattr(self.ex, "last_tp1", None),
                                             "channel_name": self._name(chat_id)})
            self.store.mark_trade(chat_id, tid, msg_id)
            if parent_id:      # opened from a reply: later replies to the parent message find these trades too
                self.store.alias(chat_id, parent_id, msg_id)

    async def _signal_from_reply(self, ev, chat_id, reply_id, text):
        """A reply (or an edited reply) that is itself the full signal: entry or zone, stop loss and take
        profits, while the short parent message ('XAUUSD BUY NOW') never opened a trade. The symbol and
        direction are taken from the parent when the reply leaves them out. Returns a Signal or None."""
        if self._rec(chat_id, reply_id): return None          # a trade (or tracker) exists: this is a modify
        upd = parse_update(text)
        if not upd or upd.sl is None or not upd.tps: return None   # not complete enough to be a new trade
        try:
            parent = await ev.message.get_reply_message()
        except Exception:
            parent = None
        ptext = (getattr(parent, "raw_text", None) or getattr(parent, "message", None) or "") if parent else ""
        if not ptext or is_ru_info(ptext): return None
        if parse_signal(ptext): return None   # the parent was a full signal on its own and was skipped on purpose
        return parse_signal(ptext + "\n" + text)

    def _log_signal(self, chat, s, prefix="SIGNAL received"):
        self.log(f"[{self._name(chat)}] {prefix}: {s.side} {s.symbol}"
                 f"{' ' + s.order_type if s.order_type and s.order_type.upper() != 'MARKET' else ''}, entry {s.entry or 'now'}"
                 f"{' to ' + str(s.entry_high) if s.entry_high else ''}, stop loss {s.sl or 'none'}, take profits {', '.join(map(str, s.tps)) or 'none'}")

    def _share_results(self, wait=False):
        """Crowd ranking: per channel results plus one record per signal (copied and tracked). Reads MT5 here,
        sends in the background. Never raises."""
        try:
            cfg = C.load()
            if not SH.enabled(cfg):
                if cfg.get("share_results", True) and not (cfg.get("licence_key") or "").strip():
                    self.log("Channel results not shared: no licence key entered.")
                return
            tr = getattr(self, "tracker", None)
            with self.mt5_lock:
                from .executor import mt5
                if mt5 is None: return
                off = int((tr.data.get("offset") if tr else 0) or 0)
                rows, copied = SH.collect_all(cfg, mt5, self.store.data, log=self.log, offset=off)
            tracked = tr.upload_rows() if tr else []
            signals = copied + tracked
            if not rows and not signals:
                self.log("Channel results not shared yet: no closed copied trades found in MT5 history."); return
            def bg():
                try:
                    un = {}
                    ids = sorted({r["id"] for r in rows} | {s["channel"] for s in signals})
                    titles = resolve_titles(cfg, ids, usernames=un)
                    if titles: self.store.set_names(titles)
                    if un: self.store.set_usernames(un)
                    for r in rows:
                        if titles.get(r["id"]) and not titles[r["id"]].startswith("@"): r["title"] = titles[r["id"]]
                        r["username"] = un.get(r["id"]) or r.get("username")
                    names = dict(self.store.data.get("names") or {})
                    for s in signals:
                        t = titles.get(s["channel"]) or names.get(s["channel"]) or self._name(s["channel"])
                        if t and t != s["channel"] and not t.startswith("@"): s["title"] = t
                        if un.get(s["channel"]): s["username"] = un[s["channel"]]
                    if SH.send(cfg, rows, log=self.log, signals=signals) and tr and tracked:
                        tr.mark_uploaded([s["signal_id"] for s in tracked])
                except Exception as e:
                    self.log(f"Sharing channel results failed: {e}")
            t = threading.Thread(target=bg, daemon=True); t.start()
            if wait: t.join(90)
        except Exception as e:
            try: self.log(f"Sharing channel results failed: {e}")
            except Exception: pass

    def _check_break_even(self):
        """Break even after TP1 (only when the customer ticked it for that channel)."""
        if not hasattr(self, "_be_warned"): self._be_warned = set()
        try:
            for key, rec in list(self.store.data.get("signals", {}).items()):
                chat_id, msg_id = key.rsplit(":", 1)
                if rec.get("be_retry"):                 # channel said 'move SL to BE' but the broker could not take it yet
                    with self.mt5_lock:
                        ps = [p for p in (self.ex._position(t) for t in rec["be_retry"]) if p]
                        left = self.ex.move_to_be_now(ps, self._be_warned)
                    self.store.update(chat_id, msg_id, be_retry=left)
                if rec.get("be_done") or not rec.get("tickets"): continue
                if not C.channel_settings(self.cfg, chat_id).get("be_after_tp1", False): continue
                with self.mt5_lock:
                    done = self.ex.check_break_even(rec, self._st(chat_id), self._be_warned)
                if done: self.store.update(chat_id, msg_id, be_done=True)
        except Exception as e:
            self.log(f"Break even check failed: {e}")

    def _apply(self, chat_id, msg_id, cmd, st):
        if _tracked(self, chat_id):
            try:
                if self.tracker.get(chat_id, msg_id) and self.tracker.apply(self._market(), chat_id, msg_id, cmd):
                    self.log(f"[{self._name(chat_id)}] tracked signal: {cmd.action.replace('_', ' ').lower()} noted (no trade, tracking only)")
            except Exception as e:
                self.log(f"[{self._name(chat_id)}] Could not update a tracked signal: {e}")
            return
        rec = self.store.get(chat_id, msg_id) if msg_id else None
        if not rec:
            self.log(f"[{self._name(chat_id)}] {cmd.action.replace('_', ' ').lower()}: no copied trades belong to that signal, nothing to do"); return
        self.log(f"[{self._name(chat_id)}] instruction {cmd.action.replace('_', ' ').lower()} for trades {', '.join(map(str, rec['tickets']))}")
        if not hasattr(self, "_be_warned"): self._be_warned = set()
        with self.mt5_lock:
            retry = self.ex.apply(cmd, rec["tickets"], st, self._be_warned) or []
        if retry: self.store.update(chat_id, msg_id, be_retry=retry)   # the 5 second loop tries again

    async def _on_new(self, ev):
        try:
            text, chat, st = ev.raw_text or "", ev.chat_id, self._st(ev.chat_id)
            if not st.get("enabled", True) and not _tracked(self, chat): return
            reply_id = ev.message.reply_to_msg_id
            text = _strip_quote(ev.message, text)
            parsed = parse(text)
            posted = message_time(ev.message)
            bare = parse_bare_call(text) if parsed is None and not _cross_chat(ev.message) else None
            if parsed is not None or bare or parse_update(text):
                what = "signal" if isinstance(parsed, Signal) or bare else "message"
                if self._too_old(chat, ev.message, st, what): return   # reconnect replays and late messages
            if reply_id and isinstance(parsed, Signal) and not _reply_may_open(ev.message, parsed):
                parsed = None   # a reply only opens when it holds its own full signal, never from a quote
            if reply_id and isinstance(parsed, Signal) and self._rec(chat, reply_id):
                self.log(f"[{self._name(chat)}] reply repeats a signal that was already copied, treating it as an update")
                parsed = Command("MODIFY", sl=parsed.sl, tps=parsed.tps)
            if isinstance(parsed, Signal):
                self.log(f"[{self._name(chat)}] SIGNAL received: {parsed.side} {parsed.symbol}"
                         f"{' ' + parsed.order_type if parsed.order_type and parsed.order_type.upper() != 'MARKET' else ''}, entry {parsed.entry or 'now'}"
                         f"{' to ' + str(parsed.entry_high) if parsed.entry_high else ''}, stop loss {parsed.sl or 'none'}, take profits {', '.join(map(str, parsed.tps)) or 'none'}")
                self._open(chat, ev.id, parsed, st, posted=posted); return
            w = self._waiting_for(chat) if parsed is None and not _cross_chat(ev.message) else None
            if w and (not reply_id or reply_id == w["msg_id"]):
                sig = self._complete(w, text)
                if sig:
                    self._log_signal(chat, sig, "follow up message gives the stop loss")
                    self._open(chat, ev.id, sig, st, parent_id=w["msg_id"]); return
            if bare and self._wait_minutes(st):
                self._remember(chat, ev.id, bare, st, posted); return
            if reply_id:
                if parsed is None and not _cross_chat(ev.message):
                    sig = await self._signal_from_reply(ev, chat, reply_id, text)
                    if sig:
                        self._log_signal(chat, sig, "reply completes the signal")
                        self._open(chat, ev.id, sig, st, parent_id=reply_id); return
                cmd = parsed if isinstance(parsed, Command) else parse_update(text)
                if cmd: self._apply(chat, reply_id, cmd, st)
                return
            if isinstance(parsed, Command) and st.get("commands_without_reply_apply_to_last", True):
                self._apply(chat, self._last(chat), parsed, st)
        except Exception as e:
            self.log(f"Could not handle a message: {e}")

    async def _on_edit(self, ev):
        try:
            chat, st = ev.chat_id, self._st(ev.chat_id)
            if not st.get("follow_edits", True) or (not st.get("enabled", True) and not _tracked(self, chat)): return
            text = _strip_quote(ev.message, ev.raw_text or "")
            rec = self._rec(chat, ev.id)
            parsed = parse(text)
            if (parsed is not None or parse_update(text)) and self._too_old(chat, ev.message, st, "edited message", edited=True):
                return
            if rec is None and getattr(ev.message, "reply_to_msg_id", None) and isinstance(parsed, Signal) \
                    and not _reply_may_open(ev.message, parsed):
                parsed = None
            if rec is None:
                w = self._waiting_for(chat) if not _cross_chat(ev.message) else None
                if isinstance(parsed, Signal):      # signal completed by an edit
                    self.log(f"[{self._name(chat)}] edited message is now a signal")
                    self._open(chat, ev.id, parsed, st, posted=message_time(ev.message, True))
                elif parsed is None and w and ev.id == w["msg_id"] and self._complete(w, text):
                    sig = self._complete(w, text)        # the call we were waiting on was edited to add the numbers
                    self._log_signal(chat, sig, "edited call now gives the stop loss")
                    self._open(chat, ev.id, sig, st)
                elif parsed is None and getattr(ev.message, "reply_to_msg_id", None) and not _cross_chat(ev.message):
                    sig = await self._signal_from_reply(ev, chat, ev.message.reply_to_msg_id, text)
                    if sig:                          # an edited reply now holds the full signal
                        self._log_signal(chat, sig, "edited reply completes the signal")
                        self._open(chat, ev.id, sig, st, parent_id=ev.message.reply_to_msg_id)
                return
            if isinstance(parsed, Signal):
                if parsed.sl != rec.get("sl") or parsed.tps != rec.get("tps"):
                    self.log(f"[{self._name(chat)}] signal was edited: stop loss {parsed.sl}, take profits {parsed.tps}")
                    self._apply(chat, ev.id, Command("MODIFY", sl=parsed.sl, tps=parsed.tps), st)
                    if not _tracked(self, chat): self.store.update(chat, ev.id, sl=parsed.sl, tps=parsed.tps)
            elif isinstance(parsed, Command):
                self._apply(chat, ev.id, parsed, st)
        except Exception as e:
            self.log(f"Could not handle an edited message: {e}")
