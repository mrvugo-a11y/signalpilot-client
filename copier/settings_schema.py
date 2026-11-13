"""Describes every config.json option for the Settings window: label, kind, choices and help text.
Pure Python (no Tk) so it can be tested headlessly. UI copy here must not contain dashes."""

# kind: bool | int | float | text | secret | choice | tps | symbolmap | file
# choice options are (stored value, label shown to the customer)
TRADE_FIELDS = [
    ("enabled", "Copy this channel", "bool", None,
     "Untick to pause copying without removing anything."),
    ("lot_mode", "Lot size method", "choice", [("fixed", "Fixed lot size"), ("risk", "Percent of my balance")],
     "Fixed uses the same lot size for every trade. Percent of my balance works out the lot size from the stop loss."),
    ("fixed_lot", "Fixed lot size", "float", None,
     "Lot size of each trade, for example 0.01. When a signal has 3 take profits you get 3 trades of this size."),
    ("risk_percent", "Risk per signal (percent)", "float", None,
     "For example 1 means risk 1 percent of your balance on the whole signal, shared over all its trades. Needs a stop loss."),
    ("default_sl_points", "Stop loss when the signal has none (points)", "int", None,
     "Used only when a signal comes without a stop loss and waiting for the stop loss below is set to 0. 0 means no stop loss."),
    ("wait_for_sl_minutes", "Wait for the stop loss (minutes)", "int", None,
     "A signal like BUY GOLD NOW without a stop loss is not copied. The copier waits this many minutes for the same channel "
     "to send the stop loss, then opens the trade. 0 means signals without a stop loss open straight away like before."),
    ("max_signal_age_minutes", "Ignore signals older than (minutes)", "int", None,
     "Messages that arrive late, for example after your internet or Telegram was disconnected, are not copied when they "
     "were sent longer ago than this. 0 turns this check off."),
    ("split_tps", "Open one trade per take profit", "bool", None,
     "Ticked: TP1, TP2, TP3 each get their own trade. Unticked: one trade that uses TP1."),
    ("use_tps", "Take profits to use", "tps", None,
     "Which take profits to trade, separated by commas. For example 1,2 uses only TP1 and TP2."),
    ("max_spread_points", "Skip if the spread is above (points)", "int", None,
     "Protects you from bad prices. 0 turns this check off."),
    ("magic_number", "Trade ID number (magic number)", "int", None,
     "A number that marks trades opened by the copier in MetaTrader 5. You can normally leave it."),
    ("symbol_suffix", "Broker symbol ending", "text", None,
     "Some brokers add letters to symbol names, for example m in XAUUSDm. Leave empty, the copier also finds it by itself."),
    ("symbol_map", "Rename symbols", "symbolmap", None,
     "When the channel uses another name than your broker. Write NAME=BROKERNAME separated by commas, for example GOLD=XAUUSD, US30=DJ30."),
    ("pending_orders", "Place limit and stop orders", "bool", None,
     "Ticked: BUY LIMIT, SELL STOP and similar signals are placed as pending orders. Unticked: they are skipped."),
    ("range_entry", "Entry given as a zone (like 1950 to 1945)", "choice",
     [("market", "Enter straight away"), ("first", "Wait at the nearest edge of the zone"), ("mid", "Wait at the middle of the zone")],
     "How to enter when the signal gives a price zone instead of one price."),
    ("max_entry_distance_points", "Price moved too far, wait for the entry (points)", "int", None,
     "If the price has moved further than this from the signal entry, a pending order is placed at the entry instead. 0 means always enter now."),
    ("reverse_signals", "Trade the opposite direction", "bool", None,
     "Ticked: every BUY becomes a SELL and every SELL becomes a BUY."),
    ("tp_hit_action", "When the channel says TP1 hit", "choice",
     [("none", "Do nothing"), ("close_half", "Close half"), ("close_all", "Close everything")],
     "What to do with the remaining trades when the channel posts that a take profit was reached. Moving the stop loss to entry after TP1 is controlled by the break even tick box."),
    ("be_after_tp1", "Move stop loss to break even after TP1 is hit", "bool", None,
     "Ticked: when the price reaches TP1, or the channel says TP1 hit, the stop loss of the other trades from that signal moves to the entry price. Unticked: neither of these moves your stop loss. A channel message that clearly says move to break even is always followed."),
    ("commands_without_reply_apply_to_last", "Plain commands act on the latest signal", "bool", None,
     "Ticked: a message like close that is not a reply acts on the newest signal of that channel."),
    ("follow_edits", "Follow edited signals", "bool", None,
     "Ticked: when the channel edits a signal message, your trades are updated too."),
    ("max_open_trades", "Most trades open at once", "int", None,
     "Safety limit for trades opened by the copier."),
    ("slippage_points", "Allowed slippage (points)", "int", None,
     "How far the price may move while a market order is being filled."),
    ("comment", "Trade comment", "text", None,
     "Text shown next to each copied trade in MetaTrader 5."),
]

