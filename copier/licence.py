"""Licence check.
- licence_url empty  -> offline TRIAL mode (TRIAL_DAYS from first run).
- licence_url set    -> POST {"key","machine","product"} to the URL (e.g. https://yoursite/api/licence), expects JSON
  {"valid": true, "expires": "YYYY-MM-DD", "plan": "...", "max_channels": N, "max_machines": N}
  or {"valid": false, "message": "..."}. max_channels 0 means no channel cap (Unlimited).
  Markets only plans have no copier. The app enforces the channel cap on distinct channels copied.
  A plan change is applied on the next successful licence check (Start asks the server; a background
  refresh also updates the cached limits).

The last confirmed answer is cached in licence.dat (signed with an HMAC tied to this computer):
- cache under CACHE_HOURS old and its expiry not passed -> OK at once, no network call (the app then refreshes quietly).
- otherwise ask the server (60 s timeout, one retry after 10 s when it cannot be reached).
- server unreachable -> still OK while the last confirmation is under GRACE_DAYS old and its expiry has not passed;
  otherwise WAITING: the app shows "Checking licence..." and keeps retrying (wait_for_server) until the server answers.
- an explicit rejection from the server always blocks and clears the cache.
"""
import hashlib, hmac, json, os, platform, time, urllib.error, urllib.request, uuid
from datetime import date, datetime
from . import config as C

TRIAL_DAYS = 7
GRACE_DAYS = 3
CACHE_HOURS = 24
RECHECK_HOURS = 12
TIMEOUT = 60          # free hosting can take about 50 s to wake up
RETRY_DELAY = 10
_SECRET = b"SignalPilot/licence-cache/v1/7f3a91c2e4b8"
UNREACHABLE_MSG = "Could not reach the licence server. Check your internet and try again."

OK, REJECTED, UNREACHABLE = "ok", "rejected", "unreachable"
WAITING = None                      # check_full's ok value when the app should wait for the server
CHECKING_MSG = "Checking licence..."
WAIT_STEPS = (15, 30, 60)           # seconds between attempts, then every WAIT_EVERY
WAIT_EVERY = 120
HINT_AFTER = 600                    # gentle internet hint after about 10 minutes of waiting
HINT_MSG = "Still checking your licence. Please check your internet connection."


def machine_id():
    raw = f"{platform.node()}-{uuid.getnode()}-{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _state():
    try:
        with open(C.path("licence.dat")) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception: return {}


def _write(st):
    with open(C.path("licence.dat"), "w") as f: json.dump(st, f)


# ---- signed cache
_FIELDS = ("key", "machine", "last_ok", "expires", "plan", "max_channels", "max_machines")

def _sig(c, machine):
    secret = hashlib.sha256(_SECRET + machine.encode()).digest()
    msg = json.dumps([c.get(k) for k in _FIELDS], separators=(",", ":")).encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def load_cache():
    """The cached confirmation, or None when missing, for another computer or tampered with."""
    c = _state().get("cache")
    if not isinstance(c, dict): return None
    m = machine_id()
    if c.get("machine") != m or not hmac.compare_digest(str(c.get("sig", "")), _sig(c, m)): return None
    return c


def save_cache(key, expires, plan, now=None, max_channels=None, max_machines=None):
    c = {"key": key, "machine": machine_id(), "last_ok": float(now if now is not None else time.time()),
         "expires": expires or "", "plan": plan or "",
         "max_channels": max_channels, "max_machines": max_machines}
    c["sig"] = _sig(c, c["machine"])
    st = _state(); st["cache"] = c
    for old in ("last_ok", "key"): st.pop(old, None)   # pre 1.2.4 unsigned fields
    _write(st)


def clear_cache():
    st = _state()
    if any(k in st for k in ("cache", "last_ok", "key")):
        for k in ("cache", "last_ok", "key"): st.pop(k, None)
        _write(st)


def _expired(expires, now):
    if not expires: return False
    try: d = datetime.strptime(str(expires)[:10], "%Y-%m-%d").date()
    except ValueError: return False
    return date.fromtimestamp(now) > d


def _usable(c, key, now, max_age):
    return bool(c and c.get("key") == key and 0 <= now - float(c.get("last_ok", 0)) < max_age
                and not _expired(c.get("expires"), now))


