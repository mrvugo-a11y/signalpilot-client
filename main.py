"""Telegram to MT5 SignalPilot: the customer facing window (setup wizard, settings, status and log)."""
import asyncio, os, queue, sys, threading, tkinter as tk
from tkinter import ttk, messagebox, simpledialog, scrolledtext, filedialog
from datetime import datetime
from copier import config as C, licence, setup_logic as W, settings_schema as S, __version__
from copier.branding import BRAND_NAME, SUPPORT_TEXT

GREEN, RED, GREY, AMBER = "#1a7f37", "#c62828", "#777777", "#b26a00"



# Lines kept out of the customer's activity box (still written to copier.log for support).
_FILE_ONLY_PREFIXES = ("Shared results for", "Sharing channel results failed", "Channel results not shared yet")
def _file_only(line):
    body = line.split("  ", 1)[-1] if line[:2].isdigit() else line
    return any(body.startswith(p) or line.startswith(p) for p in _FILE_ONLY_PREFIXES)

class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ small widgets
def scrollable(parent):
    outer = ttk.Frame(parent)
    canvas = tk.Canvas(outer, highlightthickness=0); sb = ttk.Scrollbar(outer, command=canvas.yview)
    inner = ttk.Frame(canvas)
    canvas.configure(yscrollcommand=sb.set); sb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
    return outer, inner


class FieldRow:
    """One labelled setting: label, input, plain English help. Optional 'own value' tick for channels."""
    def __init__(self, parent, row, field, value, override=None):
        self.key, self.label, self.kind, self.opts, help_ = field
        col = 0; self.override = None
        if override is not None:
            self.override = tk.BooleanVar(value=override)
            ttk.Checkbutton(parent, text="Own value", variable=self.override, command=self._toggle).grid(row=row, column=0, sticky="nw", padx=(0, 6))
            col = 1
        ttk.Label(parent, text=self.label, font=("Segoe UI", 9, "bold")).grid(row=row, column=col, sticky="nw", pady=(6, 0))
        box = ttk.Frame(parent); box.grid(row=row, column=col + 1, sticky="we", pady=(6, 0), padx=6)
        if self.kind == "bool":
            self.var = tk.BooleanVar(value=bool(value)); self.w = ttk.Checkbutton(box, variable=self.var, text="Yes")
            self.w.pack(anchor="w")
        elif self.kind == "choice":
            self.var = tk.StringVar(value=S.choice_label(self.opts, value))
            self.w = ttk.Combobox(box, textvariable=self.var, state="readonly", values=[l for _, l in self.opts], width=40)
            self.w.pack(anchor="w")
        else:
            self.var = tk.StringVar(value=S.to_text(self.kind, value))
            self.w = ttk.Entry(box, textvariable=self.var, width=44, show="*" if self.kind == "secret" else "")
            self.w.pack(side="left", anchor="w")
            if self.kind == "file":
                ttk.Button(box, text="Browse", command=self._browse).pack(side="left", padx=4)
        ttk.Label(parent, text=help_, foreground=GREY, wraplength=430, justify="left").grid(row=row + 1, column=col + 1, sticky="w", padx=6)
        self._toggle()

    def _browse(self):
        p = filedialog.askopenfilename(title="Find terminal64.exe", filetypes=[("MetaTrader 5", "terminal64.exe"), ("Programs", "*.exe")])
        if p: self.var.set(p.replace("/", "\\") if sys.platform == "win32" else p)

    def _toggle(self):
        if self.override is not None:
            st = "normal" if self.override.get() else "disabled"
            if self.kind == "choice" and st == "normal": st = "readonly"
            self.w.configure(state=st)

    def get(self):
        if self.kind == "bool": return bool(self.var.get())
        if self.kind == "choice": return S.choice_value(self.opts, self.var.get())
        return S.from_text(self.kind, self.var.get(), self.label)


def field_grid(parent, fields, values, overrides=None):
    parent.columnconfigure(2 if overrides is not None else 1, weight=1)
    rows = {}
    for i, f in enumerate(fields):
        ov = None if overrides is None else (f[0] in overrides)
        val = overrides[f[0]] if overrides is not None and f[0] in overrides else values.get(f[0])
        rows[f[0]] = FieldRow(parent, i * 2, f, val, ov)
    return rows


