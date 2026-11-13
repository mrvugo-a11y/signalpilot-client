"""First run wizard logic, kept free of Tk so it can be tested headlessly."""
import re
from . import config as C


def wizard_steps(cfg):
    steps = ["welcome", "licence"]
    if C.needs_api_credentials(cfg):
        steps.append("api")
    steps += ["telegram", "channels", "mt5", "done"]
    return steps


def needs_setup(cfg):
    return not cfg.get("setup_done") or not cfg.get("channels") or C.needs_api_credentials(cfg)


def normalise_phone(text):
    """'+27 82 123 4567' or '0027821234567' to '+27821234567'. Returns (phone, error)."""
    t = re.sub(r"[\s\-().]", "", text or "")
    if t.startswith("00"): t = "+" + t[2:]
    if not t.startswith("+"):
        return None, "Please start with + and your country code, for example +27821234567."
    if not t[1:].isdigit() or not 8 <= len(t) - 1 <= 15:
        return None, "That does not look like a phone number. Example: +27821234567."
    return t, None


def check_api_credentials(api_id, api_hash):
    """Returns (id:int, hash:str, error)."""
    try: i = int(str(api_id).strip())
    except ValueError: return 0, "", "The API ID is a number, for example 1234567."
    h = str(api_hash or "").strip()
    if i <= 0: return 0, "", "The API ID is a number, for example 1234567."
    if not re.fullmatch(r"[0-9a-fA-F]{32}", h): return 0, "", "The API hash is 32 letters and numbers. Copy it again from my.telegram.org."
    return i, h, None


def check_licence_key(cfg, key, checker=None):
    """Returns (ok, message). With no licence address configured the copier runs as a trial."""
    if checker is None:
        from .licence import check as checker
    key = (key or "").strip()
    url = C.licence_url(cfg)
    if url and not key:
        return False, "Please paste the licence key from your account page."
    return checker(key, url)


def apply_channel_selection(cfg, chans, selected_ids):
    """chans = [(id, name)], selected_ids = ids ticked. Keeps existing per channel settings."""
    cur = cfg.get("channels", {}); sel = {str(x) for x in selected_ids}; new = {}
    for cid, name in chans:
        if str(cid) in sel:
            old = cur.get(str(cid), {})
            new[str(cid)] = {"name": name, "enabled": True, "settings": old.get("settings", {})}
            if "track" in old: new[str(cid)]["track"] = old["track"]
    cfg["channels"] = new
    return cfg


def detect_mt5(cfg, mt5=None):
    """Finds the running MT5 terminal. Returns dict(ok, message, login, server, broker, name, balance, currency, path, algo)."""
    if mt5 is None:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            return {"ok": False, "message": "The MetaTrader 5 connector is missing from this install."}
    kw = {"path": cfg["mt5_path"]} if cfg.get("mt5_path") else {}
    if not mt5.initialize(**kw):
        return {"ok": False, "message": "MetaTrader 5 was not found. Open MetaTrader 5, log in to your account and click Try again."}
    try:
        ai, ti = mt5.account_info(), mt5.terminal_info()
        if ai is None:
            return {"ok": False, "message": "MetaTrader 5 is open but not logged in to an account. Log in and click Try again."}
        algo = bool(getattr(ti, "trade_allowed", True))
        msg = f"Found account {ai.login} at {getattr(ai, 'company', '') or ai.server} ({ai.server}), balance {ai.balance} {ai.currency}."
        if not algo:
            msg += " Algo Trading is OFF: click the Algo Trading button in MetaTrader 5 so it turns green."
        return {"ok": True, "message": msg, "login": ai.login, "server": ai.server, "broker": getattr(ai, "company", ""),
                "name": getattr(ai, "name", ""), "balance": ai.balance, "currency": ai.currency,
                "path": getattr(ti, "path", ""), "algo": algo}
    finally:
        mt5.shutdown()


def finish(cfg, mt5_info=None):
    if mt5_info and mt5_info.get("ok") and mt5_info.get("path") and not cfg.get("mt5_path"):
        import os
        exe = os.path.join(mt5_info["path"], "terminal64.exe")
        if os.path.exists(exe): cfg["mt5_path"] = exe
    cfg["setup_done"] = True
    return cfg