def _valid_msg(expires, extra=""):
    return "Licence valid" + (f" until {expires}" if expires else "") + extra


# ---- server
def _ask(key, url):
    body = json.dumps({"key": key, "machine": machine_id(), "product": "tg-copier"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def server_check(key, url, sleep=time.sleep, attempts=2):
    """Asks the server. Returns (OK | REJECTED | UNREACHABLE, message). Updates or clears the cache."""
    ans = None
    for attempt in range(attempts):
        try:
            ans = _ask(key, url)
            if not isinstance(ans, dict) or "valid" not in ans: raise ValueError("unexpected answer")
            break
        except Exception:
            ans = None
            if attempt < attempts - 1: sleep(RETRY_DELAY)
    if ans is None:
        return UNREACHABLE, UNREACHABLE_MSG
    if ans.get("valid"):
        save_cache(key, ans.get("expires"), ans.get("plan"),
                   max_channels=ans.get("max_channels"), max_machines=ans.get("max_machines"))
        return OK, _valid_msg(ans.get("expires"))
    clear_cache()
    return REJECTED, "Licence not accepted: " + str(ans.get("message", "rejected"))


# follow-up the app should do after check_full / refresh
REFRESH = "refresh"   # quietly ask the server once in the background
WAIT = "wait"         # keep retrying in the background (wait_for_server) until the server answers


def check_full(key, url, now=None, sleep=time.sleep, fresh=False):
    """Returns (ok, message, follow).
    ok: True (may run), False (blocked) or WAITING (never confirmed here: wait for the first server answer).
    follow: None, REFRESH or WAIT.
    fresh=True (Start): ask the server first so a plan change is applied before copying begins.
    If the server cannot be reached, the signed cache / offline grace is used as usual."""
    st = _state()
    if not url:
        first = st.setdefault("trial_start", time.time()); _write(st)
        left = TRIAL_DAYS - (time.time() - first) / 86400
        if left > 0:
            return True, f"Trial mode: {left:.1f} days left", None
        return False, "Trial expired. Please enter a licence key.", None
    if not key:
        return False, "Please enter your licence key.", None
    now = time.time() if now is None else now
    c = load_cache()
    if not fresh and _usable(c, key, now, CACHE_HOURS * 3600):
        return True, _valid_msg(c.get("expires")), REFRESH
    status, msg = server_check(key, url, sleep)
    if status == OK: return True, msg, None
    if status == REJECTED: return False, msg, None
    return offline_verdict(key, now)


def offline_verdict(key, now=None):
    """The server cannot be reached right now.
    - confirmed within GRACE_DAYS and expiry not passed: run (the 12 h recheck carries on).
    - confirmed on this computer before (older, or expiry passed): run while retrying in the background;
      only an actual answer from the server can block.
    - never confirmed for this key on this computer: WAITING for the first answer."""
    now = time.time() if now is None else now
    c = load_cache()
    if _usable(c, key, now, GRACE_DAYS * 86400):
        return True, _valid_msg(c.get("expires"), " (licence server unreachable, offline grace active)"), None
    if c and c.get("key") == key:
        return True, CHECKING_MSG, WAIT
    return WAITING, CHECKING_MSG, WAIT


def refresh(key, url, sleep=time.sleep):
    """Background recheck. Returns (ok, message, rejected, follow)."""
    if not url or not key:
        ok, msg, _ = check_full(key, url, sleep=sleep); return ok, msg, False, None
    status, msg = server_check(key, url, sleep)
    if status == OK: return True, msg, False, None
    if status == REJECTED: return False, msg, True, None
    ok, msg, follow = offline_verdict(key)
    return ok, msg, False, follow


def wait_for_server(key, url, cancelled=lambda: False, status=lambda msg: None, sleep=time.sleep, clock=time.monotonic):
    """Keeps asking the server (60 s attempts, waits of 15, 30, 60 s, then every 2 minutes) until it answers.
    Returns (True, msg) when valid, (False, msg) when rejected, or None when cancelled. Never gives up on its own.
    After about HINT_AFTER seconds it calls status(HINT_MSG) once, as a gentle hint (not an error)."""
    start, n, hinted = clock(), 0, False
    while not cancelled():
        st, msg = server_check(key, url, sleep, attempts=1)
        if st == OK: return True, msg
        if st == REJECTED: return False, msg
        if not hinted and clock() - start >= HINT_AFTER:
            hinted = True; status(HINT_MSG)
        wait = WAIT_STEPS[n] if n < len(WAIT_STEPS) else WAIT_EVERY; n += 1
        end = clock() + wait
        while not cancelled() and clock() < end:
            sleep(min(1, end - clock()))
    return None



# ---- plan channel and machine limits (enforced here; the licence API reports them)
# max_channels 0 on a copier plan means no cap. Markets only has no copier at all.
# These fallbacks match the website if an older server omits max_channels / max_machines.
NO_COPIER_PLANS = frozenset({"markets", "markets_only", "markets_year"})
PLAN_LIMITS = {
    "trial": {"max_channels": 3, "max_machines": 1, "copier": True},
    "free": {"max_channels": 3, "max_machines": 1, "copier": True},
    "starter": {"max_channels": 3, "max_machines": 1, "copier": True},
    "pro": {"max_channels": 10, "max_machines": 2, "copier": True},
    "unlimited": {"max_channels": 0, "max_machines": 5, "copier": True},
    "markets": {"max_channels": 0, "max_machines": 0, "copier": False},
    "markets_only": {"max_channels": 0, "max_machines": 0, "copier": False},
    "markets_year": {"max_channels": 0, "max_machines": 0, "copier": False},
}


def _int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def resolve_limits(plan, max_channels=None, max_machines=None):
    """Return {plan, max_channels, max_machines, copier}. max_channels 0 = no cap when copier is True."""
    plan = str(plan or "").strip().lower()
    base = dict(PLAN_LIMITS.get(plan) or {"max_channels": 3, "max_machines": 1, "copier": True})
    if plan in NO_COPIER_PLANS:
        base["copier"] = False
        base["max_channels"] = 0
        base["max_machines"] = 0
        return {"plan": plan or "unknown", **base}
    mc = _int_or_none(max_channels)
    mm = _int_or_none(max_machines)
    if mc is not None:
        base["max_channels"] = mc
    if mm is not None:
        base["max_machines"] = mm
    base["copier"] = True
    return {"plan": plan or "unknown", **base}


def cached_limits():
    c = load_cache() or {}
    return resolve_limits(c.get("plan"), c.get("max_channels"), c.get("max_machines"))


def copyable_channel_ids(enabled_ids, limits=None):
    """Split distinct enabled channel ids into those that may be copied and those that must not.
    enabled_ids stay in the given order (first in, first copied)."""
    lim = limits if limits is not None else cached_limits()
    ids = []
    seen = set()
    for raw in enabled_ids or []:
        k = str(raw)
        if k in seen:
            continue
        seen.add(k)
        ids.append(k)
    if not lim.get("copier"):
        return [], ids, lim
    cap = int(lim.get("max_channels") or 0)
    if cap <= 0:
        return ids, [], lim
    return ids[:cap], ids[cap:], lim


def limit_message(allowed, skipped, limits):
    lim = limits
    plan = lim.get("plan") or "this"
    if not lim.get("copier"):
        return "Markets only does not include the trade copier, so no channels are copied."
    cap = int(lim.get("max_channels") or 0)
    if not skipped:
        return ""
    label = {"starter": "Starter", "pro": "Pro", "unlimited": "Unlimited", "trial": "Trial",
             "free": "Free with Exness"}.get(plan, plan)
    return (f"Your {label} plan allows {cap} Telegram channel" + ("s" if cap != 1 else "")
            + f". Copying the first {len(allowed)} and not copying the other {len(skipped)}.")


def picker_caption(limits=None):
    lim = limits if limits is not None else cached_limits()
    if not lim.get("copier"):
        return "Markets only does not include the trade copier. Ticked channels are not copied."
    cap = int(lim.get("max_channels") or 0)
    if cap > 0:
        return (f"Your plan can copy {cap} Telegram channel" + ("s" if cap != 1 else "")
                + " at a time. If you tick more, only the first ones are copied.")
    return "Your plan has no limit on the number of Telegram channels you can copy."


def check(key, url):
    """Returns (ok: bool, message: str)."""
    return check_full(key, url)[:2]
