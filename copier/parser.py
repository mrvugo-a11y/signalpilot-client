"""Signal parser: turns Telegram message text into a Signal or a Command."""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Union

NUM = r"(\d+(?:[.,]\d+)?)"

# common aliases -> canonical symbol (before broker mapping)
ALIASES = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "XAU/USD": "XAUUSD", "SILVER": "XAGUSD", "XAG": "XAGUSD",
    "US30": "US30", "DOW": "US30", "DJ30": "US30", "DJI": "US30", "WALLSTREET": "US30",
    "NAS100": "NAS100", "NASDAQ": "NAS100", "NAS": "NAS100", "US100": "NAS100", "USTEC": "NAS100",
    "SPX500": "SPX500", "US500": "SPX500", "SP500": "SPX500", "GER30": "GER40", "GER40": "GER40",
    "DAX": "GER40", "DE40": "GER40", "UK100": "UK100", "BTC": "BTCUSD", "BITCOIN": "BTCUSD",
    "ETH": "ETHUSD", "OIL": "USOIL", "WTI": "USOIL", "USOIL": "USOIL", "BRENT": "UKOIL",
}
CCY = "USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|XAU|XAG|BTC|ETH|ZAR|SGD|NOK|SEK|MXN|TRY|HKD|PLN"
PAIR_RE = re.compile(rf"\b({CCY})\s*/?\s*({CCY})(?:[._-]?[A-Z]{{1,3}})?\b")
# also accept "#SYMBOL" or "SYMBOL" tokens like US30 / BTCUSDT
SIDE_RE = re.compile(r"\b(BUY|SELL|LONG|SHORT)(?:\s*[- ]?\s*(LIMIT|STOP))?\b")


@dataclass
class Signal:
    side: str                      # BUY / SELL
    symbol: str
    order_type: str = "MARKET"     # MARKET, LIMIT, STOP
    entry: Optional[float] = None
    entry_high: Optional[float] = None   # set when an entry range is given
    sl: Optional[float] = None
    tps: List[Optional[float]] = field(default_factory=list)  # None = open TP
    trade_id: Optional[str] = None       # channel's own trade number (e.g. "Номер сделки"), for dedupe


@dataclass
class Command:
    action: str        # CLOSE_ALL, CLOSE_HALF, CLOSE_PARTIAL, MOVE_SL_BE, MOVE_SL, TP_HIT, CANCEL, MODIFY
    value: Optional[float] = None    # price for MOVE_SL, fraction for CLOSE_PARTIAL, tp index for TP_HIT
    sl: Optional[float] = None
    tps: List[Optional[float]] = field(default_factory=list)


def _f(s: str) -> float:
    return float(s.replace(",", "."))


# Russian terms -> English, applied (on upper-cased text) BEFORE the non-ASCII strip.
_C = "А-ЯЁ"
RU_MAP = [
    (r"НОМЕР\s+СДЕЛКИ\s*:?\s*\d+", " "),                  # Signum trade number is not a price
    (r"СДЕЛКА\s+НА\s+\d+(?:[.,]\d+)?\s*%\s*РИСКА\.?", " "),  # info only; user's risk settings apply
    (r"СТОП\s*/\s*ОТМЕНА", " "),                              # analysis-post label, never a cancel
    (r"ТОЧКА\s+ВХОДА", " ENTRY "),
    (rf"ПЕРЕНЕСТИ\s+СТОП\s+В\s+БЕЗУБЫТОК|(?<![{_C}])(?:В\s+)?БЕЗУБЫТОК(?![{_C}])", " BREAKEVEN "),
    (r"СТОП[\s-]*ЛОСС", " SL "),
    (r"ТЕЙК[\s-]*ПРОФИТ", " TP "),
    (rf"(?<![{_C}])(?:ОТМЕНИТЬ\s+ОРДЕР|ОТМЕНИТЬ|ОТМЕНА)(?![{_C}])", " CANCEL "),
    (rf"(?<![{_C}])(?:ПОКУПКА|КУПИТЬ)(?![{_C}])", " BUY "),
    (rf"(?<![{_C}])(?:ПРОДАЖА|ПРОДАТЬ)(?![{_C}])", " SELL "),
]
RU_TRADE_ID_RE = re.compile(r"НОМЕР\s+СДЕЛКИ\s*:?\s*(\d+)", re.I)
# Repost safety rule: analysis posts (marked 'Стоп / отмена') and results reports are
# informational; a reply/repost of one must never open, modify or command a trade.
RU_ANALYSIS_RE = re.compile(r"СТОП\s*/\s*ОТМЕНА", re.I)
RU_RESULT_RE = re.compile(r"ЗАКРЫЛ(?:СЯ|АСЬ|ИСЬ)?\s+(?:В\s+)?(?:ПЛЮС|МИНУС|ПРОФИТ|С\s+ПРОФИТОМ)|РЕЗУЛЬТАТ\s*:"
                          r"|ЗАБРАЛИ|ОТРАБОТКА\s+АНАЛИТИКИ", re.I)


