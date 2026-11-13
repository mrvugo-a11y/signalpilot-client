"""Config file handling. User data lives in %APPDATA%\\<BRAND_NAME> (Windows) or ~/.<BRAND_NAME> elsewhere.
Set the env var SIGNALPILOT_HOME to use another folder (used by the tests)."""
import copy, json, os, sys
from .branding import BRAND_NAME
from . import app_credentials as AC

DEFAULT_SETTINGS = {
    "enabled": True, "lot_mode": "fixed", "fixed_lot": 0.01, "risk_percent": 1.0,
    "default_sl_points": 0, "split_tps": True, "use_tps": [1, 2, 3, 4, 5],
    "max_spread_points": 50, "magic_number": 777001, "symbol_suffix": "",
    "symbol_map": {"GOLD": "XAUUSD"}, "pending_orders": True, "range_entry": "market",
    "max_entry_distance_points": 0, "reverse_signals": False, "tp_hit_action": "none", "be_after_tp1": False,
    "commands_without_reply_apply_to_last": True, "follow_edits": True,
    "max_open_trades": 20, "slippage_points": 20, "comment": "TGCopier",
    "max_signal_age_minutes": 5, "wait_for_sl_minutes": 15,
}
DEFAULTS = {
    "licence_key": "", "licence_url": "",
    "telegram_api_id": 0, "telegram_api_hash": "", "telegram_phone": "",
    "mt5_path": "", "mt5_login": 0, "mt5_password": "", "mt5_server": "",
    "channels": {}, "default_settings": DEFAULT_SETTINGS,
    "setup_done": False, "share_results": True, "minimise_to_tray": True, "start_copying_on_launch": False,
    "track_followed": True,
}


def app_dir():
    """Folder of the program itself (exe or source)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """Folder for the customer's own files (config, Telegram login, trades, log)."""
    d = os.environ.get("SIGNALPILOT_HOME")
    if not d:
        if sys.platform == "win32":
            d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), BRAND_NAME)
        else:
            d = os.path.join(os.path.expanduser("~"), "." + BRAND_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def config_path():
    return os.path.join(data_dir(), "config.json")


def path(name):
    return os.path.join(data_dir(), name)


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "symbol_map":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate(cfg):
    """Version 1.3.2: moving the stop to entry is controlled only by the break even tick box.
    The old choice "move_sl_be" was the default and saved for everyone, so a deliberate choice cannot be told
    apart from the default: every "move_sl_be" becomes "none" (Do nothing). The tick box is left as it is."""
    sets = [cfg.get("default_settings") or {}] + [(c or {}).get("settings") or {} for c in (cfg.get("channels") or {}).values()]
    changed = False
    for s in sets:
        if s.get("tp_hit_action") == "move_sl_be":
            s["tp_hit_action"] = "none"; changed = True
    return changed


def load():
    """Returns the config with every missing option filled from DEFAULTS.
    First run: copies a config.json shipped next to the program (old zip download, keeps a baked in key)."""
    p = config_path()
    if not os.path.exists(p):
        legacy = os.path.join(app_dir(), "config.json")
        start = {}
        if os.path.exists(legacy):
            try:
                with open(legacy, encoding="utf-8") as f: start = json.load(f)
            except Exception: start = {}
        save(_merge(DEFAULTS, start))
    try:
        with open(p, encoding="utf-8") as f: raw = json.load(f)
    except Exception:
        raw = {}
    cfg = _merge(DEFAULTS, raw)
    if migrate(cfg):
        try: save(cfg)
        except Exception: pass
    return cfg


def save(cfg):
    p = config_path(); tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, p)


def api_credentials(cfg):
    """(api_id, api_hash): the built in ones, else whatever the customer typed in. (0, '') if none."""
    if AC.TELEGRAM_API_ID and AC.TELEGRAM_API_HASH:
        return int(AC.TELEGRAM_API_ID), AC.TELEGRAM_API_HASH
    try: i = int(cfg.get("telegram_api_id") or 0)
    except (TypeError, ValueError): i = 0
    return i, str(cfg.get("telegram_api_hash") or "").strip()


def needs_api_credentials(cfg):
    i, h = api_credentials(cfg)
    return not (i and h)


def licence_url(cfg):
    return (AC.LICENCE_URL or cfg.get("licence_url") or "").strip()


def channel_settings(cfg, chat_id):
    """Default settings merged with the per channel override (channels[id]['settings'])."""
    s = dict(cfg.get("default_settings", {}))
    ch = cfg.get("channels", {}).get(str(chat_id), {})
    over = ch.get("settings", {}) or {}
    if "symbol_map" in over:
        m = dict(s.get("symbol_map", {})); m.update(over["symbol_map"]); over = dict(over, symbol_map=m)
    s.update(over)
    if ch and not ch.get("enabled", True):
        s["enabled"] = False
    return s