# ------------------------------------------------------------------ channel results
class ResultsWindow(tk.Toplevel):
    """Profit and loss of closed trades per signal channel, read from MetaTrader 5 history."""
    def __init__(self, app):
        super().__init__(app.root); self.app = app
        self.title("Channel results"); self.geometry("900x440"); self.transient(app.root)
        from copier import results as R
        self.R = R
        top = ttk.Frame(self, padding=(12, 10, 12, 4)); top.pack(fill="x")
        ttk.Label(top, text="Period").pack(side="left")
        self.period = tk.StringVar(value="7 days")
        cb = ttk.Combobox(top, textvariable=self.period, values=[p for p, _ in R.PERIODS], state="readonly", width=10)
        cb.pack(side="left", padx=6); cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left")
        self.info = ttk.Label(top, text="", foreground=GREY); self.info.pack(side="right")
        cols = (("name", "Channel", 200), ("source", "Source", 130), ("profit", "Profit", 80), ("loss", "Loss", 80), ("net", "Net", 80),
                ("pips", "Pips", 80), ("trades", "Trades", 60), ("win", "Win rate", 70))
        self.tree = ttk.Treeview(self, columns=[c for c, _, _ in cols], show="headings")
        for c, t, w in cols:
            self.tree.heading(c, text=t); self.tree.column(c, width=w, anchor="w" if c == "name" else "e")
        self.tree.tag_configure("pos", foreground=GREEN); self.tree.tag_configure("neg", foreground=RED)
        self.tree.tag_configure("tracked", background="#eef3fb")
        self.tree.pack(fill="both", expand=True, padx=12, pady=4)
        ttk.Label(self, text="Closed trades only, including swap and commission. Trades copied before version 1.2.8 "
                  "are matched while their signal is still remembered; older ones show as Not tagged. Pips count each signal once. "
                  "Rows marked Tracked, not copied are channels you follow but do not copy: their signals are followed on paper "
                  "with live prices (result at TP1, the stop loss, a close message or after 7 days) and no trade is opened.",
                  foreground=GREY, wraplength=690, padding=(12, 0, 12, 8)).pack(anchor="w")
        if C.load().get("share_results", True):
            ttk.Label(self, text="Your channel results (no account details) are shared with SignalPilot to rank signal providers. "
                      "You can switch this off in Settings.", foreground=GREY, wraplength=690, padding=(12, 0, 12, 8)).pack(anchor="w")
        self.refresh()

    def refresh(self):
        self.info.configure(text="Loading...")
        period = dict(self.R.PERIODS)[self.period.get()]
        def run():
            from copier.executor import Executor
            cfg = C.load(); own = None
            try:
                eng = self.app.engine
                lock = eng.mt5_lock if eng and eng.running() else None
                if lock is None:
                    own = Executor(cfg, lambda m: None); own.connect()
                    rows, cur = self.R.fetch(cfg, period)
                else:
                    with lock: rows, cur = self.R.fetch(cfg, period)
                self.app.q.put(("call", lambda: self.show(rows, cur)))
                if self.R.missing_names(rows):     # look up titles Telegram knows, then show them (still off the UI thread)
                    from copier import engine as E
                    from copier.store import Store
                    if self.R.backfill_names(cfg, rows, lambda ids: E.resolve_titles(cfg, ids),
                                              (eng.store if eng and eng.running() and getattr(eng, "store", None) else Store())):
                        self.app.q.put(("call", lambda: self.show(rows, cur)))
            except Exception as e:
                msg = f"Could not read results: {e}"
                self.app.q.put(("call", lambda: self.info.winfo_exists() and self.info.configure(text=msg)))
            finally:
                if own: own.shutdown()
        threading.Thread(target=run, daemon=True).start()

    def show(self, rows, cur):
        if not self.winfo_exists(): return
        self.tree.delete(*self.tree.get_children())
        f = lambda v: "" if v is None else f"{v:,.2f}"
        fp = lambda v: "" if v is None else f"{v:+,.1f}"
        for r in rows:
            tracked = r.get("source") == "tracked"
            score = r["pips"] if tracked else r["net"]
            tags = (("pos",) if (score or 0) > 0 else ("neg",) if (score or 0) < 0 else ()) + (("tracked",) if tracked else ())
            self.tree.insert("", "end", values=(r["name"], "Tracked, not copied" if tracked else "Copied", f(r["profit"]), f(r["loss"]),
                                                f(r["net"]), fp(r.get("pips")), r["trades"], f"{r['win_rate']:.0f}%"), tags=tags)
        self.info.configure(text=f"Amounts in {cur or 'account currency'}" if rows else "No closed trades in this period.")


# ------------------------------------------------------------------ channel picker
class ChannelPicker(ttk.Frame):
    def __init__(self, parent, chans, current):
        super().__init__(parent)
        top = ttk.Frame(self); top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.q = tk.StringVar(); e = ttk.Entry(top, textvariable=self.q); e.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(top, text="Tick all shown", command=lambda: self._all(True)).pack(side="left")
        ttk.Button(top, text="Untick all", command=lambda: self._all(False)).pack(side="left", padx=4)
        outer, self.inner = scrollable(self); outer.pack(fill="both", expand=True, pady=4)
        self.chans = chans; self.vars = {}; self.rows = {}
        for cid, name in chans:
            v = tk.BooleanVar(value=str(cid) in current and current[str(cid)].get("enabled", True))
            self.vars[cid] = v; self.rows[cid] = ttk.Checkbutton(self.inner, text=name, variable=v)
        self.q.trace_add("write", lambda *a: self._filter()); self._filter()

    def _filter(self):
        t = self.q.get().lower()
        for cid, name in self.chans:
            self.rows[cid].pack_forget()
            if t in name.lower(): self.rows[cid].pack(anchor="w")

    def _all(self, on):
        t = self.q.get().lower()
        for cid, name in self.chans:
            if not on or t in name.lower(): self.vars[cid].set(on)

    def selected(self):
        return [cid for cid, v in self.vars.items() if v.get()]