# Result / status updates ("XAU/USD SELL hit TP1 +75 pips", "TP2 hit +120 pips", "SL hit",
# "+75 pips running") report on an existing trade and must never be opened as a new trade.
OUTCOME_RE = re.compile(
    r"\bHIT\b|\bREACHED\b|\bSMASHED\b|\bACHIEVED\b|\bSTOPPED\s*OUT\b|\bCLOSED\b|\bRUNNING\b"
    r"|\bIN\s*(?:PROFIT|PROFITS|LOSS)\b|\bBOOKED\b|\bSECURED\b|\bBOOM\b"
    r"|[+-]\s*\d+(?:[.,]\d+)?\s*(?:PIPS?|POINTS?|PTS)\b|\bPIPS?\s*(?:PROFIT|GAIN|LOSS)\b"
    r"|\bTP\s*\d\s*(?:DONE|\+)|\bPROFIT\s*(?:BOOKED|TAKEN|SECURED)\b"
    r"|\bACTIVATED\b|\bTRIGGERED\b|\bFILLED\b|\bSTOPPED\b")
# "SL -75 pips" / "SL hit" style: a stop loss mention that is a result, not a stop price
SL_RESULT_RE = re.compile(r"\bSL\s*(?:HIT|TAKEN)\b|\bHIT\s*SL\b|\bSL\s*[+-]\s*\d+(?:[.,]\d+)?\s*(?:PIPS?|POINTS?|PTS)\b")


# Unmistakable status phrases: block even when the text also carries a (quoted) full signal.
STATUS_RE = re.compile(r"\b(?:TRADE|ORDER|SIGNAL|ENTRY|POSITION)\s*(?:IS\s*|HAS\s*BEEN\s*|GOT\s*)?(?:ACTIVATED|TRIGGERED|FILLED)\b"
                       r"|\b(?:TP|SL)\s*\d?\s*(?:HIT|TAKEN)\b|\bHIT\s*(?:TP|SL)\s*\d?\b|\bRUNNING\s*IN\s*(?:PROFIT|LOSS)\b")


def is_outcome(t: str) -> bool:
    """True when normalised text reports a result/status rather than giving a new trade."""
    return bool(OUTCOME_RE.search(t))


def trade_id(text: str) -> Optional[str]:
    m = RU_TRADE_ID_RE.search(text or "")
    return m.group(1) if m else None


def is_ru_info(text: str) -> bool:
    """Russian analysis post or results report: never a trade instruction."""
    return bool(RU_ANALYSIS_RE.search(text or "") or RU_RESULT_RE.search(text or ""))


def normalise(text: str) -> str:
    t = text.upper()
    for pat, rep in RU_MAP:
        t = re.sub(pat, rep, t)
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("：", ":")
    t = re.sub(r"[^\x00-\x7F]+", " ", t)          # strip emojis
    t = t.replace("TAKE PROFIT", "TP").replace("TAKEPROFIT", "TP").replace("T/P", "TP")
    t = re.sub(r"STOP\s*-?\s*LOSS|S/L|\bSTOPLOSS\b", "SL", t)
    # "TP1. 4151.00" / "SL. 4121.00": a dot used as the separator (only when a space follows, so TP1.0850 is untouched)
    t = re.sub(r"\b(TP\s*\d{0,2}|SL)\s*\.(?=\s+\d)", r"\1:", t)
    t = re.sub(r"\bTARGET\s*(\d)", r"TP\1", t)
    t = re.sub(r"\bTARGETS?\b", "TP", t)
    return t


