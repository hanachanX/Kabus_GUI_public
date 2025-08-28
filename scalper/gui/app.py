# -*- coding: utf-8 -*-
"""
Package-integrated GUI for scalper (kabuS) – v1.0
=================================================
このキャンバスには **3ファイル** を収録しています。プロジェクト直下 `scalper/` 配下に配置してください。

1) `scalper/gui/app.py` … GUI本体（Tkinter）。CSV tail/JSONL発注(=SIM向け)が既定。kabuS実弾用アダプタの差し替え口あり。
2) `scalper/gui/__init__.py` … 起動ヘルパ。
3) `scalper/main_gui.py` … `python -m scalper.main_gui --csv sim_logs/test.csv --poll 10` で起動。

既存の `python -m scalper.main_exec_demo --prod --symbol 7203 --poll 10 --csv sim_logs/test.csv` と**併走**できます。
GUIは `sim_logs/test.csv` を tail して「板/歩み値/5分足」「SIM/LIVE履歴」を更新、
発注は `sim_logs/orders_gui.jsonl` に JSONL で書き出します（OCO/Trail含む）。

――――――――――――――――――――――――――――――――――――――――
File: scalper/gui/app.py
――――――――――――――――――――――――――――――――――――――――
"""
from __future__ import annotations
import os, sys, csv, json, time, math, queue, threading, argparse
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

try:
    import pandas as pd  # optional
except Exception:
    pd = None

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
except Exception:
    Figure = None
    FigureCanvasTkAgg = None

APP_NAME = "kabuS Scalper GUI"
PRESET_SYMBOLS = ["7203", "8306", "8411", "8316", "9432", "9433", "6758", "7267", "6501", "6981"]
DEFAULT_POLL_SECS = 5
DEFAULT_CSV = os.path.join("sim_logs", "test.csv")
ORDERS_OUT = os.path.join("sim_logs", "orders_gui.jsonl")
SETTINGS_JSON = os.path.join(os.path.dirname(__file__), "scalper_gui_settings.json")
JST = dt.timezone(dt.timedelta(hours=9), name="JST")

# ─────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────

def ts_now() -> str:
    return dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(x: Any, default: float = math.nan) -> float:
    try:
        return float(x)
    except Exception:
        return default


def ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def read_csv_incremental(path: str, last_pos: int) -> Tuple[List[List[str]], int]:
    rows: List[List[str]] = []
    if not os.path.isfile(path):
        return rows, 0
    with open(path, "r", newline="", encoding="utf-8") as f:
        f.seek(last_pos)
        reader = csv.reader(f)
        for row in reader:
            if row:
                rows.append(row)
        new_pos = f.tell()
    return rows, new_pos


def parse_sim_row(header: List[str], row: List[str]) -> Dict[str, Any]:
    m = {h: row[i] if i < len(row) else None for i, h in enumerate(header)}
    t = m.get("ts") or m.get("time") or m.get("timestamp")
    try:
        dtobj = dt.datetime.fromisoformat(t)
        if dtobj.tzinfo is None:
            dtobj = dtobj.replace(tzinfo=JST)
    except Exception:
        dtobj = dt.datetime.now(JST)
    m["ts_dt"] = dtobj
    m["price"] = safe_float(m.get("price"))
    m["qty"] = int(safe_float(m.get("qty"), 0))
    m["pnl_ticks"] = safe_float(m.get("pnl_ticks"), 0.0)
    return m


def resample_5m_from_ticks(ticks: List[Dict[str, Any]]):
    if not ticks:
        return []
    try:
        import pandas as _pd  # local alias
        s = _pd.DataFrame([{"ts": t["ts_dt"], "price": t["price"], "qty": t.get("qty", 0)} for t in ticks])
        s.set_index("ts", inplace=True)
        s = s.sort_index()
        o = s["price"].resample("5min").ohlc()
        v = s["qty"].resample("5min").sum()
        o["volume"] = v
        out = []
        for idx, row in o.iterrows():
            if not math.isnan(row["open"]):
                out.append({
                    "ts": idx,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row["volume"] if not math.isnan(row.get("volume", math.nan)) else 0),
                })
        return out
    except Exception:
        pass
    buckets, vols = {}, {}
    for t in ticks:
        ts = t["ts_dt"].astimezone(JST)
        key = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        buckets.setdefault(key, []).append(t["price"])
        vols[key] = vols.get(key, 0) + int(t.get("qty", 0))
    out = []
    for key in sorted(buckets.keys()):
        arr = buckets[key]
        o, h, l, c = arr[0], max(arr), min(arr), arr[-1]
        out.append({"ts": key, "open": o, "high": h, "low": l, "close": c, "volume": vols.get(key, 0)})
    return out