# ------------------------------------------------------------------ setup wizard
class Wizard(tk.Toplevel):
    TITLES = {"welcome": "Welcome", "licence": "Your licence", "api": "Telegram app details", "telegram": "Log in to Telegram",
              "channels": "Pick your signal channels", "mt5": "Find MetaTrader 5", "done": "All set"}

    def __init__(self, app):
        super().__init__(app.root); self.app = app
        self.title(f"{BRAND_NAME} setup"); self.geometry("620x520"); self.transient(app.root); self.grab_set()
        self.cfg = C.load(); self.steps = W.wizard_steps(self.cfg); self.i = 0
        self.chans = []; self.mt5_info = None; self.busy = False
        self.head = ttk.Label(self, font=("Segoe UI", 14, "bold")); self.head.pack(anchor="w", padx=16, pady=(14, 2))
        self.steplbl = ttk.Label(self, foreground=GREY); self.steplbl.pack(anchor="w", padx=16)
        self.body = ttk.Frame(self, padding=16); self.body.pack(fill="both", expand=True)
        self.msg = ttk.Label(self, text="", wraplength=580, foreground=RED); self.msg.pack(anchor="w", padx=16)
        nav = ttk.Frame(self, padding=12); nav.pack(fill="x")
        self.b_back = ttk.Button(nav, text="Back", command=self.back); self.b_back.pack(side="left")
        self.b_next = ttk.Button(nav, text="Next", command=self.next); self.b_next.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.cancel); self.show()

    @property
    def step(self): return self.steps[self.i]

    def cancel(self):
        if self.busy: return
        if messagebox.askyesno("Leave setup", "Leave the setup for now? You can finish it later with the Setup button.", parent=self):
            self.destroy()

    def say(self, text, colour=RED): self.msg.configure(text=text, foreground=colour)

    def set_busy(self, on, text=""):
        self.busy = on
        for b in (self.b_back, self.b_next): b.configure(state="disabled" if on else "normal")
        if on: self.say(text, GREY)

    def worker(self, fn, done):
        """Run fn() in a thread, then done(result, error) back in the window."""
        def run():
            try: r, e = fn(), None
            except Exception as ex: r, e = None, ex
            self.app.q.put(("call", lambda: done(r, e) if self.winfo_exists() else None))
        threading.Thread(target=run, daemon=True).start()

    def text(self, t, **kw):
        ttk.Label(self.body, text=t, wraplength=570, justify="left", **kw).pack(anchor="w", pady=(0, 8))

    def show(self):
        for w in self.body.winfo_children(): w.destroy()
        self.say("")
        self.head.configure(text=self.TITLES[self.step]); self.steplbl.configure(text=f"Step {self.i + 1} of {len(self.steps)}")
        self.b_back.configure(state="normal" if self.i else "disabled")
        self.b_next.configure(text="Finish" if self.step == "done" else "Next")
        getattr(self, "page_" + self.step)()

    def back(self):
        if self.i: self.i -= 1; self.show()

    def go(self):
        self.i += 1; C.save(self.cfg); self.show()

    def next(self):
        getattr(self, "next_" + self.step)()

    # -- pages
    def page_welcome(self):
        self.text(f"Thank you for choosing {BRAND_NAME}. This short setup takes about two minutes.")
        self.text("You will need:\n\n   1.  Your licence key (on your account page on our website)\n"
                  "   2.  Your phone with Telegram on it, to receive a login code\n"
                  "   3.  MetaTrader 5 open and logged in to your trading account")
        self.text("Tip: try it on a demo account first.", foreground=GREY)

    def next_welcome(self): self.go()

    def page_licence(self):
        if C.licence_url(self.cfg):
            self.text("Paste your licence key below. You find it on your account page on our website.")
        else:
            self.text("This copy runs as a free 7 day trial. If you already have a licence key you can paste it, otherwise just click Next.")
        self.key = tk.StringVar(value=self.cfg.get("licence_key", ""))
        e = ttk.Entry(self.body, textvariable=self.key, width=50, font=("Consolas", 11)); e.pack(anchor="w"); e.focus_set()

    def next_licence(self):
        key = self.key.get().strip(); self.set_busy(True, "Checking your licence. This can take up to a minute...")
        def done(r, e):
            self.set_busy(False)
            if e: return self.say(f"Could not check the licence: {e}")
            ok, msg = r
            if not ok: return self.say(msg)
            self.cfg["licence_key"] = key; self.app.set_status("licence", True, msg); self.go()
        self.worker(lambda: W.check_licence_key(self.cfg, key), done)

    def page_api(self):
        self.text("This copy did not come with Telegram app details built in, so please create your own once (free):")
        self.text("   1.  Go to my.telegram.org and log in with your phone number.\n"
                  "   2.  Click API development tools.\n   3.  Type any app name, choose Desktop, click Create.\n"
                  "   4.  Copy App api_id and App api_hash into the boxes below.")
        f = ttk.Frame(self.body); f.pack(anchor="w")
        self.api_id = tk.StringVar(value=str(self.cfg.get("telegram_api_id") or "")); self.api_hash = tk.StringVar(value=self.cfg.get("telegram_api_hash", ""))
        ttk.Label(f, text="API ID").grid(row=0, column=0, sticky="w"); ttk.Entry(f, textvariable=self.api_id, width=20).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(f, text="API hash").grid(row=1, column=0, sticky="w"); ttk.Entry(f, textvariable=self.api_hash, width=40).grid(row=1, column=1, sticky="w", pady=3)

    def next_api(self):
        i, h, err = W.check_api_credentials(self.api_id.get(), self.api_hash.get())
        if err: return self.say(err)
        self.cfg["telegram_api_id"], self.cfg["telegram_api_hash"] = i, h; self.go()

    def page_telegram(self):
        self.text("Type the phone number of your Telegram account, starting with + and your country code.")
        self.text("Telegram will then send a login code to your Telegram app. A box will pop up for you to type it in, "
                  "and a second box for your two step password if you have one.", foreground=GREY)
        self.phone = tk.StringVar(value=self.cfg.get("telegram_phone", ""))
        e = ttk.Entry(self.body, textvariable=self.phone, width=24, font=("Segoe UI", 12)); e.pack(anchor="w"); e.focus_set()
        ttk.Label(self.body, text="Example: +27821234567", foreground=GREY).pack(anchor="w")
        self.text("Your login stays on this computer only. We never see your messages.", foreground=GREY)

    def next_telegram(self):
        phone, err = W.normalise_phone(self.phone.get())
        if err: return self.say(err)
        if self.app.engine and self.app.engine.running(): self.app.stop()
        self.cfg["telegram_phone"] = phone; C.save(self.cfg)
        self.set_busy(True, "Connecting to Telegram... watch your Telegram app for the login code.")
        from copier.engine import login_and_list
        cfg = dict(self.cfg)
        def done(r, e):
            self.set_busy(False)
            if isinstance(e, Cancelled): return self.say("Login cancelled. Click Next to try again.")
            if e:
                from copier.engine import friendly_error
                return self.say(f"Telegram login did not work: {friendly_error(e)}")
            name, self.chans = r; self.app.set_status("telegram", True, f"Logged in as {name}")
            self.app.log(f"Logged in to Telegram as {name}."); self.go()
        self.worker(lambda: login_and_list(cfg, lambda p, s: self.app.ask(p, s, parent=self), phone), done)

    def page_channels(self):
        if not self.chans:
            self.text("No channels were loaded. Go back and log in to Telegram again."); return
        self.text("Tick the channels whose signals you want to copy. You can change this later with the Channels button.")
        self.picker = ChannelPicker(self.body, self.chans, self.cfg.get("channels", {})); self.picker.pack(fill="both", expand=True)

    def next_channels(self):
        sel = self.picker.selected() if self.chans else []
        if not sel: return self.say("Please tick at least one channel.")
        W.apply_channel_selection(self.cfg, self.chans, sel); self.go()

    def page_mt5(self):
        self.text("Open MetaTrader 5 and log in to your trading account. The copier looks for it now.")
        self.mt5lbl = ttk.Label(self.body, text="Looking for MetaTrader 5...", wraplength=560, font=("Segoe UI", 10, "bold")); self.mt5lbl.pack(anchor="w", pady=8)
        ttk.Button(self.body, text="Try again", command=self.find_mt5).pack(anchor="w")
        self.text("\nIn MetaTrader 5 make sure the Algo Trading button on the toolbar is green, and under Tools, Options, Expert Advisors "
                  "the box Allow algorithmic trading is ticked.", foreground=GREY)
        self.find_mt5()

    def find_mt5(self):
        self.mt5lbl.configure(text="Looking for MetaTrader 5...", foreground=GREY)
        def done(r, e):
            if not self.winfo_exists() or self.step != "mt5": return
            self.mt5_info = r or {"ok": False, "message": str(e)}
            ok = self.mt5_info["ok"]; algo = self.mt5_info.get("algo", True)
            self.mt5lbl.configure(text=self.mt5_info["message"], foreground=(GREEN if algo else AMBER) if ok else RED)
            self.app.set_status("mt5", ok, f"Account {r['login']} at {r.get('broker') or r['server']}" if ok else "Not found")
        self.worker(lambda: W.detect_mt5(self.cfg), done)

    def next_mt5(self):
        if not (self.mt5_info and self.mt5_info["ok"]):
            if not messagebox.askyesno("MetaTrader 5 not found", "MetaTrader 5 was not found yet. Continue anyway? "
                                       "You can open it later before you click Start.", parent=self): return
        self.go()

    def page_done(self):
        n = len(self.cfg.get("channels", {}))
        self.text(f"Setup is complete. {n} channel(s) will be copied.")
        self.text("Your trades use the default settings: 0.01 lots per trade, one trade per take profit. "
                  "Change this any time with the Settings button.")
        self.startnow = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.body, text="Start copying now", variable=self.startnow).pack(anchor="w", pady=8)

    def next_done(self):
        W.finish(self.cfg, self.mt5_info); C.save(self.cfg); start = self.startnow.get()
        self.destroy(); self.app.log("Setup finished.")
        if start: self.app.start()


