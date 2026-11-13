"""Remembers which MT5 tickets belong to which Telegram message (survives restarts)."""
import json, os, threading
from . import config as C

class Store:
    def __init__(self, path=None):
        path = path or C.path("trades.json")
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {"signals": {}, "last": {}}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def put(self, chat_id, msg_id, record):
        """record = {symbol, side, entry, sl, tickets:[...]}"""
        with self.lock:
            self.data["signals"][f"{chat_id}:{msg_id}"] = record
            if record.get("channel_name"):
                self.data.setdefault("names", {})[str(chat_id)] = record["channel_name"]
            tm = self.data.setdefault("tickets", {})     # permanent ticket to channel map, for Channel results
            for t in record.get("tickets") or []: tm[str(t)] = str(chat_id)
            ts = self.data.setdefault("ticket_signals", {})   # permanent ticket to signal map, for per signal pips
            for t in record.get("tickets") or []: ts[str(t)] = f"{chat_id}:{msg_id}"
            self.data["last"][str(chat_id)] = msg_id
            if len(self.data["signals"]) > 2000:
                for k in list(self.data["signals"])[:500]:
                    del self.data["signals"][k]
            self._save()

    def set_names(self, names):
        """Remembers channel titles {chat_id: title} (used by Channel results, also for removed channels)."""
        names = {str(k): v for k, v in (names or {}).items() if v}
        if not names: return
        with self.lock:
            cur = self.data.setdefault("names", {})
            if all(cur.get(k) == v for k, v in names.items()): return
            cur.update(names); self._save()

    def set_usernames(self, names):
        names = {str(k): v for k, v in (names or {}).items() if v}
        if not names: return
        with self.lock:
            cur = self.data.setdefault("usernames", {})
            if all(cur.get(k) == v for k, v in names.items()): return
            cur.update(names); self._save()

    def has_trade(self, chat_id, trade_id):
        """True if this channel's trade number was already copied (channels may repost it)."""
        return bool(trade_id) and f"{chat_id}:{trade_id}" in self.data.get("trade_ids", {})

    def mark_trade(self, chat_id, trade_id, msg_id):
        if not trade_id:
            return
        with self.lock:
            self.data.setdefault("trade_ids", {})[f"{chat_id}:{trade_id}"] = msg_id
            self._save()

    def _key(self, chat_id, msg_id):
        k = f"{chat_id}:{msg_id}"
        return self.data.get("aliases", {}).get(k, k)

    def alias(self, chat_id, msg_id, real_msg_id):
        """Replies to msg_id (a short parent like 'XAUUSD BUY NOW') use the trades opened from real_msg_id."""
        k = f"{chat_id}:{msg_id}"
        if k in self.data["signals"]: return
        with self.lock:
            self.data.setdefault("aliases", {})[k] = f"{chat_id}:{real_msg_id}"
            self._save()

    def get(self, chat_id, msg_id):
        return self.data["signals"].get(self._key(chat_id, msg_id))

    def last_id(self, chat_id):
        return self.data["last"].get(str(chat_id))

    def update(self, chat_id, msg_id, **kw):
        with self.lock:
            r = self.data["signals"].get(self._key(chat_id, msg_id))
            if r is not None:
                r.update(kw); self._save()