# ─────────────────────────────────────────────────────────────
# Data models & Adapters
# ─────────────────────────────────────────────────────────────

@dataclass
class OCOParams:
    tp: float = 5.0
    sl: float = 5.0
    enabled: bool = False

@dataclass
class TrailParams:
    start: float = 5.0
    step: float = 1.0
    enabled: bool = False

@dataclass
class Settings:
    symbol: str = PRESET_SYMBOLS[0]
    poll_secs: int = DEFAULT_POLL_SECS
    sim_csv: str = DEFAULT_CSV
    live_csv: str = os.path.join("sim_logs", "live.csv")
    oco: OCOParams = field(default_factory=OCOParams)
    trail: TrailParams = field(default_factory=TrailParams)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "poll_secs": self.poll_secs,
            "sim_csv": self.sim_csv,
            "live_csv": self.live_csv,
            "oco": {"tp": self.oco.tp, "sl": self.oco.sl, "enabled": self.oco.enabled},
            "trail": {"start": self.trail.start, "step": self.trail.step, "enabled": self.trail.enabled},
        }

    @classmethod
    def from_file(cls, path: str) -> "Settings":
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            st = Settings()
            st.symbol = d.get("symbol", st.symbol)
            st.poll_secs = int(d.get("poll_secs", st.poll_secs))
            st.sim_csv = d.get("sim_csv", st.sim_csv)
            st.live_csv = d.get("live_csv", st.live_csv)
            oc = d.get("oco", {})
            st.oco = OCOParams(tp=safe_float(oc.get("tp", st.oco.tp)), sl=safe_float(oc.get("sl", st.oco.sl)), enabled=bool(oc.get("enabled", st.oco.enabled)))
            tr = d.get("trail", {})
            st.trail = TrailParams(start=safe_float(tr.get("start", st.trail.start)), step=safe_float(tr.get("step", st.trail.step)), enabled=bool(tr.get("enabled", st.trail.enabled)))
            return st
        except Exception:
            return Settings()

    def save(self, path: str) -> None:
        ensure_dir(path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

class KabuClientAdapter:
    """kabuS 実弾アダプタの差替え口（未接続ダミー）。"""
    def __init__(self):
        self.connected = False

    def place_order(self, symbol: str, side: str, qty: int, price: Optional[float] = None,
                    oco: Optional[OCOParams] = None, trail: Optional[TrailParams] = None,
                    note: str = "") -> Dict[str, Any]:
        return {"ok": False, "msg": "kabuS router not connected"}

    def get_positions(self) -> List[Dict[str, Any]]:
        return []

    def get_funds(self) -> Dict[str, Any]:
        return {"cash": 0.0, "equity": 0.0, "margin": 0.0}

    def get_board(self, last_price: float) -> Dict[str, Any]:
        step, depth = 1, 5
        bids, asks = [], []
        lp = last_price if last_price and not math.isnan(last_price) else 1000.0
        for i in range(depth, 0, -1):
            bids.append({"price": lp - i * step, "size": 100 * i})
        for i in range(1, depth + 1):
            asks.append({"price": lp + i * step, "size": 100 * i})
        return {"bids": bids, "asks": asks}

class SimCsvTailer(threading.Thread):
    def __init__(self, path: str, out_q: queue.Queue, poll_secs: int = DEFAULT_POLL_SECS, tag: str = "SIM"):
        super().__init__(daemon=True)
        self.path = path
        self.out_q = out_q
        self.poll_secs = poll_secs
        self.tag = tag
        self._stop = threading.Event()
        self._pos = 0
        self._header: List[str] = []

    def stop(self):
        self._stop.set()

    def run(self):
        last_mtime = 0.0
        if os.path.isfile(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                first = f.readline()
                self._pos = f.tell()
                self._header = [h.strip() for h in first.strip().split(",")]
        while not self._stop.is_set():
            try:
                if not os.path.isfile(self.path):
                    time.sleep(self.poll_secs)
                    continue
                mtime = os.path.getmtime(self.path)
                if mtime < last_mtime:
                    self._pos = 0
                    self._header = []
                last_mtime = mtime
                rows, self._pos = read_csv_incremental(self.path, self._pos)
                if rows:
                    if not self._header:
                        self._header = [h.strip() for h in rows[0]]
                        rows = rows[1:]
                    for r in rows:
                        item = parse_sim_row(self._header, r)
                        item["src_tag"] = self.tag
                        self.out_q.put(("csv_row", item))
            except Exception as e:
                self.out_q.put(("log", f"[{self.tag}] tailer error: {e}"))
            time.sleep(self.poll_secs)

# ─────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────

class ChartWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, get_ohlc_callable, symbol_getter):
        super().__init__(master)
        self.title("5分足チャート")
        self.geometry("920x560")
        self.get_ohlc = get_ohlc_callable
        self.get_symbol = symbol_getter
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if Figure is None:
            tk.Label(self, text="matplotlib が見つかりません。チャート不可").pack(fill="both", expand=True)
            return
        self.fig = Figure(figsize=(9, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        btnf = ttk.Frame(self); btnf.pack(fill="x")
        ttk.Button(btnf, text="更新 (F5)", command=self.redraw).pack(side="left", padx=6, pady=6)
        self.bind("<F5>", lambda e: self.redraw())
        self.redraw()

    def on_close(self):
        self.destroy()

    def redraw(self):
        ohlc = self.get_ohlc(); sym = self.get_symbol()
        self.ax.clear(); self.ax.set_title(f"{sym} – 5分足")
        if not ohlc:
            self.ax.text(0.5, 0.5, "No Data", ha="center", va="center"); self.canvas.draw(); return
        xs = [o["ts"] for o in ohlc]
        op = [o["open"] for o in ohlc]; hi = [o["high"] for o in ohlc]
        lo = [o["low"] for o in ohlc]; cl = [o["close"] for o in ohlc]
        for i, (x, o, h, l, c) in enumerate(zip(xs, op, hi, lo, cl)):
            color = "red" if c >= o else "blue"
            self.ax.vlines(i, l, h)
            self.ax.vlines(i, min(o, c), max(o, c), linewidth=6, colors=color)
        self.ax.set_xlim(-1, len(xs)); self.ax.grid(True, alpha=0.3)
        self.canvas.draw()

class App(tk.Tk):
    def __init__(self, sim_csv: str = DEFAULT_CSV, poll_secs: int = DEFAULT_POLL_SECS):
        super().__init__()
        self.title(APP_NAME); self.geometry("1180x760")
        self.settings = Settings.from_file(SETTINGS_JSON)
        if sim_csv: self.settings.sim_csv = sim_csv
        if poll_secs: self.settings.poll_secs = poll_secs

        self.last_price_by_symbol: Dict[str, float] = {}
        self.ticks_by_symbol: Dict[str, List[Dict[str, Any]]] = {s: [] for s in PRESET_SYMBOLS}
        self.sim_rows: List[Dict[str, Any]] = []
        self.live_rows: List[Dict[str, Any]] = []

        self.event_q: queue.Queue = queue.Queue()
        self.sim_tailer = SimCsvTailer(self.settings.sim_csv, self.event_q, self.settings.poll_secs, tag="SIM")
        self.live_tailer = SimCsvTailer(self.settings.live_csv, self.event_q, self.settings.poll_secs, tag="LIVE")
        self.kabu = KabuClientAdapter()  # 実弾は後差し替え

        self._build_menu()
        top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="銘柄:").pack(side="left")
        self.symbol_var = tk.StringVar(value=self.settings.symbol)
        self.symbol_combo = ttk.Combobox(top, textvariable=self.symbol_var, values=PRESET_SYMBOLS, width=8, state="readonly")
        self.symbol_combo.pack(side="left", padx=4)
        for s in PRESET_SYMBOLS:
            ttk.Button(top, text=s, command=lambda x=s: self._choose_symbol(x)).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="チャートウィンドウ", command=self.open_chart).pack(side="left")
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="ポーリング(s):").pack(side="left")
        self.poll_var = tk.IntVar(value=self.settings.poll_secs)
        ttk.Spinbox(top, from_=1, to=60, textvariable=self.poll_var, width=5, command=self._apply_poll).pack(side="left", padx=4)
        ttk.Button(top, text="開始/再開", command=self.start_tailers).pack(side="left", padx=6)
        ttk.Button(top, text="停止", command=self.stop_tailers).pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x")
        self.nb = ttk.Notebook(self); self.nb.pack(fill="both", expand=True)
        self._build_tab_board_ticks(); self._build_tab_funds_pos(); self._build_tab_sim_hist()
        self._build_tab_live_hist(); self._build_tab_screening(); self._build_tab_settings_logs()

        # 4段ステータスバー
        self.status_vars: List[tk.StringVar] = []
        status = ttk.Frame(self); status.pack(fill="x")
        for i in range(4):
            f = ttk.Frame(status); f.pack(fill="x")
            var = tk.StringVar(value=f"Ready {i+1}")
            ttk.Label(f, textvariable=var, anchor="w").pack(side="left", fill="x", expand=True, padx=6)
            self.status_vars.append(var)

        self.after(200, self._drain_events)
        self.start_tailers()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # menu
    def _build_menu(self):
        m = tk.Menu(self); self.config(menu=m)
        filem = tk.Menu(m, tearoff=False)
        filem.add_command(label="設定を保存", command=self._save_settings)
        filem.add_separator(); filem.add_command(label="終了", command=self.on_close)
        m.add_cascade(label="ファイル", menu=filem)
        helpm = tk.Menu(m, tearoff=False)
        helpm.add_command(label="バージョン情報", command=lambda: messagebox.showinfo(APP_NAME, "kabuS Scalper GUI v1.0"))
        m.add_cascade(label="ヘルプ", menu=helpm)

    # Tab: 板/歩み値/5分足
    def _build_tab_board_ticks(self):
        self.tab_bt = ttk.Frame(self.nb); self.nb.add(self.tab_bt, text="板/歩み値/5分足")
        left = ttk.Frame(self.tab_bt); mid = ttk.Frame(self.tab_bt); right = ttk.Frame(self.tab_bt)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        mid.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        right.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        ttk.Label(left, text="板 (シンセ)").pack(anchor="w")
        self.board_bids = ttk.Treeview(left, columns=("price", "size"), show="headings", height=8)
        self.board_asks = ttk.Treeview(left, columns=("price", "size"), show="headings", height=8)
        for tv in (self.board_bids, self.board_asks):
            tv.heading("price", text="価格"); tv.heading("size", text="数量")
            tv.column("price", width=80, anchor="e"); tv.column("size", width=70, anchor="e")
            tv.pack(fill="x", pady=4)
        ttk.Button(left, text="板を更新", command=self._refresh_board).pack(anchor="e")

        ttk.Label(mid, text="歩み値 (uptick=赤 / downtick=青 / 不変=黒)").pack(anchor="w")
        self.ticks_tv = ttk.Treeview(mid, columns=("ts", "price", "qty", "side"), show="headings")
        for c, w in (("ts", 135), ("price", 80), ("qty", 70), ("side", 60)):
            self.ticks_tv.heading(c, text=c.upper())
            self.ticks_tv.column(c, width=w, anchor="e" if c in ("price", "qty") else "w")
        self.ticks_tv.pack(fill="both", expand=True)
        self.ticks_tv.tag_configure("uptick", foreground="#d00")
        self.ticks_tv.tag_configure("downtick", foreground="#06c")
        self.ticks_tv.tag_configure("unch", foreground="#000")

        ttk.Label(right, text="5分足 OHLC").pack(anchor="w")
        self.ohlc_tv = ttk.Treeview(right, columns=("ts", "open", "high", "low", "close", "vol"), show="headings")
        for c, w in (("ts", 120), ("open", 75), ("high", 75), ("low", 75), ("close", 75), ("vol", 70)):
            self.ohlc_tv.heading(c, text=c.upper())
            self.ohlc_tv.column(c, width=w, anchor="e" if c != "ts" else "w")
        self.ohlc_tv.pack(fill="both", expand=True)
        ttk.Button(right, text="OHLC更新", command=self._refresh_ohlc_table).pack(anchor="e", pady=4)

        ctl = ttk.LabelFrame(self.tab_bt, text="注文 / OCO / トレール (GUI→orders_gui.jsonl)")
        ctl.pack(fill="x", padx=6, pady=6)
        ttk.Label(ctl, text="数量").grid(row=0, column=0, padx=4, pady=4)
        self.qty_var = tk.IntVar(value=100)
        ttk.Spinbox(ctl, from_=100, to=10000, increment=100, textvariable=self.qty_var, width=8).grid(row=0, column=1)
        ttk.Button(ctl, text="成行 Buy", command=lambda: self._place_order("BUY")).grid(row=0, column=2, padx=4)
        ttk.Button(ctl, text="成行 Sell", command=lambda: self._place_order("SELL")).grid(row=0, column=3, padx=4)
        self.oco_enable = tk.BooleanVar(value=self.settings.oco.enabled)
        ttk.Checkbutton(ctl, text="OCO", variable=self.oco_enable).grid(row=0, column=4, padx=8)
        ttk.Label(ctl, text="TP").grid(row=0, column=5)
        self.oco_tp = tk.DoubleVar(value=self.settings.oco.tp)
        ttk.Spinbox(ctl, from_=0.1, to=100.0, increment=0.1, textvariable=self.oco_tp, width=6).grid(row=0, column=6)
        ttk.Label(ctl, text="SL").grid(row=0, column=7)
        self.oco_sl = tk.DoubleVar(value=self.settings.oco.sl)
        ttk.Spinbox(ctl, from_=0.1, to=100.0, increment=0.1, textvariable=self.oco_sl, width=6).grid(row=0, column=8)
        self.trail_enable = tk.BooleanVar(value=self.settings.trail.enabled)
        ttk.Checkbutton(ctl, text="Trail", variable=self.trail_enable).grid(row=0, column=9, padx=8)
        ttk.Label(ctl, text="開始").grid(row=0, column=10)
        self.trail_start = tk.DoubleVar(value=self.settings.trail.start)
        ttk.Spinbox(ctl, from_=0.1, to=100.0, increment=0.1, textvariable=self.trail_start, width=6).grid(row=0, column=11)
        ttk.Label(ctl, text="幅").grid(row=0, column=12)
        self.trail_step = tk.DoubleVar(value=self.settings.trail.step)
        ttk.Spinbox(ctl, from_=0.1, to=50.0, increment=0.1, textvariable=self.trail_step, width=6).grid(row=0, column=13)

    # Tab: 資金・建玉
    def _build_tab_funds_pos(self):
        self.tab_fp = ttk.Frame(self.nb); self.nb.add(self.tab_fp, text="資金・建玉")
        top = ttk.Frame(self.tab_fp); top.pack(fill="x", padx=8, pady=6)
        self.cash_var = tk.StringVar(value="Cash: 0"); self.eq_var = tk.StringVar(value="Equity: 0"); self.mgn_var = tk.StringVar(value="Margin: 0")
        ttk.Label(top, textvariable=self.cash_var).pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.eq_var).pack(side="left", padx=6)
        ttk.Label(top, textvariable=self.mgn_var).pack(side="left", padx=6)
        ttk.Button(top, text="更新", command=self._refresh_funds_positions).pack(side="right")

        self.pos_tv = ttk.Treeview(self.tab_fp, columns=("symbol", "side", "qty", "price", "pnl"), show="headings")
        for c, w in (("symbol", 70), ("side", 60), ("qty", 60), ("price", 80), ("pnl", 80)):
            self.pos_tv.heading(c, text=c.upper())
            self.pos_tv.column(c, width=w, anchor="e" if c in ("qty", "price", "pnl") else "w")
        self.pos_tv.pack(fill="both", expand=True, padx=8, pady=6)

    # Tab: SIM履歴
    def _build_tab_sim_hist(self):
        self.tab_sim = ttk.Frame(self.nb); self.nb.add(self.tab_sim, text="SIM履歴")
        self.sim_tv = ttk.Treeview(self.tab_sim, columns=("ts", "event", "symbol", "side", "qty", "price", "pnl_ticks"), show="headings")
        for c, w in (("ts", 140), ("event", 80), ("symbol", 70), ("side", 60), ("qty", 70), ("price", 80), ("pnl_ticks", 80)):
            self.sim_tv.heading(c, text=c.upper()); self.sim_tv.column(c, width=w, anchor="e" if c in ("qty", "price", "pnl_ticks") else "w")
        self.sim_tv.pack(fill="both", expand=True, padx=8, pady=6)
        btnf = ttk.Frame(self.tab_sim); btnf.pack(fill="x", padx=8, pady=6)
        ttk.Button(btnf, text="CSV出力", command=lambda: self._export_history(self.sim_rows, kind="SIM", ext="csv")).pack(side="left")
        ttk.Button(btnf, text="Excel出力", command=lambda: self._export_history(self.sim_rows, kind="SIM", ext="xlsx")).pack(side="left", padx=6)

    # Tab: LIVE履歴
    def _build_tab_live_hist(self):
        self.tab_live = ttk.Frame(self.nb); self.nb.add(self.tab_live, text="LIVE履歴")
        self.live_tv = ttk.Treeview(self.tab_live, columns=("ts", "event", "symbol", "side", "qty", "price", "pnl_ticks"), show="headings")
        for c, w in (("ts", 140), ("event", 80), ("symbol", 70), ("side", 60), ("qty", 70), ("price", 80), ("pnl_ticks", 80)):
            self.live_tv.heading(c, text=c.upper()); self.live_tv.column(c, width=w, anchor="e" if c in ("qty", "price", "pnl_ticks") else "w")
        self.live_tv.pack(fill="both", expand=True, padx=8, pady=6)
        btnf = ttk.Frame(self.tab_live); btnf.pack(fill="x", padx=8, pady=6)
        ttk.Button(btnf, text="CSV出力", command=lambda: self._export_history(self.live_rows, kind="LIVE", ext="csv")).pack(side="left")
        ttk.Button(btnf, text="Excel出力", command=lambda: self._export_history(self.live_rows, kind="LIVE", ext="xlsx")).pack(side="left", padx=6)

    # Tab: スクリーニング
    def _build_tab_screening(self):
        self.tab_scr = ttk.Frame(self.nb); self.nb.add(self.tab_scr, text="スクリーニング")
        ctl = ttk.Frame(self.tab_scr); ctl.pack(fill="x", padx=8, pady=6)
        ttk.Label(ctl, text="プリセット銘柄の直近価格一覧（歩み値から算出）").pack(side="left")
        ttk.Button(ctl, text="更新", command=self._refresh_screening).pack(side="right")
        self.scr_tv = ttk.Treeview(self.tab_scr, columns=("symbol", "last", "chg"), show="headings")
        for c, w in (("symbol", 80), ("last", 90), ("chg", 90)):
            self.scr_tv.heading(c, text=c.upper()); self.scr_tv.column(c, width=w, anchor="e" if c != "symbol" else "w")
        self.scr_tv.pack(fill="both", expand=True, padx=8, pady=6)

    # Tab: 設定/ログ
    def _build_tab_settings_logs(self):
        self.tab_set = ttk.Frame(self.nb); self.nb.add(self.tab_set, text="設定/ログ")
        lf = ttk.LabelFrame(self.tab_set, text="ファイル/ポーリング"); lf.pack(fill="x", padx=8, pady=6)
        ttk.Label(lf, text="SIM CSV").grid(row=0, column=0, sticky="e")
        self.sim_csv_var = tk.StringVar(value=self.settings.sim_csv)
        ttk.Entry(lf, textvariable=self.sim_csv_var, width=60).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(lf, text="…", command=lambda: self._browse_csv(self.sim_csv_var)).grid(row=0, column=2)
        ttk.Label(lf, text="LIVE CSV").grid(row=1, column=0, sticky="e")
        self.live_csv_var = tk.StringVar(value=self.settings.live_csv)
        ttk.Entry(lf, textvariable=self.live_csv_var, width=60).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(lf, text="…", command=lambda: self._browse_csv(self.live_csv_var)).grid(row=1, column=2)
        ttk.Label(lf, text="ポーリング(s)").grid(row=2, column=0, sticky="e")
        self.poll_var = tk.IntVar(value=self.settings.poll_secs)
        ttk.Spinbox(lf, from_=1, to=60, textvariable=self.poll_var, width=6, command=self._apply_poll).grid(row=2, column=1, sticky="w")

        lf2 = ttk.LabelFrame(self.tab_set, text="OCO/トレール（デフォルト）"); lf2.pack(fill="x", padx=8, pady=6)
        ttk.Checkbutton(lf2, text="OCO 有効", variable=self.oco_enable).grid(row=0, column=0, padx=4)
        ttk.Label(lf2, text="TP").grid(row=0, column=1)
        ttk.Spinbox(lf2, from_=0.1, to=100.0, increment=0.1, textvariable=self.oco_tp, width=6).grid(row=0, column=2)
        ttk.Label(lf2, text="SL").grid(row=0, column=3)
        ttk.Spinbox(lf2, from_=0.1, to=100.0, increment=0.1, textvariable=self.oco_sl, width=6).grid(row=0, column=4)
        ttk.Checkbutton(lf2, text="Trail 有効", variable=self.trail_enable).grid(row=1, column=0, padx=4)
        ttk.Label(lf2, text="開始").grid(row=1, column=1)
        ttk.Spinbox(lf2, from_=0.1, to=100.0, increment=0.1, textvariable=self.trail_start, width=6).grid(row=1, column=2)
        ttk.Label(lf2, text="幅").grid(row=1, column=3)
        ttk.Spinbox(lf2, from_=0.1, to=100.0, increment=0.1, textvariable=self.trail_step, width=6).grid(row=1, column=4)

        ttk.Label(self.tab_set, text="ログ").pack(anchor="w", padx=8)
        self.log_box = ScrolledText(self.tab_set, height=12); self.log_box.pack(fill="both", expand=True, padx=8, pady=6)

    # lifecycle
    def start_tailers(self):
        self.stop_tailers()
        self.settings.sim_csv = self.sim_csv_var.get(); self.settings.live_csv = self.live_csv_var.get()
        self.settings.poll_secs = int(self.poll_var.get())
        self.sim_tailer = SimCsvTailer(self.settings.sim_csv, self.event_q, self.settings.poll_secs, tag="SIM")
        self.live_tailer = SimCsvTailer(self.settings.live_csv, self.event_q, self.settings.poll_secs, tag="LIVE")
        self.sim_tailer.start(); self.live_tailer.start()
        self._log(f"[tail] SIM:{self.settings.sim_csv} LIVE:{self.settings.live_csv} {self.settings.poll_secs}s")
        self._set_status(1, f"tail開始 SIM:{os.path.basename(self.settings.sim_csv)} LIVE:{os.path.basename(self.settings.live_csv)}")

    def stop_tailers(self):
        for t in (getattr(self, "sim_tailer", None), getattr(self, "live_tailer", None)):
            if t and t.is_alive(): t.stop()
        self._set_status(1, "tail停止")

    def _drain_events(self):
        try:
            while True:
                typ, payload = self.event_q.get_nowait()
                if typ == "csv_row": self._apply_csv_row(payload)
                elif typ == "log": self._log(str(payload))
        except queue.Empty:
            pass
        self.after(200, self._drain_events)

    def _apply_csv_row(self, item: Dict[str, Any]):
        sym = item.get("symbol") or self.symbol_var.get(); ts = item.get("ts_dt")
        price = item.get("price"); qty = int(item.get("qty", 0))
        side = (item.get("side") or "").upper(); tag = item.get("src_tag", "SIM")
        event = item.get("event") or "trade"
        if sym == self.symbol_var.get() and price:
            prev = self.last_price_by_symbol.get(sym)
            color_tag = "unch" if prev is None else ("uptick" if price > prev else ("downtick" if price < prev else "unch"))
            self.ticks_tv.insert("", "end", values=(ts.strftime("%H:%M:%S"), f"{price:.2f}", qty, side), tags=(color_tag,))
            self.ticks_tv.see("end"); self.last_price_by_symbol[sym] = price
            self.ticks_by_symbol.setdefault(sym, []).append(item)
        self._refresh_board(auto=True)
        rec = {"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "event": event, "symbol": sym, "side": side, "qty": qty, "price": price, "pnl_ticks": item.get("pnl_ticks", 0.0)}
        if tag == "SIM":
            self.sim_rows.append(rec)
            self.sim_tv.insert("", "end", values=(rec["ts"], rec["event"], rec["symbol"], rec["side"], rec["qty"], f"{rec['price']:.2f}", f"{rec['pnl_ticks']:.2f}")); self.sim_tv.see("end")
        else:
            self.live_rows.append(rec)
            self.live_tv.insert("", "end", values=(rec["ts"], rec["event"], rec["symbol"], rec["side"], rec["qty"], f"{rec['price']:.2f}", f"{rec['pnl_ticks']:.2f}")); self.live_tv.see("end")
        self._set_status(2, f"{sym} last={price}")

    def _refresh_board(self, auto: bool = False):
        sym = self.symbol_var.get(); lp = self.last_price_by_symbol.get(sym, math.nan)
        bd = self.kabu.get_board(lp)
        for tv in (self.board_bids, self.board_asks):
            for i in tv.get_children(): tv.delete(i)
        for b in bd.get("bids", [])[::-1]: self.board_bids.insert("", "end", values=(f"{b['price']:.2f}", b["size"]))
        for a in bd.get("asks", []): self.board_asks.insert("", "end", values=(f"{a['price']:.2f}", a["size"]))
        if not auto: self._set_status(3, f"板更新 {sym}")

    def _refresh_ohlc_table(self):
        sym = self.symbol_var.get(); ticks = self.ticks_by_symbol.get(sym, [])
        ohlc = resample_5m_from_ticks(ticks)
        for i in self.ohlc_tv.get_children(): self.ohlc_tv.delete(i)
        for r in ohlc[-100:]:
            self.ohlc_tv.insert("", "end", values=(r["ts"].strftime("%m-%d %H:%M"), f"{r['open']:.2f}", f"{r['high']:.2f}", f"{r['low']:.2f}", f"{r['close']:.2f}", r["volume"]))
        self._set_status(3, f"5分足更新 {sym} 本数={len(ohlc)}")

    def _refresh_funds_positions(self):
        f = self.kabu.get_funds()
        self.cash_var.set(f"Cash: {f.get('cash', 0):,.0f}"); self.eq_var.set(f"Equity: {f.get('equity', 0):,.0f}"); self.mgn_var.set(f"Margin: {f.get('margin', 0):,.0f}")
        for i in self.pos_tv.get_children(): self.pos_tv.delete(i)
        for p in self.kabu.get_positions(): self.pos_tv.insert("", "end", values=(p.get("symbol"), p.get("side"), p.get("qty"), p.get("price"), p.get("pnl")))
        self._set_status(4, "資金・建玉 更新")

    def _refresh_screening(self):
        for i in self.scr_tv.get_children(): self.scr_tv.delete(i)
        for s in PRESET_SYMBOLS:
            last = self.last_price_by_symbol.get(s); chg = ""
            if last is not None and not math.isnan(last): self.scr_tv.insert("", "end", values=(s, f"{last:.2f}", chg))
            else: self.scr_tv.insert("", "end", values=(s, "-", ""))
        self._set_status(4, "スクリーニング 更新")

    def _place_order(self, side: str):
        sym = self.symbol_var.get(); qty = int(self.qty_var.get())
        oco = OCOParams(tp=float(self.oco_tp.get()), sl=float(self.oco_sl.get()), enabled=bool(self.oco_enable.get()))
        trl = TrailParams(start=float(self.trail_start.get()), step=float(self.trail_step.get()), enabled=bool(self.trail_enable.get()))
        cmd = {"ts": ts_now(), "cmd": "order", "symbol": sym, "side": side, "qty": qty, "oco": oco.__dict__, "trail": trl.__dict__}
        ensure_dir(ORDERS_OUT)
        with open(ORDERS_OUT, "a", encoding="utf-8") as f: f.write(json.dumps(cmd, ensure_ascii=False) + "")
        self._log(f"[GUI→SIM] {cmd}"); self._set_status(2, f"注文送信 {sym} {side} {qty}")

    def open_chart(self):
        def get_ohlc():
            sym = self.symbol_var.get(); return resample_5m_from_ticks(self.ticks_by_symbol.get(sym, []))
        ChartWindow(self, get_ohlc_callable=get_ohlc, symbol_getter=lambda: self.symbol_var.get())

    def _choose_symbol(self, sym: str):
        self.symbol_var.set(sym); self._set_status(1, f"銘柄選択 {sym}")
        self._refresh_board(); self._refresh_ohlc_table()

    def _browse_csv(self, var: tk.StringVar):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if p: var.set(p)

    def _export_history(self, rows: List[Dict[str, Any]], kind: str, ext: str = "csv"):
        if not rows: messagebox.showwarning("出力", f"{kind}履歴が空です"); return
        sym = self.symbol_var.get(); fname = f"{kind}_history_{sym}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
        p = filedialog.asksaveasfilename(defaultextension=f".{ext}", initialfile=fname, filetypes=[(ext.upper(), f"*.{ext}"), ("All", "*.*")])
        if not p: return
        ensure_dir(p)
        if ext == "csv" or pd is None:
            header = list(rows[0].keys())
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=header); w.writeheader(); [w.writerow(r) for r in rows]
            messagebox.showinfo("出力", f"CSV出力完了{p}")
        else:
            try:
                df = pd.DataFrame(rows)
                try:
                    with pd.ExcelWriter(p, engine="xlsxwriter") as ex: df.to_excel(ex, sheet_name=kind, index=False)
                except Exception:
                    with pd.ExcelWriter(p, engine="openpyxl") as ex: df.to_excel(ex, sheet_name=kind, index=False)
                messagebox.showinfo("出力", f"Excel出力完了{p}")
            except Exception as e:
                messagebox.showerror("出力", f"Excel出力に失敗: {e}")

    def _apply_poll(self):
        try:
            v = int(self.poll_var.get()); self.sim_tailer.poll_secs = v; self.live_tailer.poll_secs = v
            self._set_status(1, f"ポーリング = {v}s")
        except Exception:
            pass

    def _save_settings(self):
        s = self.settings
        s.symbol = self.symbol_var.get(); s.poll_secs = int(self.poll_var.get())
        s.sim_csv = self.sim_csv_var.get(); s.live_csv = self.live_csv_var.get()
        s.oco = OCOParams(tp=float(self.oco_tp.get()), sl=float(self.oco_sl.get()), enabled=bool(self.oco_enable.get()))
        s.trail = TrailParams(start=float(self.trail_start.get()), step=float(self.trail_step.get()), enabled=bool(self.trail_enable.get()))
        s.save(SETTINGS_JSON); self._log(f"設定保存: {SETTINGS_JSON}")

    def _log(self, msg: str):
        ts = ts_now(); self.log_box.insert("end", f"[{ts}] {msg}"); self.log_box.see("end")

    def _set_status(self, idx: int, text: str):
        if 1 <= idx <= len(self.status_vars): self.status_vars[idx - 1].set(text)

    def on_close(self):
        try: self.stop_tailers(); self._save_settings()
        except Exception: pass
        self.destroy()

# entry point
def launch(sim_csv: str = DEFAULT_CSV, poll_secs: int = DEFAULT_POLL_SECS):
    app = App(sim_csv=sim_csv, poll_secs=poll_secs); app.mainloop()

# CLI (module direct)
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--poll", type=int, default=DEFAULT_POLL_SECS)
    a = ap.parse_args(); launch(sim_csv=a.csv, poll_secs=a.poll)


# ─────────────────────────────────────────────────────────────
# File: scalper/gui/__init__.py
# ─────────────────────────────────────────────────────────────
from .app import launch, App
__all__ = ["launch", "App"]


# ─────────────────────────────────────────────────────────────
# File: scalper/main_gui.py
# ─────────────────────────────────────────────────────────────
"""
GUI ランチャ。
Usage:
  python -m scalper.main_gui --csv sim_logs/test.csv --poll 10
"""
from __future__ import annotations
import argparse
from .gui import launch

def main():
    ap = argparse.ArgumentParser(description="kabuS Scalper GUI")
    ap.add_argument("--csv", default="sim_logs/test.csv")
    ap.add_argument("--poll", type=int, default=5)
    a = ap.parse_args(); launch(sim_csv=a.csv, poll_secs=a.poll)

if __name__ == "__main__":
    main()