# ------------------------------------------------------------------ settings window
class SettingsWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root); self.app = app; self.cfg = C.load()
        self.title(f"{BRAND_NAME} settings"); self.geometry("780x620"); self.transient(app.root)
        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.groups = {}
        def tab(title, fields, values, intro):
            outer, inner = scrollable(nb); nb.add(outer, text=title)
            ttk.Label(inner, text=intro, wraplength=700, foreground=GREY).grid(row=0, column=0, columnspan=3, sticky="w", pady=(4, 6), padx=4)
            g = ttk.Frame(inner, padding=4); g.grid(row=1, column=0, columnspan=3, sticky="we"); inner.columnconfigure(0, weight=1)
            return field_grid(g, fields, values)
        self.groups["trade"] = tab("Trading", [f for f in S.TRADE_FIELDS if f[0] != "enabled"], self.cfg["default_settings"],
                                   "These settings apply to every channel, unless a channel has its own value on the Channels tab.")
        self.groups["general"] = tab("General", S.GENERAL_FIELDS, self.cfg, "Your licence, Telegram number and how the window behaves.")
        self.groups["mt5"] = tab("MetaTrader 5", S.MT5_FIELDS, self.cfg, "Normally you can leave all of these empty: the copier uses the MetaTrader 5 that is open.")
        if C.needs_api_credentials(self.cfg) or self.cfg.get("telegram_api_id"):
            self.groups["api"] = tab("Telegram app", S.API_FIELDS, self.cfg, "Your own Telegram app details from my.telegram.org.")
        self._channels_tab(nb)
        bar = ttk.Frame(self, padding=8); bar.pack(fill="x")
        ttk.Button(bar, text="Save", command=self.save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        ttk.Label(bar, text="Changes apply straight away, even while copying.", foreground=GREY).pack(side="left")

    def _channels_tab(self, nb):
        f = ttk.Frame(nb, padding=6); nb.add(f, text="Channels")
        self.chcfg = {k: dict(v, settings=dict(v.get("settings") or {})) for k, v in self.cfg.get("channels", {}).items()}
        if not self.chcfg:
            ttk.Label(f, text="No channels yet. Click Channels in the main window to pick some.").pack(anchor="w"); return
        ttk.Label(f, text="Give one channel its own settings. Tick Own value next to a setting to override the Trading tab for this channel only.",
                  wraplength=720, foreground=GREY).pack(anchor="w")
        top = ttk.Frame(f); top.pack(fill="x", pady=6)
        ttk.Label(top, text="Channel:").pack(side="left")
        self.names = {f"{v.get('name', k)}": k for k, v in self.chcfg.items()}
        self.chsel = tk.StringVar(value=next(iter(self.names)))
        cb = ttk.Combobox(top, textvariable=self.chsel, values=list(self.names), state="readonly", width=50); cb.pack(side="left", padx=6)
        self.chon = tk.BooleanVar(); ttk.Checkbutton(top, text="Copy this channel", variable=self.chon).pack(side="left", padx=8)
        self.chtrack = tk.BooleanVar(); ttk.Checkbutton(top, text="Track pips when not copying", variable=self.chtrack).pack(side="left")
        outer, self.chinner = scrollable(f); outer.pack(fill="both", expand=True)
        self.chrows = None; self.chcur = None
        cb.bind("<<ComboboxSelected>>", lambda e: self._show_channel()); self._show_channel()

    def _collect_channel(self):
        """Store what is on screen for the current channel. Raises FieldError."""
        if not self.chcur: return
        over = {k: r.get() for k, r in self.chrows.items() if r.override.get()}
        self.chcfg[self.chcur]["settings"] = over; self.chcfg[self.chcur]["enabled"] = bool(self.chon.get())
        self.chcfg[self.chcur]["track"] = bool(self.chtrack.get())

    def _show_channel(self):
        try: self._collect_channel()
        except S.FieldError as e: messagebox.showerror("Check this value", str(e), parent=self); return
        self.chcur = self.names[self.chsel.get()]; ch = self.chcfg[self.chcur]
        self.chon.set(ch.get("enabled", True)); self.chtrack.set(ch.get("track", True))
        for w in self.chinner.winfo_children(): w.destroy()
        g = ttk.Frame(self.chinner, padding=4); g.pack(fill="x")
        defaults = self.cfg["default_settings"]
        self.chrows = field_grid(g, [f for f in S.TRADE_FIELDS if f[0] != "enabled"], defaults, ch["settings"])

    def save(self):
        try:
            new = C.load()
            for k, r in self.groups["trade"].items(): new["default_settings"][k] = r.get()
            for grp in ("general", "mt5", "api"):
                for k, r in self.groups.get(grp, {}).items(): new[k] = r.get()
            if self.chcfg: self._collect_channel()
        except S.FieldError as e:
            messagebox.showerror("Check this value", str(e), parent=self); return
        probs = S.validate_trade_settings(new["default_settings"])
        for ch in self.chcfg.values():
            probs += [f"{ch.get('name')}: {p}" for p in S.validate_trade_settings(dict(new["default_settings"], **ch["settings"]))]
        if probs: messagebox.showerror("Check these values", "\n".join(probs), parent=self); return
        new["channels"] = self.chcfg
        C.save(new); self.destroy(); self.app.settings_saved(new)


# ------------------------------------------------------------------ main window
class App:
    def __init__(self, root):
        self.root = root; root.title(f"{BRAND_NAME}  v{__version__}"); root.geometry("860x560"); root.minsize(640, 420)
        self.q = queue.Queue(); self.engine = None; self.tray = None
        self._starting = False; self._restart_pending = False   # only one start or restart at a time
        try: ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError: pass
        head = ttk.Frame(root, padding=(12, 10)); head.pack(fill="x")
        ttk.Label(head, text=BRAND_NAME, font=("Segoe UI", 16, "bold")).pack(side="left")
        self.state_lbl = ttk.Label(head, text="Stopped", font=("Segoe UI", 12, "bold"), foreground=RED); self.state_lbl.pack(side="right")
        st = ttk.Frame(root, padding=(12, 0)); st.pack(fill="x")
        self.lights = {}
        for i, (k, name) in enumerate((("licence", "Licence"), ("telegram", "Telegram"), ("mt5", "MetaTrader 5"))):
            box = ttk.Frame(st, relief="groove", padding=8); box.grid(row=0, column=i, sticky="we", padx=(0, 8)); st.columnconfigure(i, weight=1)
            dot = tk.Label(box, text="●", fg=GREY, font=("Segoe UI", 14)); dot.pack(side="left")
            ttk.Label(box, text=name, font=("Segoe UI", 10, "bold")).pack(side="left", padx=4)
            txt = ttk.Label(box, text="Not checked", foreground=GREY); txt.pack(side="left", padx=4)
            self.lights[k] = (dot, txt)
        bar = ttk.Frame(root, padding=12); bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="▶  Start copying", command=self.start); self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="■  Stop", command=self.stop, state="disabled"); self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="Channels", command=self.select_channels).pack(side="left", padx=(18, 0))
        ttk.Button(bar, text="Channel results", command=lambda: ResultsWindow(self)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Settings", command=lambda: SettingsWindow(self)).pack(side="left", padx=6)
        ttk.Button(bar, text="Setup", command=lambda: Wizard(self)).pack(side="left")
        ttk.Button(bar, text="Open data folder", command=self.open_data).pack(side="right")
        ttk.Label(root, text="Recent signals and trades", padding=(12, 0)).pack(anchor="w")
        self.logbox = scrolledtext.ScrolledText(root, state="disabled", font=("Segoe UI", 9), wrap="word", height=12)
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(2, 4))
        for tag, col in (("trade", GREEN), ("signal", "#1565c0"), ("bad", RED), ("warn", AMBER)):
            self.logbox.tag_configure(tag, foreground=col)
        ttk.Label(root, text=SUPPORT_TEXT, foreground=GREY, padding=(12, 0, 12, 8)).pack(anchor="w")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<Unmap>", self._on_minimise)
        root.after(200, self.pump)
        cfg = C.load()
        self.log(f"Welcome to {BRAND_NAME}. Your files are kept in {C.data_dir()}")
        if W.needs_setup(cfg): root.after(400, lambda: Wizard(self))
        else:
            self.check_licence_async()
            if cfg.get("start_copying_on_launch"): root.after(800, self.start)
        root.after(1500, self.report_update_result)
        root.after(5000, self.check_updates)

    # -- automatic updates
    def check_updates(self):
        from copier import updater
        def run():
            try: r = updater.newer_available(C.load())
            except Exception: r = None
            if r and updater.should_offer(r[0]): self.q.put(("call", lambda: self.offer_update(*r)))
        if not getattr(self, "_update_offered", False):
            threading.Thread(target=run, daemon=True).start()
        self.root.after(updater.CHECK_EVERY_MS, self.check_updates)

    def offer_update(self, version, url):
        if getattr(self, "_update_offered", False): return
        self._update_offered = True
        if not messagebox.askyesno("Update", f"A new version is ready ({version}). Update now?\n\n"
                                   "Your settings, licence and Telegram login are kept.", parent=self.root):
            self._update_offered = False; return
        self.log(f"Downloading {BRAND_NAME} {version}...")
        from copier import updater
        def run():
            try:
                path = updater.download(url)
                self.q.put(("call", lambda: self._install_update(path, version)))
            except Exception as e:
                self._update_offered = False
                self.log(f"Update download failed: {e}")
        threading.Thread(target=run, daemon=True).start()

    def report_update_result(self):
        from copier import updater
        try: r = updater.pending_result()
        except Exception: r = None
        if not r: return
        _, ver, log_path = r
        self.log(f"The automatic update to {ver} did not install. Details: {log_path}")
        if messagebox.askyesno("Update", f"The automatic update to version {ver} could not be installed.\n\n"
                               "Please download the installer and run it once. Your settings, licence and Telegram "
                               "login are kept.\n\nOpen the download page now?\n" + updater.DOWNLOAD_PAGE, parent=self.root):
            import webbrowser; webbrowser.open(updater.DOWNLOAD_PAGE)

    def _install_update(self, path, version=""):
        from copier import updater
        try:
            updater.launch_installer(path, version)
        except Exception as e:
            self._update_offered = False; self.log(f"Could not start the update: {e}")
            if messagebox.askyesno("Update", f"The update could not be started ({e}).\n\nOpen the download page?\n"
                                   + updater.DOWNLOAD_PAGE, parent=self.root):
                import webbrowser; webbrowser.open(updater.DOWNLOAD_PAGE)
            return
        self.log("Installing the update. SignalPilot will reopen by itself in a moment.")
        if self.engine and self.engine.running(): self.engine.stop()
        if self.tray: self.tray.stop()
        self._telegram_shutdown()
        global _instance_handle
        if sys.platform == "win32" and _instance_handle:
            try: import ctypes; ctypes.windll.kernel32.CloseHandle(_instance_handle); _instance_handle = None
            except Exception: pass
        self.root.after(800, self.root.destroy)

    # -- thread safe helpers
    def log(self, msg): self.q.put(("log", f"{datetime.now():%H:%M:%S}  {msg}"))

    def set_status(self, kind, ok, text): self.q.put(("status", (kind, ok, text)))

    def ask(self, prompt, secret=False, parent=None):
        """Called from worker threads: shows a dialog and blocks until the customer answers."""
        box = queue.Queue(); self.q.put(("ask", (prompt, secret, box, parent))); ans = box.get()
        if ans is None: raise Cancelled("cancelled")
        return ans

    def pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log": self._write_log(val)
                elif kind == "status":
                    k, ok, text = val; dot, lbl = self.lights[k]
                    col = GREY if ok is None else GREEN if ok else RED
                    dot.configure(fg=col); lbl.configure(text=text, foreground=col if ok is False else "")
                elif kind == "ask":
                    prompt, secret, box, parent = val
                    box.put(simpledialog.askstring("Telegram login", prompt, show="*" if secret else None, parent=parent or self.root))
                elif kind == "call":
                    try: val()
                    except Exception as e: self._write_log(f"Something went wrong: {e}")
        except queue.Empty:
            pass
        if self.engine and not self.engine.running() and str(self.btn_stop["state"]) == "normal":
            self.set_running(False)
        self.root.after(200, self.pump)

    def _write_log(self, line):
        u = line.upper()
        tag = "trade" if "TRADE " in u else "signal" if "SIGNAL" in u else "bad" if any(w in u for w in ("PROBLEM", "REJECT", "COULD NOT", "FAILED")) \
            else "warn" if any(w in u for w in ("WARNING", "SKIPPED")) else None
        if not _file_only(line):
            self.logbox.configure(state="normal"); self.logbox.insert("end", line + "\n", tag)
            if int(self.logbox.index("end").split(".")[0]) > 1500: self.logbox.delete("1.0", "500.0")
            self.logbox.see("end"); self.logbox.configure(state="disabled")
        try:
            p = C.path("copier.log")
            if os.path.exists(p) and os.path.getsize(p) > 5_000_000: os.replace(p, p + ".old")
            with open(p, "a", encoding="utf-8") as f: f.write(line + "\n")
        except OSError: pass

    # -- licence / buttons
    def check_licence_async(self, then=None):
        self.set_status("licence", None, "Checking...")
        def run():
            cfg = C.load(); key, url = cfg.get("licence_key", ""), C.licence_url(cfg)
            ok, msg, follow = licence.check_full(key, url)
            if ok is licence.WAITING:     # never confirmed on this computer: wait for the first answer
                self.q.put(("call", lambda: self._wait_for_licence(key, url, then, blocking=True))); return
            self._licence_result(ok, msg, then)
            if follow == licence.WAIT: self.q.put(("call", lambda: self._wait_for_licence(key, url)))
            elif follow == licence.REFRESH: self._licence_refresh(key, url)
        threading.Thread(target=run, daemon=True).start()
        if not getattr(self, "_licence_timer", False):
            self._licence_timer = True
            self.root.after(licence.RECHECK_HOURS * 3600 * 1000, self._licence_recheck)

    def _licence_result(self, ok, msg, then=None):
        self.set_status("licence", ok, msg if len(msg) < 60 else msg[:57] + "...")
        if not ok: self.log(f"Licence problem: {msg}")
        if then: self.q.put(("call", lambda: then(ok, msg)))

    def _wait_for_licence(self, key, url, then=None, blocking=False):
        """Server asleep or no internet: retry calmly in the background until it answers.
        blocking=True (key never confirmed on this computer): Start waits (Stop cancels) and copying starts by itself once valid.
        blocking=False (confirmed before): the app stays fully usable; only an actual rejection stops copying."""
        w = getattr(self, "_licence_waiting", None)
        if w:
            if then: w["then"] = then
            if blocking and not w["blocking"]:
                w["blocking"] = True; self.btn_start["state"] = "disabled"; self.btn_stop["state"] = "normal"
            return
        w = self._licence_waiting = {"cancel": False, "then": then, "blocking": blocking}
        self.set_status("licence", None, licence.CHECKING_MSG); self.log("Checking your licence in the background. This can take a minute if the server is waking up.")
        if blocking: self.btn_start["state"] = "disabled"; self.btn_stop["state"] = "normal"
        def status(msg): self.set_status("licence", None, "Still checking..."); self.log(msg)
        def run():
            r = licence.wait_for_server(key, url, cancelled=lambda: w["cancel"], status=status)
            def done():
                self._licence_waiting = None
                if r is None:
                    if w["blocking"]: self._starting = False
                    self.set_status("licence", None, "Not checked"); self.log("Stopped waiting for the licence check.")
                    if w["blocking"] and not (self.engine and self.engine.running()): self.set_running(False)
                    return
                if w["blocking"] and not (self.engine and self.engine.running()): self.set_running(False)
                self._licence_result(r[0], r[1], w["then"])
                if not r[0]:
                    if self.engine and self.engine.running():
                        self.stop(); self.log("Copying stopped because of the licence.")
                    if not w["then"]: messagebox.showerror("Licence", r[1], parent=self.root)
            self.q.put(("call", done))
        threading.Thread(target=run, daemon=True).start()

    def _licence_recheck(self):
        """Every RECHECK_HOURS while open, so long running copiers notice revocation or expiry."""
        def run():
            cfg = C.load(); self._licence_refresh(cfg.get("licence_key", ""), C.licence_url(cfg))
        threading.Thread(target=run, daemon=True).start()
        self.root.after(licence.RECHECK_HOURS * 3600 * 1000, self._licence_recheck)

    def _licence_refresh(self, key, url):
        """Quiet server check (worker thread). Stops copying only on an explicit rejection; beyond the grace it keeps waiting calmly."""
        ok, msg, rejected, follow = licence.refresh(key, url)
        if follow == licence.WAIT:
            self.q.put(("call", lambda: self._wait_for_licence(key, url))); return
        self.set_status("licence", ok, msg if len(msg) < 60 else msg[:57] + "...")
        if ok: return
        self.log(f"Licence problem: {msg}")
        def block():
            if self.engine and self.engine.running():
                self.stop(); self.log("Copying stopped because of the licence.")
            messagebox.showerror("Licence", msg, parent=self.root)
        self.q.put(("call", block))

    def set_running(self, on):
        self.btn_start["state"] = "disabled" if on else "normal"
        self.btn_stop["state"] = "normal" if on else "disabled"
        self.state_lbl.configure(text="Copying" if on else "Stopped", foreground=GREEN if on else RED)

    def start(self):
        """Never runs two copiers: refuses while one runs AND while a start is still on its way
        (the licence check runs before the copier exists)."""
        if self.engine and self.engine.running(): return
        if getattr(self, "_starting", False): return
        cfg = C.load()
        if W.needs_setup(cfg):
            messagebox.showinfo("Setup", "Please finish the setup first."); Wizard(self); return
        self._starting = True
        self.btn_start["state"] = "disabled"; self.log("Checking your licence...")
        def go(ok, msg):
            self._starting = False
            if not ok:
                self.btn_start["state"] = "normal"; messagebox.showerror("Licence", msg); return
            if self.engine and self.engine.running(): return
            from copier.engine import Engine
            self.engine = Engine(self.log, self.ask, self.set_status); self.engine.start(); self.set_running(True)
            self.log("Starting... connecting to MetaTrader 5 and Telegram.")
        self.check_licence_async(go)

    def stop(self):
        w = getattr(self, "_licence_waiting", None)
        if w and w["blocking"] and not w["cancel"]:
            w["cancel"] = True; w["then"] = None; self._starting = False
        self._restart_pending = False            # Stop also cancels a restart that is still waiting
        if self.engine and self.engine.running(): self.engine.stop(); self.log("Stopping...")

    def settings_saved(self, cfg):
        self.log("Settings saved.")
        if self.engine and self.engine.running():
            old = self.engine.channel_ids(); self.engine.reload_settings(cfg)
            if self.engine.channel_ids() != old:
                self.log("The channel list changed, restarting the copier to apply it."); self.restart()
            else:
                self.log("New settings are used from the next signal.")

    def restart(self):
        """Overlapping restarts (for example saving the channel list twice quickly) collapse into one.
        The single restart reads the newest settings when it starts."""
        if getattr(self, "_restart_pending", False): return
        self._restart_pending = True
        eng = self.engine
        if eng: eng.stop()
        def wait():
            if not self._restart_pending: return            # Stop was clicked meanwhile
            if (eng and eng.running()) or getattr(self, "_starting", False):
                self.root.after(300, wait); return
            self._restart_pending = False; self.start()
        self.root.after(300, wait)

    def open_data(self):
        import subprocess
        if sys.platform == "win32": os.startfile(C.data_dir())
        else: subprocess.Popen(["xdg-open", C.data_dir()])

    def select_channels(self):
        cfg = C.load()
        if C.needs_api_credentials(cfg) or not cfg.get("telegram_phone"):
            Wizard(self); return
        self.log("Loading your Telegram channels...")
        def work():
            try:
                from copier.engine import safe_list_channels
                chans = safe_list_channels(cfg, self.ask, self.log)
                if chans is not None: self.q.put(("call", lambda: self.channel_window(chans)))
            except BaseException as e:      # never let the Channels path take the app down
                self.log(f"Could not load channels: {e}")
        threading.Thread(target=work, daemon=True).start()

    def channel_window(self, chans):
        """Channels work while copying (one shared Telegram client). Copying only restarts if the ticked list changed."""
        try:
            cfg = C.load()
            w = tk.Toplevel(self.root); w.title("Tick the channels to copy"); w.geometry("480x540"); w.transient(self.root)
            p = ChannelPicker(w, chans, cfg.get("channels", {})); p.pack(fill="both", expand=True, padx=8, pady=8)
            def save():
                try:
                    W.apply_channel_selection(cfg, chans, p.selected()); C.save(cfg)
                    self.log(f"Saved {len(cfg['channels'])} channel(s)."); w.destroy()
                    if self.engine and self.engine.running(): self.settings_saved(cfg)
                except Exception as e:
                    self.log(f"Could not save channels: {e}")
            w.protocol("WM_DELETE_WINDOW", w.destroy)
            ttk.Button(w, text="Save", command=save).pack(side="bottom", pady=6)
        except Exception as e:
            self.log(f"Could not show channels: {e}")

    # -- tray (optional: needs pystray and Pillow)
    def _on_minimise(self, ev):
        if ev.widget is not self.root or self.root.state() != "iconic": return
        if not C.load().get("minimise_to_tray", True): return
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        if self.tray: return
        img = Image.new("RGB", (64, 64), "#1565c0"); d = ImageDraw.Draw(img); d.ellipse((14, 14, 50, 50), fill="white")
        def show(icon=None, item=None):
            self.q.put(("call", self._from_tray))
        self.tray = pystray.Icon(BRAND_NAME, img, BRAND_NAME, menu=pystray.Menu(
            pystray.MenuItem("Show", show, default=True), pystray.MenuItem("Quit", lambda i, it: self.q.put(("call", self.quit)))))
        self.root.withdraw(); threading.Thread(target=self.tray.run, daemon=True).start()

    def _from_tray(self):
        if self.tray: self.tray.stop(); self.tray = None
        self.root.deiconify(); self.root.state("normal"); self.root.lift()

    def quit(self):
        if self.engine and self.engine.running():
            if not messagebox.askyesno("Quit", "Copying is running. Stop copying and close?"): return
            self.engine.stop()
        if self.tray: self.tray.stop()
        self._telegram_shutdown()
        self.root.destroy()

    def _telegram_shutdown(self):
        from copier import engine as E
        if E._SERVICE is not None:
            try: E._SERVICE.shutdown()
            except Exception: pass


