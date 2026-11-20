"""Automatic updates: asks the website (or GitHub as a fallback) for the latest version and installs it silently.
Customer data lives in %APPDATA%\\SignalPilot (see config.data_dir), so an update never touches settings, licence or login."""
import json, os, re, subprocess, sys, tempfile, urllib.request
from . import __version__, config as C

OFFICIAL_SITE = "https://signalpilot.vodsfire.co.za"
GITHUB_LATEST = "https://api.github.com/repos/mrvugo-a11y/signalpilot-client/releases/latest"
CHECK_EVERY_MS = 6 * 3600 * 1000


def site_base(cfg=None):
    url = C.licence_url(cfg or {})
    m = re.match(r"(https?://[^/]+)", url)
    return m.group(1) if m else OFFICIAL_SITE


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3]) or (0,)


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": f"SignalPilot/{__version__}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def latest(cfg=None):
    """Returns (version, download_url) or None."""
    base = site_base(cfg)
    try:
        d = _get_json(base + "/api/latest-version")
        if d.get("version"):
            return str(d["version"]).lstrip("v"), d.get("url") or base + "/download/SignalPilot-Setup.exe"
    except Exception:
        pass
    try:
        d = _get_json(GITHUB_LATEST)
        return str(d.get("tag_name", "")).lstrip("v"), base + "/download/SignalPilot-Setup.exe"
    except Exception:
        return None


def newer_available(cfg=None, current=__version__):
    r = latest(cfg)
    return r if r and vtuple(r[0]) > vtuple(current) else None


def download(url, progress=None):
    dest = os.path.join(tempfile.gettempdir(), "SignalPilot-Setup-update.exe"); tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": f"SignalPilot/{__version__}"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk: break
            f.write(chunk)
    if os.path.getsize(tmp) < 100_000:
        os.remove(tmp); raise RuntimeError("The download was incomplete. Please try again later.")
    os.replace(tmp, dest); return dest


DOWNLOAD_PAGE = OFFICIAL_SITE + "/download"
STATE_FILE = "update_state.json"
LOG_NAME = "SignalPilot-update.log"


def _state_path():
    return C.path(STATE_FILE)


def read_state():
    try:
        with open(_state_path(), encoding="utf-8") as f: return json.load(f)
    except Exception:
        return {}


def write_state(d):
    try:
        os.makedirs(C.data_dir(), exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as f: json.dump(d, f)
    except Exception:
        pass


def should_offer(version, current=__version__):
    """Never re-offer a version whose automatic install already failed (the customer gets the manual link instead)."""
    st = read_state()
    return vtuple(version) > vtuple(current) and str(st.get("failed_version") or "") != str(version)


def pending_result(current=__version__):
    """Called at startup. Returns ("failed", version, log_path) once if the last automatic install failed, else None.
    Clears the pending marker; records success so a later newer version is offered again."""
    st = read_state(); ver = st.get("pending_version")
    if not ver: return None
    st.pop("pending_version", None)
    if vtuple(current) >= vtuple(ver):
        st.pop("failed_version", None); write_state(st); return None
    st["failed_version"] = ver; write_state(st)
    return ("failed", ver, st.get("log") or os.path.join(tempfile.gettempdir(), LOG_NAME))


def needs_admin(app_dir=None):
    """True when the running copy sits in a folder this user cannot write (e.g. C:\\Program Files)."""
    app_dir = app_dir or os.path.dirname(os.path.abspath(sys.executable))
    try:
        probe = os.path.join(app_dir, ".sp-write-test"); open(probe, "w").close(); os.remove(probe); return False
    except OSError:
        return True


def installer_args(log_path, admin):
    args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/CLOSEAPPLICATIONS", f'/LOG="{log_path}"']
    args.append("/ALLUSERS" if admin else "/CURRENTUSER")
    return args


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def helper_script(installer, log_path, admin, app_exe, pid, version, state_path):
    """PowerShell that waits for this app to exit, runs the installer (elevated through a real UAC prompt when the
    existing install is in Program Files), checks its exit code and reopens SignalPilot either way."""
    args = " ".join(installer_args(log_path, admin))
    verb = "-Verb RunAs" if admin else ""
    return f"""$ErrorActionPreference = 'Continue'
$inst = {_ps_quote(installer)}; $exe = {_ps_quote(app_exe)}; $state = {_ps_quote(state_path)}
try {{ Wait-Process -Id {int(pid)} -Timeout 60 -ErrorAction SilentlyContinue }} catch {{}}
Start-Sleep -Seconds 1
try {{ Unblock-File -LiteralPath $inst -ErrorAction SilentlyContinue }} catch {{}}
$code = -1
try {{
  $p = Start-Process -FilePath $inst -ArgumentList {_ps_quote(args)} {verb} -Wait -PassThru
  $code = $p.ExitCode
}} catch {{ $code = 1223 }}
try {{ $j = Get-Content -LiteralPath $state -Raw | ConvertFrom-Json }} catch {{ $j = $null }}
if ($j -eq $null) {{ $j = [pscustomobject]@{{}} }}
$j | Add-Member -NotePropertyName exit_code -NotePropertyValue $code -Force
$j | ConvertTo-Json | Set-Content -LiteralPath $state -Encoding UTF8
$new = $exe
if ($code -eq 0) {{
  $k = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{{D72C9711-B6E2-4A42-8CF1-9737549A12EC}}_is1'
  foreach ($root in 'HKCU:','HKLM:') {{
    try {{ $d = (Get-ItemProperty -Path "$root\\$k" -ErrorAction Stop).InstallLocation; if ($d -and (Test-Path (Join-Path $d 'SignalPilot.exe'))) {{ $new = Join-Path $d 'SignalPilot.exe'; break }} }} catch {{}}
  }}
}}
Start-Process -FilePath $new
"""


def launch_installer(path, version="", app_exe=None):
    """Hands the install to a hidden PowerShell helper and returns; the caller then closes the app.
    The helper writes the Inno Setup log to %TEMP%\\SignalPilot-update.log, checks the exit code and reopens the app;
    at the next start pending_result() tells the app whether the update actually landed."""
    if sys.platform != "win32":
        raise RuntimeError("Automatic install only works on Windows.")
    with open(path, "rb") as f:
        if f.read(2) != b"MZ": raise RuntimeError("The downloaded installer is damaged. Please use the download page.")
    app_exe = app_exe or sys.executable
    log_path = os.path.join(tempfile.gettempdir(), LOG_NAME)
    admin = needs_admin(os.path.dirname(os.path.abspath(app_exe)))
    write_state({**read_state(), "pending_version": str(version), "log": log_path, "admin": admin})
    script = os.path.join(tempfile.gettempdir(), "SignalPilot-update.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(helper_script(path, log_path, admin, app_exe, os.getpid(), version, _state_path()))
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script],
                     creationflags=0x08000000 | 0x00000200, close_fds=True)  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