def find_symbol(t: str) -> Optional[str]:
    m = PAIR_RE.search(t)
    cands = []
    if m:
        cands.append((m.start(), m.group(1) + m.group(2)))
    for alias, sym in ALIASES.items():
        am = re.search(rf"(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])", t)
        if am:
            cands.append((am.start(), sym))
    cm = re.search(r"\b(BTC|ETH|SOL|XRP|BNB|ADA|DOGE)USDT?\b", t)
    if cm:
        cands.append((cm.start(), cm.group(1) + "USD"))
    hm = re.search(r"#([A-Z0-9]{3,10})\b", t)
    if hm and not cands:
        cands.append((hm.start(), hm.group(1)))
    if not cands:
        return None
    return sorted(cands)[0][1]


def parse_command(text: str) -> Optional[Command]:
    t = normalise(text)
    if re.search(r"\b(CANCEL|DELETE)\b.*\b(ORDER|LIMIT|PENDING|SIGNAL|IT)?\b", t) and not SIDE_RE.search(t):
        return Command("CANCEL")
    if re.search(r"\b(SL|STOP)\b.*\b(BE|B/E|BREAK\s*EVEN|BREAKEVEN|ENTRY|ENTRY\s*PRICE|COST)\b", t) \
            or re.search(r"\b(BREAK\s*EVEN|BREAKEVEN)\b", t) or re.search(r"\bSET\s*BE\b|\bBE\s*NOW\b", t):
        return Command("MOVE_SL_BE")
    m = re.search(r"\b(?:MOVE|SET|CHANGE|UPDATE|PUT)\s*(?:THE\s*)?SL\s*(?:TO|AT|@|:)?\s*" + NUM, t) \
        or re.search(r"\bSL\s*(?:MOVED\s*)?(?:TO|AT|@|=)\s*" + NUM, t) \
        or re.search(r"\bNEW\s*SL\s*[:=@]?\s*" + NUM, t)
    if m:
        return Command("MOVE_SL", value=_f(m.group(1)))
    m = re.search(r"\bCLOSE\s*(\d{1,2})\s*%", t) or re.search(r"\b(\d{1,2})\s*%\s*CLOSE", t)
    if m:
        return Command("CLOSE_PARTIAL", value=int(m.group(1)) / 100.0)
    if re.search(r"\bCLOSE\s*(?:THE\s*)?(?:HALF|50\s*%|PARTIAL|PARTIALS?|SOME)\b|\b(?:TAKE|SECURE)\s*PARTIALS?\b|\bHALF\s*CLOSE\b|\bCLOSE\s*1/2\b", t):
        return Command("CLOSE_HALF")
    m = re.search(r"\bTP\s*(\d)\s*(?:HIT|DONE|REACHED|SMASHED|ACHIEVED|\+|PIPS|BOOM)", t) \
        or re.search(r"\b(?:HIT|REACHED)\s*TP\s*(\d)", t)
    if m:
        return Command("TP_HIT", value=int(m.group(1)))
    if re.search(r"\bTP\s*(?:HIT|DONE|REACHED)\b", t):
        return Command("TP_HIT", value=1)
    if re.search(r"\bSL\s*(?:HIT|TAKEN)\b|\bSTOPPED\s*OUT\b", t):
        return Command("SL_HIT")
    if re.search(r"\b(CLOSE|EXIT|CLOSED)\b(?!.*\b\d)|\bCLOSE\s*(ALL|NOW|FULL|EVERYTHING|TRADES?|POSITIONS?)\b|\bEXIT\s*NOW\b", t):
        return Command("CLOSE_ALL")
    return None


def _sl_tps(t: str):
    sl = None
    m = re.search(r"\bSL\s*(?:PRICE)?\s*[:=@\-]?\s*" + NUM, t)
    if m:
        sl = _f(m.group(1))
    tps = []
    for m in re.finditer(r"\bTP(?:\s*\d{1,2}\s*[:=@\-)]|\d{1,2}\s+|\s*)\s*[:=@\-]?\s*(" + NUM[1:-1] + r"|OPEN)\b", t):
        v = m.group(1)
        tps.append(None if v == "OPEN" else _f(v))
    # "TP: 1960 / 1970 / 1980" style
    m = re.search(r"\bTP\s*[:=@]?\s*" + NUM + r"((?:(?:\s*[/|]\s*|,\s+)" + NUM + r")+)", t)
    if m and len(tps) <= 1:
        tps = [_f(m.group(1))] + [_f(x) for x in re.findall(NUM, m.group(2))]
    return sl, tps