_instance_handle = None


def acquire_single_instance():
    """True if this is the only running SignalPilot. Windows: named mutex; elsewhere: a locked file."""
    global _instance_handle
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        _instance_handle = k32.CreateMutexW(None, False, "Local\\SignalPilot-SingleInstance")
        return not (_instance_handle and k32.GetLastError() == 183)   # ERROR_ALREADY_EXISTS
    try:
        import fcntl
    except ImportError:
        return True
    try:
        os.makedirs(C.data_dir(), exist_ok=True)
        _instance_handle = open(C.path("signalpilot.lock"), "w")
        fcntl.flock(_instance_handle, fcntl.LOCK_EX | fcntl.LOCK_NB); return True
    except OSError:
        return False


def focus_existing_window():
    """Brings the already running window to the front. Returns True when it was found."""
    if sys.platform != "win32": return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, f"{BRAND_NAME}  v{__version__}")
        if not hwnd: return False
        u32.ShowWindow(hwnd, 9); u32.SetForegroundWindow(hwnd); return True   # SW_RESTORE
    except Exception:
        return False


def main():
    if not acquire_single_instance():
        if not focus_existing_window():
            r = tk.Tk(); r.withdraw()
            messagebox.showinfo(BRAND_NAME, f"{BRAND_NAME} is already open. Look for it on the taskbar or in the system tray.")
            r.destroy()
        return
    root = tk.Tk(); App(root); root.mainloop()


if __name__ == "__main__":
    main()