GENERAL_FIELDS = [
    ("licence_key", "Licence key", "text", None, "The key from your account page on our website."),
    ("telegram_phone", "Telegram phone number", "text", None,
     "With country code, for example +27821234567."),
    ("minimise_to_tray", "Hide to the tray when minimised", "bool", None,
     "Keeps the copier running next to the clock instead of on the taskbar."),
    ("share_results", "Share my channel results to help rank signal providers", "bool", None,
     "Shares only your per channel results, never your account details or logins. Untick to stop sharing."),
    ("start_copying_on_launch", "Start copying when the program opens", "bool", None,
     "Saves you clicking Start every time."),
    ("track_followed", "Track pips for channels I follow but do not copy", "bool", None,
     "While the copier runs, signals from your other Telegram channels are followed on paper with live MetaTrader 5 "
     "prices. No trade is ever opened for them. Their pips show in Channel results as Tracked, not copied."),
]

MT5_FIELDS = [
    ("mt5_path", "MetaTrader 5 program (terminal64.exe)", "file", None,
     "Leave empty to use the MetaTrader 5 that is open. Set it if you have more than one installed."),
    ("mt5_login", "MT5 account number", "int", None, "Optional. Leave 0 to use the account already logged in."),
    ("mt5_password", "MT5 password", "secret", None, "Optional. Only needed together with the account number."),
    ("mt5_server", "MT5 server", "text", None, "Optional. For example YourBroker Live."),
]

API_FIELDS = [
    ("telegram_api_id", "Telegram API ID", "int", None, "Only needed if your copier did not come with one built in."),
    ("telegram_api_hash", "Telegram API hash", "text", None, "Only needed if your copier did not come with one built in."),
]


class FieldError(ValueError):
    pass


def to_text(kind, value):
    """Stored value to what the entry box shows."""
    if kind == "tps":
        return ",".join(str(x) for x in (value or []))
    if kind == "symbolmap":
        return ", ".join(f"{k}={v}" for k, v in (value or {}).items())
    if value is None:
        return ""
    return str(value)


def from_text(kind, text, label="This field"):
    """What the customer typed to the stored value. Raises FieldError with a friendly message."""
    t = (text or "").strip()
    if kind == "int":
        if t == "": return 0
        try: return int(float(t))
        except ValueError: raise FieldError(f"{label}: please type a whole number.")
    if kind == "float":
        if t == "": return 0.0
        try: return float(t.replace(",", "."))
        except ValueError: raise FieldError(f"{label}: please type a number, for example 0.01.")
    if kind == "tps":
        try: out = sorted({int(x) for x in t.replace(" ", "").split(",") if x})
        except ValueError: raise FieldError(f"{label}: type numbers separated by commas, for example 1,2,3.")
        if not out or any(x < 1 for x in out): raise FieldError(f"{label}: use at least one number from 1 up.")
        return out
    if kind == "symbolmap":
        out = {}
        for part in [p for p in t.replace(";", ",").split(",") if p.strip()]:
            if "=" not in part: raise FieldError(f"{label}: write each pair as NAME=BROKERNAME.")
            k, v = (x.strip().upper() if i == 0 else x.strip() for i, x in enumerate(part.split("=", 1)))
            if not k or not v: raise FieldError(f"{label}: write each pair as NAME=BROKERNAME.")
            out[k] = v
        return out
    return t


def choice_label(options, value):
    for v, lab in options:
        if v == value: return lab
    return options[0][1]


def choice_value(options, label):
    for v, lab in options:
        if lab == label: return v
    return options[0][0]


def validate_trade_settings(s):
    """Extra sanity checks across fields. Returns a list of friendly problems (empty = fine)."""
    probs = []
    if s.get("lot_mode") == "fixed" and not (s.get("fixed_lot", 0) > 0):
        probs.append("Fixed lot size must be above 0.")
    if s.get("lot_mode") == "risk" and not (0 < s.get("risk_percent", 0) <= 100):
        probs.append("Risk per signal must be between 0 and 100.")
    for k in ("max_spread_points", "default_sl_points", "max_open_trades", "slippage_points", "max_entry_distance_points",
              "max_signal_age_minutes", "wait_for_sl_minutes"):
        if s.get(k, 0) < 0: probs.append(f"{label_of(k)} cannot be negative.")
    return probs


def label_of(key):
    for f in TRADE_FIELDS + GENERAL_FIELDS + MT5_FIELDS + API_FIELDS:
        if f[0] == key: return f[1]
    return key


def all_ui_text():
    """Every label and help string (used by a test that checks there are no dashes)."""
    out = []
    for f in TRADE_FIELDS + GENERAL_FIELDS + MT5_FIELDS + API_FIELDS:
        out += [f[1], f[4]] + [lab for _, lab in (f[3] or [])]
    return out
