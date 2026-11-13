"""Pip sizes, the same table the SignalPilot website uses (lib/pips.php), so the app and the leaderboard agree.
Broker suffixes are handled: XAUUSDm, XAUUSD.c, #XAUUSD all count as XAUUSD. Unknown symbols return None (never guessed)."""
import re

TABLE = {
    # metals
    "XAUUSD": 0.1, "GOLD": 0.1, "XAGUSD": 0.01, "SILVER": 0.01,
    # oil
    "USOIL": 0.01, "XTIUSD": 0.01, "WTI": 0.01, "UKOIL": 0.01, "XBRUSD": 0.01, "BRENT": 0.01,
    # indices
    "US30": 1.0, "DJ30": 1.0, "WS30": 1.0, "NAS100": 1.0, "USTEC": 1.0, "US100": 1.0,
    "US500": 0.1, "SPX500": 0.1, "GER40": 1.0, "DE40": 1.0, "DE30": 1.0, "GER30": 1.0, "UK100": 1.0, "JP225": 1.0,
    # crypto
    "BTCUSD": 1.0, "BTCUSDT": 1.0, "ETHUSD": 0.1,
}
JPY_PIP, FX_PIP = 0.01, 0.0001
FX_CODES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "DKK", "PLN", "HUF", "CZK", "TRY",
            "ZAR", "MXN", "SGD", "HKD", "CNH", "ILS", "THB"}


def clean(symbol):
    return re.sub(r"[^A-Za-z0-9]", "", str(symbol or "")).upper()


def pip_size(symbol, table=None):
    """Pip size for a broker symbol, or None when it is not in the table."""
    table = TABLE if table is None else table
    s = clean(symbol)
    if not s: return None
    best, ln = None, 0
    for k, size in table.items():
        # the broker symbol is the table symbol plus at most a short suffix such as m, c, z, pro, ecn, micro
        if s.startswith(k) and len(s) - len(k) <= 5 and len(k) > ln:
            best, ln = size, len(k)
    if best is not None: return best
    a, b = s[:3], s[3:6]
    if 6 <= len(s) <= 11 and a in FX_CODES and b in FX_CODES:
        return JPY_PIP if b == "JPY" else FX_PIP
    return None


def pips(symbol, side, entry, exit_price):
    """Signed pips of a trade (side BUY/SELL or +1/-1). None when the pip size is unknown."""
    size = pip_size(symbol)
    if size is None or entry is None or exit_price is None: return None
    sgn = side if isinstance(side, int) else (1 if str(side).upper() == "BUY" else -1)
    return round(sgn * (float(exit_price) - float(entry)) / size, 1)