def parse_signal(text: str) -> Optional[Signal]:
    t = normalise(text)
    sm = SIDE_RE.search(t)
    if not sm:
        return None
    side = "BUY" if sm.group(1) in ("BUY", "LONG") else "SELL"
    otype = sm.group(2) or "MARKET"
    symbol = find_symbol(t)
    if not symbol:
        return None
    sl, tps = _sl_tps(t)
    if sl is None and not tps:
        return None  # not a full signal (avoid "buy the dip" chatter)
    if STATUS_RE.search(t) or (is_outcome(t) and (sl is None or SL_RESULT_RE.search(t))):
        return None  # result/status update ("SELL hit TP1 +75 pips", "BUY hit SL -75 pips"), not a new trade
    # entry: search in text before first SL/TP keyword, excluding symbol digits
    cut = re.search(r"\b(SL|TP)\b", t)
    head = t[: cut.start()] if cut else t
    head = PAIR_RE.sub(" ", head)
    head = re.sub(r"\b(US30|NAS100|US100|SPX500|US500|SP500|GER30|GER40|DE40|UK100|DJ30)\b", " ", head)
    head = re.sub(r"\bTP\s*\d", " ", head)
    entry = entry_high = None
    m = re.search(NUM + r"\s*(?:-|TO|/|~)\s*" + NUM, head)
    if m:
        a, b = _f(m.group(1)), _f(m.group(2))
        entry, entry_high = min(a, b), max(a, b)
    else:
        m = re.search(r"(?:@|AT|ENTRY|PRICE|ZONE|OPEN)\s*(?:PRICE|POINT|ZONE)?\s*[:=@\-]?\s*" + NUM, head) \
            or re.search(r"(?<![\w.])" + NUM + r"(?![\w.%])", head)
        if m:
            entry = _f(m.group(1))
    if entry is not None and otype == "MARKET" and re.search(r"\b(NOW|MARKET|CMP|CURRENT)\b", head) and entry_high is None:
        pass  # market with reference price
    return Signal(side=side, symbol=symbol, order_type=otype, entry=entry,
                  entry_high=entry_high, sl=sl, tps=tps, trade_id=trade_id(text))


def parse(text: str) -> Optional[Union[Signal, Command]]:
    if not text or not text.strip():
        return None
    if is_ru_info(text):
        return None   # analysis post / results report (repost safety rule)
    s = parse_signal(text)
    if s:
        return s
    return parse_command(text)


BARE_MAX_WORDS = 12


def parse_bare_call(text: str) -> Optional[Signal]:
    """A short call without numbers, like 'BUY XAUUSD NOW' or 'sell gold now'. Returns a Signal holding only
    the side and symbol (never opened on its own: the copier waits for the stop loss), or None."""
    if not text or not text.strip() or is_ru_info(text):
        return None
    t = normalise(text)
    if len(t.split()) > BARE_MAX_WORDS or STATUS_RE.search(t) or is_outcome(t):
        return None
    if parse_signal(text) or parse_command(text):
        return None
    sm = SIDE_RE.search(t)
    if not sm:
        return None
    symbol = find_symbol(t)
    if not symbol:
        return None
    sl, tps = _sl_tps(t)
    if sl is not None or tps:
        return None
    return Signal(side="BUY" if sm.group(1) in ("BUY", "LONG") else "SELL", symbol=symbol, order_type=sm.group(2) or "MARKET")


def is_status(text: str) -> bool:
    """Result or status message (TP hit, SL hit, running in profit, activated): never opens a trade."""
    t = normalise(text or "")
    return bool(STATUS_RE.search(t) or is_outcome(t) or SL_RESULT_RE.search(t) or is_ru_info(text or ""))


def parse_update(text: str) -> Optional[Command]:
    """For edits/replies that contain new SL/TP values without being a full signal."""
    if is_ru_info(text):
        return None   # reposted analysis / results report replying to a post is not a modification
    t = normalise(text)
    sl, tps = _sl_tps(t)
    if is_outcome(t) and (sl is None or SL_RESULT_RE.search(t)):
        return None   # "TP1 +75 pips" is a result, not new take profit values
    if sl is not None or tps:
        return Command("MODIFY", sl=sl, tps=tps)
    return None
