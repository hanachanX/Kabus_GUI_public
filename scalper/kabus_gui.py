# -*- coding: utf-8 -*-
"""
File: scraper/Kabus_gui.py
kabuS – Board/Tape + 5m Chart + Tabs + Screener + OCO/Trail
- 板 / 歩み値（色分け：アップ=赤、ダウン=青、同値=黒）
- 5分足チャート（別窓ポップアウト）
- 資金・建玉タブ（OCO/トレール操作）
- SIM/LIVE履歴（CSV/XLSXエクスポート）
- スクリーニング
- 設定/ログ
- 実運用(LIVE)はプロバイダ差し替え式のスタブ。初期はSIMで動作可能。

実行例:
  python -m scraper.Kabus_gui --symbol 7203 --poll 2 --sim
  python scraper/Kabus_gui.py --symbol 7203 --poll 2 --sim
"""
import os
import sys
import time
import math
import queue
import json
import random
import argparse
import threading
from datetime import datetime, timedelta, timezone

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import pandas as pd
except Exception:
    pd = None

# Excel書き出しは openpyxl or xlsxwriter のどちらかがあればOK（無ければCSVのみ）
_HAS_XLSX = False
for _xlsx in ("openpyxl", "xlsxwriter"):
    try:
        __import__(_xlsx)
        _HAS_XLSX = True
        break
    except Exception:
        continue

# ====== カラー & スタイル ======
COLOR_BG = "#0f1116"
COLOR_FG = "#e6e6e6"
COLOR_ACCENT = "#1f6feb"
COLOR_UP = "#e11d48"     # 赤（上）
COLOR_DN = "#2563eb"     # 青（下）
COLOR_EQ = "#a3a3a3"     # 同値（灰）
COLOR_WARN = "#f59e0b"

JST = timezone(timedelta(hours=9))

def jst_now():
    return datetime.now(JST)

def ts():
    return jst_now().strftime("%H:%M:%S")

def pretty_num(x):
    try:
        if isinstance(x, float) and (abs(x) >= 1000):
            return f"{x:,.2f}"
        if isinstance(x, (int,)) and (abs(x) >= 1000):
            return f"{x:,}"
        return str(x)
    except Exception:
        return str(x)

# ====== データモデル ======
class Candle:
    __slots__ = ("t", "o", "h", "l", "c", "v")
    def __init__(self, t: datetime, o: float, h: float, l: float, c: float, v: int):
        self.t, self.o, self.h, self.l, self.c, self.v = t, o, h, l, c, v

class Position:
    __slots__ = ("symbol", "side", "qty", "avg_price", "unreal", "time")
    def __init__(self, symbol, side, qty, avg_price, unreal=0.0, time=None):
        self.symbol, self.side, self.qty, self.avg_price = symbol, side, qty, avg_price
        self.unreal = unreal
        self.time = time or jst_now()

class TradeRecord:
    __slots__ = ("time", "symbol", "side", "qty", "price", "pnl_ticks", "mode")
    def __init__(self, time, symbol, side, qty, price, pnl_ticks, mode):
        self.time, self.symbol, self.side = time, symbol, side
        self.qty, self.price, self.pnl_ticks, self.mode = qty, price, pnl_ticks, mode

class OrderReq:
    __slots__ = ("symbol", "side", "qty", "price", "type", "tp", "sl", "trail")
    def __init__(self, symbol, side, qty, price=None, type="MKT", tp=None, sl=None, trail=None):
        self.symbol, self.side, self.qty = symbol, side, qty
        self.price, self.type = price, type   # "MKT" or "LMT"
        self.tp, self.sl, self.trail = tp, sl, trail

# ====== ロギング（UIキューへ流す） ======
class UILogger:
    def __init__(self):
        self.q = queue.Queue(maxsize=10000)
    def info(self, msg):  self._put("INFO", msg)
    def warn(self, msg):  self._put("WARN", msg)
    def error(self, msg): self._put("ERROR", msg)
    def _put(self, lvl, msg):
        try:
            self.q.put_nowait(f"[{ts()}] {lvl}: {msg}")
        except queue.Full:
            pass

LOGGER = UILogger()

# ====== データプロバイダ基底 ======
class DataProviderBase:
    """LIVE接続やSIM接続の差し替え用インターフェース"""
    def start(self): ...
    def stop(self): ...
    def get_board(self, symbol): ...
    def get_tape(self, symbol, limit=200): ...
    def get_5m_candles(self, symbol, limit=200): ...
    def get_cash(self): ...
    def get_positions(self): ...
    def get_watchlist(self): ...
    def place(self, order: OrderReq) -> dict: ...
    def close_all(self, symbol=None): ...
    def get_trades(self, mode_filter=None): ...
    def mode(self) -> str: ...

# ====== SIMプロバイダ ======
class SimProvider(DataProviderBase):
    """
    ランダムウォークで価格/板/歩み値/5分足を生成。
    OCO/トレールはアプリ側で補助（TP/SL到達で約定）。
    """
    def __init__(self, symbol="7203", seed=None):
        self.symbol = symbol
        self._rnd = random.Random(seed or int(time.time()))
        self._running = False
        self._thr = None
        self._lock = threading.Lock()
        # マーケット状態
        self._px = 2500.0
        self._last_px = self._px
        self._tape = []           # list[(t, price, qty)]
        self._board = {"bids": [], "asks": []}  # (price, qty)
        self._candles_5m = []     # list[Candle]
        self._cash = 1_000_000.0
        self._positions = []      # list[Position]
        self._trades = []         # list[TradeRecord]
        self._watch = [self.symbol, "6758", "9432", "9984", "8306", "8035"]
        self._mk_task_interval = 1.0
        self._make_initial()

    def mode(self): return "SIM"

    def _make_initial(self):
        now = jst_now()
        # 5分足 100本
        base = (now - timedelta(minutes=5*99)).replace(second=0, microsecond=0)
        base = base - timedelta(minutes=base.minute % 5)
        px = self._px
        for i in range(100):
            t = base + timedelta(minutes=5*i)
            o = px
            for _ in range(20):
                px += self._rnd.uniform(-2.0, 2.0)
            h = max(o, px) + self._rnd.uniform(0, 1.0)
            l = min(o, px) - self._rnd.uniform(0, 1.0)
            c = px
            v = self._rnd.randint(500, 1500)
            self._candles_5m.append(Candle(t, o, h, l, c, v))
        self._px = self._candles_5m[-1].c
        self._last_px = self._px
        self._rebuild_board()

    def _rebuild_board(self):
        mid = self._px
        bids = []
        asks = []
        for i in range(10):
            price_b = round(mid - (i+1)*0.5, 1)
            price_a = round(mid + (i+1)*0.5, 1)
            bids.append((price_b, self._rnd.randint(100, 1000)))
            asks.append((price_a, self._rnd.randint(100, 1000)))
        self._board["bids"] = bids
        self._board["asks"] = asks

    def _mk(self):
        # マーケット更新ループ
        while self._running:
            with self._lock:
                # ランダムウォーク
                drift = self._rnd.uniform(-1.0, 1.0)
                vol = self._rnd.uniform(0.0, 2.5)
                d = drift + vol * self._rnd.choice([-1, 1])
                self._last_px = self._px
                self._px = max(1.0, self._px + d)
                # 歩み値
                qty = self._rnd.randint(1, 50) * 100
                self._tape.append((jst_now(), round(self._px, 1), qty))
                if len(self._tape) > 600:
                    self._tape = self._tape[-600:]
                # 5分足更新
                self._update_5m(self._px, qty)
                # 板更新
                self._rebuild_board()
                # 建玉評価損益更新
                self._mark_positions()
            time.sleep(self._mk_task_interval)

    def _update_5m(self, price, vol):
        if not self._candles_5m:
            t = jst_now().replace(second=0, microsecond=0)
            t = t - timedelta(minutes=t.minute % 5)
            self._candles_5m.append(Candle(t, price, price, price, price, vol))
            return
        last = self._candles_5m[-1]
        cur_bucket = jst_now().replace(second=0, microsecond=0)
        cur_bucket = cur_bucket - timedelta(minutes=cur_bucket.minute % 5)
        if cur_bucket == last.t:
            last.h = max(last.h, price)
            last.l = min(last.l, price)
            last.c = price
            last.v += vol
        elif cur_bucket > last.t:
            # 新足
            o = last.c
            self._candles_5m.append(Candle(cur_bucket, o, price, price, price, vol))
            if len(self._candles_5m) > 400:
                self._candles_5m = self._candles_5m[-400:]

    def _mark_positions(self):
        for p in self._positions:
            if p.side == "LONG":
                p.unreal = (self._px - p.avg_price) * p.qty
            else:
                p.unreal = (p.avg_price - self._px) * p.qty

    # === API風メソッド ===
    def start(self):
        if self._running:
            return
        self._running = True
        self._thr = threading.Thread(target=self._mk, daemon=True)
        self._thr.start()
        LOGGER.info("SIM provider started")

    def stop(self):
        self._running = False
        if self._thr:
            self._thr.join(timeout=1.0)
        LOGGER.info("SIM provider stopped")

    def get_board(self, symbol):
        with self._lock:
            return self._board.copy(), round(self._px, 1), round(self._last_px, 1)

    def get_tape(self, symbol, limit=200):
        with self._lock:
            return list(self._tape[-limit:])

    def get_5m_candles(self, symbol, limit=200):
        with self._lock:
            return list(self._candles_5m[-limit:])

    def get_cash(self):
        with self._lock:
            return self._cash

    def get_positions(self):
        with self._lock:
            return list(self._positions)

    def get_watchlist(self):
        return list(self._watch)

    def get_trades(self, mode_filter=None):
        with self._lock:
            if mode_filter in (None, "", "ALL"):
                return list(self._trades)
            return [t for t in self._trades if t.mode == mode_filter]

    def close_all(self, symbol=None):
        with self._lock:
            pnl = 0.0
            remains = []
            for p in self._positions:
                if (symbol is None) or (p.symbol == symbol):
                    side = "SELL" if p.side == "LONG" else "BUY"
                    price = self._px
                    ticks = (price - p.avg_price) if p.side == "LONG" else (p.avg_price - price)
                    ticks *= p.qty
                    pnl += ticks
                    self._trades.append(TradeRecord(jst_now(), p.symbol, side, p.qty, price, ticks, self.mode()))
                else:
                    remains.append(p)
            self._positions = remains
            self._cash += pnl
            return {"ok": True, "closed_pnl": pnl}

    def place(self, order: OrderReq) -> dict:
        with self._lock:
            price = self._px if order.type == "MKT" or order.price is None else float(order.price)
            qty = int(order.qty)
            if qty <= 0:
                return {"ok": False, "err": "qty must be > 0"}
            # 建玉反映（単純化）
            if order.side == "BUY":
                self._positions.append(Position(order.symbol, "LONG", qty, price))
                self._cash -= price * qty
            elif order.side == "SELL":
                self._positions.append(Position(order.symbol, "SHORT", qty, price))
                self._cash += price * qty
            self._trades.append(TradeRecord(jst_now(), order.symbol, order.side, qty, price, 0.0, self.mode()))
            # 歩み値に反映
            self._tape.append((jst_now(), round(price, 1), max(100, qty)))
            # OCO/Trailはアプリ側で管理（ここでは受領だけ）
            return {"ok": True, "avg_price": price, "tp": order.tp, "sl": order.sl, "trail": order.trail}


# ====== LIVEスタブ（差し替えポイント） ======
class LiveProviderStub(DataProviderBase):
    """実弾LIVE接続用の雛形（KabuS API等への接続を各自実装）"""
    def __init__(self, symbol="7203"):
        self.symbol = symbol
        self._sim = SimProvider(symbol=symbol, seed=1234)  # とりあえずSIMを裏で動かす
    def mode(self): return "LIVE"
    def start(self): self._sim.start(); LOGGER.info("LIVE stub started (SIM-backed)")
    def stop(self): self._sim.stop(); LOGGER.info("LIVE stub stopped")
    def get_board(self, s): return self._sim.get_board(s)
    def get_tape(self, s, limit=200): return self._sim.get_tape(s, limit)
    def get_5m_candles(self, s, limit=200): return self._sim.get_5m_candles(s, limit)
    def get_cash(self): return self._sim.get_cash()
    def get_positions(self): return self._sim.get_positions()
    def get_watchlist(self): return self._sim.get_watchlist()
    def place(self, order): 
        r = self._sim.place(order); r["mode"]="LIVE"; return r
    def close_all(self, symbol=None): return self._sim.close_all(symbol)
    def get_trades(self, mode_filter=None):
        return [TradeRecord(t.time, t.symbol, t.side, t.qty, t.price, t.pnl_ticks, "LIVE")
                for t in self._sim.get_trades()]

# ====== GUI アプリ骨格 ======
class ChartWindow(tk.Toplevel):
    """5分足チャート別窓"""
    def __init__(self, master, get_candles_callable, symbol_getter):
        super().__init__(master)
        self.title("5分足チャート")
        self.geometry("900x480+80+80")
        self.configure(bg=COLOR_BG)
        self._get_candles = get_candles_callable
        self._symbol_getter = symbol_getter
        # matplotlib embed
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self.fig = Figure(figsize=(8.5, 4.2), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.after(200, self._refresh)

    def _refresh(self):
        try:
            cnds = self._get_candles()
            self.ax.clear()
            if cnds:
                xs = [c.t for c in cnds]
                os_ = [c.o for c in cnds]; hs=[c.h for c in cnds]
                ls  = [c.l for c in cnds]; cs=[c.c for c in cnds]
                # ローソク代替：高低線 + 終値ライン
                self.ax.plot(xs, cs, linewidth=1.2)
                for i in range(0, len(xs), max(1, len(xs)//60)):
                    self.ax.vlines(xs[i], ls[i], hs[i], linewidth=0.5)
                self.ax.set_title(f"{self._symbol_getter()} – 5m", color="white")
                self.ax.grid(True, alpha=0.3)
                self.ax.tick_params(axis='x', labelrotation=0)
                self.ax.set_facecolor("#11131a")
                self.fig.patch.set_facecolor(COLOR_BG)
                for spine in self.ax.spines.values():
                    spine.set_color("#333")
                self.ax.tick_params(colors="#ddd")
                self.ax.yaxis.label.set_color("#ddd")
            self.canvas.draw_idle()
        except Exception as e:
            LOGGER.error(f"Chart refresh error: {e}")
        finally:
            self.after(1500, self._refresh)

class App(tk.Tk):
    def __init__(self, provider: DataProviderBase, symbol="7203", poll=2.0):
        super().__init__()
        self.title("kabuS – Board/Tape + Chart + Tabs")
        self.geometry("1280x800+50+30")
        self.configure(bg=COLOR_BG)
        self.provider = provider
        self.symbol = tk.StringVar(value=symbol)
        self.poll = float(poll)
        self.prev_trade_px = None
        self.chart_win = None
        self._stop = False

        # OCO/トレール管理
        self.oco_tp = tk.StringVar(value="")
        self.oco_sl = tk.StringVar(value="")
        self.trail_sz = tk.StringVar(value="")

        # Notebook
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)

        style = ttk.Style()
        try:
            style.theme_use("default")
        except Exception:
            pass
        style.configure("Treeview", background=COLOR_BG, fieldbackground=COLOR_BG, foreground=COLOR_FG, rowheight=22)
        style.configure("TNotebook", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TNotebook.Tab", background="#11131a", foreground=COLOR_FG)
        style.map("TNotebook.Tab", background=[("selected", "#1b1f2a")])

        # タブ作成（実体は Part2 で実装）
        self._build_tab_board_tape()
        self._build_tab_assets_positions()
        self._build_tab_history()
        self._build_tab_screener()
        self._build_tab_settings_log()

        self._build_menu()

        # ポーリング開始（実体は Part3 で実装）
        self.provider.start()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self._log_drain_loop()

    # ====== メニュー ======
    def _build_menu(self):
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="エクスポート（表示中テーブル→CSV）", command=lambda: self._export_table("csv"))
        filemenu.add_command(label="エクスポート（表示中テーブル→XLSX）", command=lambda: self._export_table("xlsx"))
        filemenu.add_separator()
        filemenu.add_command(label="全建玉クローズ", command=self._close_all_positions)
        filemenu.add_separator()
        filemenu.add_command(label="終了", command=self._on_close)
        menubar.add_cascade(label="ファイル", menu=filemenu)

        viewmenu = tk.Menu(menubar, tearoff=0)
        viewmenu.add_command(label="チャートを別窓で開く", command=self._open_chart_window)
        menubar.add_cascade(label="表示", menu=viewmenu)

        self.config(menu=menubar)

    # ====== Part2 で埋めるプレースホルダ ======
    def _build_tab_board_tape(self): pass
    def _build_tab_assets_positions(self): pass
    def _build_tab_history(self): pass
    def _build_tab_screener(self): pass
    def _build_tab_settings_log(self): pass

    # ====== Part3 実装予定のコールバック ======
    def _poll_loop(self): ...
    def _log_drain_loop(self): ...
    def _update_board_and_tape(self): ...
    def _update_candles(self): ...
    def _update_positions(self): ...
    def _update_history_view(self): ...
    def _apply_oco_trail_checks(self): ...
    def _open_chart_window(self): ...
    def _export_table(self, kind): ...
    def _close_all_positions(self): ...
    def _on_close(self): ...
    # テーブル参照ヘルパ
    def _current_tree(self): ...
    # 5分足取得ヘルパ（ChartWindow用）
    def _get_candles(self): return self.provider.get_5m_candles(self.symbol.get(), limit=200)


# === Part 2 / 3 === ここから下を同じファイルに追記してください ===

    # ====== 板/歩み値/5分足 タブ ======
    def _build_tab_board_tape(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="板 / 歩み値 / 5分")

        top = ttk.Frame(frame)
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text="銘柄: ", foreground=COLOR_FG).pack(side="left")
        self.ent_symbol = ttk.Entry(top, width=10, textvariable=self.symbol)
        self.ent_symbol.pack(side="left", padx=4)

        ttk.Button(top, text="反映", command=lambda: LOGGER.info(f"symbol set: {self.symbol.get()}")).pack(side="left", padx=4)
        ttk.Button(top, text="チャート別窓", command=self._open_chart_window).pack(side="left", padx=8)

        # 中段：板 + 5分足（ダミー説明ラベル）
        mid = ttk.Frame(frame)
        mid.pack(fill="both", expand=True, padx=6, pady=(0,6))

        # 板（左）
        board_f = ttk.Frame(mid)
        board_f.pack(side="left", fill="y", padx=(0,6))
        ttk.Label(board_f, text="板", foreground=COLOR_FG).pack(anchor="w")
        cols_b = ("price", "qty")
        self.tv_bids = ttk.Treeview(board_f, columns=cols_b, show="headings", height=12)
        self.tv_bids.heading("price", text="Bid価格")
        self.tv_bids.heading("qty", text="数量")
        self.tv_bids.column("price", width=90, anchor="e")
        self.tv_bids.column("qty", width=80, anchor="e")
        self.tv_bids.pack()

        self.tv_asks = ttk.Treeview(board_f, columns=cols_b, show="headings", height=12)
        self.tv_asks.heading("price", text="Ask価格")
        self.tv_asks.heading("qty", text="数量")
        self.tv_asks.column("price", width=90, anchor="e")
        self.tv_asks.column("qty", width=80, anchor="e")
        self.tv_asks.pack(pady=(4,0))

        # 5分足は別窓で描くのでここは現値のみ簡易表示
        right = ttk.Frame(mid)
        right.pack(side="left", fill="both", expand=True)
        self.lbl_px = ttk.Label(right, text="--", foreground=COLOR_FG, font=("Meiryo UI", 20, "bold"))
        self.lbl_px.pack(anchor="w")
        ttk.Label(right, text="※ 詳細チャートは『チャート別窓』を開いてください", foreground="#bbb").pack(anchor="w")

        # 下段：歩み値
        btm = ttk.Frame(frame)
        btm.pack(fill="both", expand=True, padx=6, pady=(0,6))
        ttk.Label(btm, text="歩み値（最新200件）", foreground=COLOR_FG).pack(anchor="w")
        cols_t = ("time","price","qty")
        self.tv_tape = ttk.Treeview(btm, columns=cols_t, show="headings", height=14)
        for c, w, a in (("time",140,"center"),("price",100,"e"),("qty",80,"e")):
            self.tv_tape.heading(c, text=c.upper())
            self.tv_tape.column(c, width=w, anchor=a)
        self.tv_tape.tag_configure("UP", foreground=COLOR_UP)
        self.tv_tape.tag_configure("DN", foreground=COLOR_DN)
        self.tv_tape.tag_configure("EQ", foreground=COLOR_EQ)
        self.tv_tape.pack(fill="both", expand=True)

    # ====== 資金・建玉 + OCO/トレール ======
    def _build_tab_assets_positions(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="資金・建玉 / OCO / Trail")

        top = ttk.Frame(frame); top.pack(fill="x", padx=6, pady=6)
        self.lbl_cash = ttk.Label(top, text="現金: --", foreground=COLOR_FG, font=("Meiryo UI", 12, "bold"))
        self.lbl_cash.pack(side="left")
        ttk.Button(top, text="全建玉クローズ", command=self._close_all_positions).pack(side="right", padx=6)

        mid = ttk.Frame(frame); mid.pack(fill="both", expand=True, padx=6, pady=(0,6))
        cols = ("symbol","side","qty","avg","unreal","time")
        self.tv_pos = ttk.Treeview(mid, columns=cols, show="headings", height=12)
        for c, w, a in (("symbol",80,"center"),("side",60,"center"),("qty",80,"e"),
                        ("avg",100,"e"),("unreal",110,"e"),("time",160,"center")):
            self.tv_pos.heading(c, text=c.upper())
            self.tv_pos.column(c, width=w, anchor=a)
        self.tv_pos.pack(fill="both", expand=True)

        # 発注 & OCO/Trail
        ctl = ttk.LabelFrame(frame, text="発注 / OCO / トレール")
        ctl.pack(fill="x", padx=6, pady=6)

        self.var_side = tk.StringVar(value="BUY")
        ttk.Radiobutton(ctl, text="BUY", value="BUY", variable=self.var_side).grid(row=0, column=0, padx=4, pady=4)
        ttk.Radiobutton(ctl, text="SELL", value="SELL", variable=self.var_side).grid(row=0, column=1, padx=4, pady=4)

        ttk.Label(ctl, text="数量").grid(row=0, column=2)
        self.ent_qty = ttk.Entry(ctl, width=8)
        self.ent_qty.insert(0, "100")
        self.ent_qty.grid(row=0, column=3, padx=4)

        ttk.Label(ctl, text="価格(LMT省略可)").grid(row=0, column=4)
        self.ent_price = ttk.Entry(ctl, width=10); self.ent_price.grid(row=0, column=5, padx=4)

        ttk.Label(ctl, text="TP").grid(row=0, column=6)
        ent_tp = ttk.Entry(ctl, width=8, textvariable=self.oco_tp); ent_tp.grid(row=0, column=7, padx=4)
        ttk.Label(ctl, text="SL").grid(row=0, column=8)
        ent_sl = ttk.Entry(ctl, width=8, textvariable=self.oco_sl); ent_sl.grid(row=0, column=9, padx=4)
        ttk.Label(ctl, text="Trail幅").grid(row=0, column=10)
        ent_tr = ttk.Entry(ctl, width=8, textvariable=self.trail_sz); ent_tr.grid(row=0, column=11, padx=4)

        ttk.Button(ctl, text="成行(MKT)", command=lambda: self._send_order("MKT")).grid(row=1, column=0, columnspan=2, padx=4, pady=6, sticky="ew")
        ttk.Button(ctl, text="指値(LMT)", command=lambda: self._send_order("LMT")).grid(row=1, column=2, columnspan=2, padx=4, pady=6, sticky="ew")

    # ====== 履歴（SIM/LIVEフィルタ） ======
    def _build_tab_history(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="履歴（SIM/LIVE）")

        top = ttk.Frame(frame); top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="モード:").pack(side="left")
        self.var_hist_mode = tk.StringVar(value="ALL")
        ttk.Combobox(top, textvariable=self.var_hist_mode, values=["ALL","SIM","LIVE"], width=8).pack(side="left", padx=4)
        ttk.Button(top, text="更新", command=self._update_history_view).pack(side="left", padx=4)

        cols = ("time","symbol","side","qty","price","pnl_ticks","mode")
        self.tv_hist = ttk.Treeview(frame, columns=cols, show="headings", height=20)
        widths = (160,90,60,80,100,110,70)
        for c, w in zip(cols, widths):
            self.tv_hist.heading(c, text=c.upper())
            self.tv_hist.column(c, width=w, anchor="center" if c in ("time","symbol","side","mode") else "e")
        self.tv_hist.pack(fill="both", expand=True, padx=6, pady=(0,6))

    # ====== スクリーニング ======
    def _build_tab_screener(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="スクリーニング")

        top = ttk.Frame(frame); top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="監視銘柄:").pack(side="left")
        self.lb_watch = tk.Listbox(top, height=5, exportselection=False)
        self.lb_watch.pack(side="left", padx=6)
        for s in self.provider.get_watchlist():
            self.lb_watch.insert("end", s)

        ctl = ttk.Frame(top); ctl.pack(side="left", padx=10)
        ttk.Button(ctl, text="追加", command=self._add_watch).pack(fill="x", pady=2)
        ttk.Button(ctl, text="削除", command=self._del_watch).pack(fill="x", pady=2)

        filt = ttk.LabelFrame(frame, text="簡易フィルタ（ダミー計算）")
        filt.pack(fill="x", padx=6, pady=6)
        ttk.Label(filt, text="最低出来高").grid(row=0, column=0); self.ent_minv = ttk.Entry(filt, width=10); self.ent_minv.insert(0,"500"); self.ent_minv.grid(row=0, column=1, padx=4)
        ttk.Label(filt, text="5分 足の上昇率≧（%）").grid(row=0, column=2); self.ent_minchg = ttk.Entry(filt, width=10); self.ent_minchg.insert(0,"0.5"); self.ent_minchg.grid(row=0, column=3, padx=4)
        ttk.Button(filt, text="スクリーニング実行", command=self._run_screen).grid(row=0, column=4, padx=6)

        cols = ("symbol","chg_5m_%","vol")
        self.tv_screen = ttk.Treeview(frame, columns=cols, show="headings", height=18)
        for c, w in (("symbol",100),("chg_5m_%",110),("vol",100)):
            self.tv_screen.heading(c, text=c.upper()); self.tv_screen.column(c, width=w, anchor="e" if c!="symbol" else "center")
        self.tv_screen.pack(fill="both", expand=True, padx=6, pady=(0,6))

    # ====== 設定/ログ ======
    def _build_tab_settings_log(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="設定 / ログ")

        top = ttk.LabelFrame(frame, text="接続設定")
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text="現在モード").grid(row=0, column=0, sticky="w")
        self.lbl_mode = ttk.Label(top, text=self.provider.mode(), foreground=COLOR_FG, font=("Meiryo UI", 11, "bold"))
        self.lbl_mode.grid(row=0, column=1, sticky="w")

        ttk.Label(top, text="ポーリング秒").grid(row=0, column=2, sticky="e")
        self.ent_poll = ttk.Entry(top, width=8); self.ent_poll.insert(0, str(self.poll)); self.ent_poll.grid(row=0, column=3, padx=4)
        ttk.Button(top, text="適用", command=self._apply_poll).grid(row=0, column=4, padx=6)

        # ログ
        logf = ttk.LabelFrame(frame, text="ログ")
        logf.pack(fill="both", expand=True, padx=6, pady=6)
        self.txt_log = tk.Text(logf, height=16, bg="#0c0e14", fg="#dcdcdc")
        self.txt_log.pack(fill="both", expand=True)

    # ====== 各種操作 ======
    def _apply_poll(self):
        try:
            self.poll = max(0.5, float(self.ent_poll.get()))
            LOGGER.info(f"poll interval set to {self.poll}s")
        except Exception:
            messagebox.showerror("Error", "数値を入力してください")

    def _send_order(self, typ):
        try:
            side = self.var_side.get()
            qty = int(self.ent_qty.get())
            price = self.ent_price.get().strip()
            price = None if (typ == "MKT" or price == "") else float(price)
            tp = self.oco_tp.get().strip() or None
            sl = self.oco_sl.get().strip() or None
            tr = self.trail_sz.get().strip() or None
            if tp: tp = float(tp)
            if sl: sl = float(sl)
            if tr: tr = float(tr)
            o = OrderReq(self.symbol.get(), side, qty, price=price, type=typ, tp=tp, sl=sl, trail=tr)
            r = self.provider.place(o)
            if not r.get("ok"):
                messagebox.showerror("発注エラー", str(r))
                return
            LOGGER.info(f"ORDER OK: {side} {qty}@{r.get('avg_price')} tp={tp} sl={sl} tr={tr}")
        except Exception as e:
            messagebox.showerror("発注エラー", str(e))

    def _add_watch(self):
        s = self.symbol.get().strip()
        if not s: return
        if s not in self.provider.get_watchlist():
            # SimProviderの監視銘柄は簡易対応：ListBoxだけ反映
            self.lb_watch.insert("end", s)
            LOGGER.info(f"監視に追加: {s}")

    def _del_watch(self):
        sel = self.lb_watch.curselection()
        if not sel: return
        idx = sel[0]
        sym = self.lb_watch.get(idx)
        self.lb_watch.delete(idx)
        LOGGER.info(f"監視から削除: {sym}")

    def _run_screen(self):
        try:
            minv = int(self.ent_minv.get())
            minchg = float(self.ent_minchg.get())
        except Exception:
            messagebox.showerror("Error", "フィルタ数値が不正です")
            return
        self.tv_screen.delete(*self.tv_screen.get_children())
        # ダミー: watchlistごとに5分足の直近2本で上昇率・出来高推定
        for s in list(self.provider.get_watchlist()):
            cnds = self.provider.get_5m_candles(s, limit=2)
            if len(cnds) >= 2:
                chg = (cnds[-1].c - cnds[-2].c) / max(1e-9, cnds[-2].c) * 100.0
                vol = cnds[-1].v
                if (vol >= minv) and (chg >= minchg):
                    self.tv_screen.insert("", "end", values=(s, f"{chg:.2f}", pretty_num(vol)))
        LOGGER.info("スクリーニング完了")

# === Part 3 / 3 === ここから下を同じファイルに追記してください ===

    # ====== 背景ポーリング ======
    def _poll_loop(self):
        while not self._stop:
            try:
                self._update_board_and_tape()
                self._update_positions()
                self._apply_oco_trail_checks()
                # 履歴タブは明示更新ボタンでも更新できるが、ここでも軽く回す
                if self.nb.index(self.nb.select()) == 2:
                    self._update_history_view()
            except Exception as e:
                LOGGER.error(f"poll error: {e}")
            time.sleep(self.poll)

    def _log_drain_loop(self):
        try:
            while True:
                try:
                    line = LOGGER.q.get_nowait()
                    self.txt_log.insert("end", line + "\n")
                    self.txt_log.see("end")
                except queue.Empty:
                    break
        except Exception:
            pass
        self.after(300, self._log_drain_loop)

    def _update_board_and_tape(self):
        board, px, last_px = self.provider.get_board(self.symbol.get())
        # 板
        self.tv_bids.delete(*self.tv_bids.get_children())
        self.tv_asks.delete(*self.tv_asks.get_children())
        for pr, qt in board.get("bids", []):
            self.tv_bids.insert("", "end", values=(f"{pr:.1f}", pretty_num(qt)))
        for pr, qt in board.get("asks", []):
            self.tv_asks.insert("", "end", values=(f"{pr:.1f}", pretty_num(qt)))

        # 現値（色分け）
        if last_px is None: last_px = px
        col = COLOR_EQ
        if px > last_px: col = COLOR_UP
        elif px < last_px: col = COLOR_DN
        self.lbl_px.configure(text=f"{px:.1f}", foreground=col)

        # 歩み値
        tape = self.provider.get_tape(self.symbol.get(), limit=200)
        self.tv_tape.delete(*self.tv_tape.get_children())
        # 直近の上げ下げで色分け
        prev = None
        for t, price, qty in tape:
            if prev is None: tag = "EQ"
            else:
                if price > prev: tag = "UP"
                elif price < prev: tag = "DN"
                else: tag = "EQ"
            prev = price
            self.tv_tape.insert("", "end", values=(t.strftime("%H:%M:%S"), f"{price:.1f}", pretty_num(qty)), tags=(tag,))

    def _update_positions(self):
        # 資金
        cash = self.provider.get_cash()
        self.lbl_cash.configure(text=f"現金: {pretty_num(round(cash,2))}")
        # 建玉
        self.tv_pos.delete(*self.tv_pos.get_children())
        for p in self.provider.get_positions():
            self.tv_pos.insert("", "end", values=(
                p.symbol, p.side, pretty_num(p.qty), f"{p.avg_price:.1f}", pretty_num(round(p.unreal,1)), p.time.strftime("%Y-%m-%d %H:%M:%S")
            ))

    def _update_history_view(self):
        mode = self.var_hist_mode.get()
        rows = self.provider.get_trades(mode_filter=mode)
        self.tv_hist.delete(*self.tv_hist.get_children())
        for r in rows[-1000:]:
            self.tv_hist.insert("", "end", values=(
                r.time.strftime("%Y-%m-%d %H:%M:%S"),
                r.symbol, r.side, pretty_num(r.qty),
                f"{r.price:.1f}", pretty_num(round(r.pnl_ticks,1)), r.mode
            ))

    # ====== OCO / トレール判定（簡易） ======
    def _apply_oco_trail_checks(self):
        # SIM用の簡易ルール: TP/SL/Trailを Position.avg_price と現値で判定
        # 実際のOCOはブローカー側で管理するのが望ましい
        board, px, _ = self.provider.get_board(self.symbol.get())
        changed = False
        for p in list(self.provider.get_positions()):
            # TP/SLはTradeRecord側に埋めてないので省略（必要なら拡張可）
            # Trailは、self.trail_sz が指定済みのときに、含み益がTrail幅超えたらクローズする簡易版
            try:
                tr = self.trail_sz.get().strip()
                if tr:
                    tr = float(tr)
                    gain = (px - p.avg_price) if p.side == "LONG" else (p.avg_price - px)
                    if gain >= tr:
                        # クローズ
                        self.provider.close_all(symbol=p.symbol)
                        LOGGER.info(f"Trail exit: {p.symbol} gain={gain:.1f} >= {tr}")
                        changed = True
            except Exception:
                pass
        if changed:
            self._update_positions()
            self._update_history_view()

    # ====== チャート別窓 ======
    def _open_chart_window(self):
        if (self.chart_win is None) or (not tk.Toplevel.winfo_exists(self.chart_win)):
            self.chart_win = ChartWindow(self, self._get_candles, lambda: self.symbol.get())
        else:
            try:
                self.chart_win.focus_force()
            except Exception:
                pass

    # ====== エクスポート（表示中テーブル）=====
    def _current_tree(self):
        # 現在タブに応じて対象のTreeviewを返す
        idx = self.nb.index(self.nb.select())
        if idx == 0:
            return self.tv_tape
        if idx == 1:
            return self.tv_pos
        if idx == 2:
            return self.tv_hist
        if idx == 3:
            return self.tv_screen
        return None

    def _export_table(self, kind):
        tv = self._current_tree()
        if tv is None:
            messagebox.showwarning("Export", "このタブはエクスポート対象がありません")
            return
        rows = [tv.item(i, "values") for i in tv.get_children()]
        if not rows:
            messagebox.showwarning("Export", "出力対象の行がありません")
            return
        cols = tv["columns"]
        if kind == "csv":
            path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
            if not path: return
            try:
                if pd is None:
                    # pandas無い場合の簡易CSV
                    import csv
                    with open(path, "w", newline="", encoding="utf-8-sig") as f:
                        w = csv.writer(f)
                        w.writerow(cols)
                        for r in rows:
                            w.writerow(list(r))
                else:
                    df = pd.DataFrame(list(rows), columns=cols)
                    df.to_csv(path, index=False, encoding="utf-8-sig")
                LOGGER.info(f"CSV Exported: {path}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))
        else:
            if not _HAS_XLSX:
                messagebox.showwarning("Export", "XLSXライブラリが見つかりません。openpyxl か xlsxwriter を入れてください。")
                return
            path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel","*.xlsx")])
            if not path: return
            try:
                if pd is None:
                    messagebox.showwarning("Export", "pandas が必要です")
                    return
                df = pd.DataFrame(list(rows), columns=cols)
                df.to_excel(path, index=False)
                LOGGER.info(f"XLSX Exported: {path}")
            except Exception as e:
                messagebox.showerror("Export Error", str(e))

    def _close_all_positions(self):
        r = self.provider.close_all(symbol=None)
        if r.get("ok"):
            LOGGER.info(f"全建玉クローズ: PnL={round(r.get('closed_pnl',0.0),1)}")
            self._update_positions()
            self._update_history_view()
        else:
            messagebox.showerror("Error", str(r))

    def _on_close(self):
        self._stop = True
        try:
            self.provider.stop()
        except Exception:
            pass
        self.destroy()


# ====== main ======
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="7203", help="銘柄コード（例: 7203）")
    ap.add_argument("--poll", type=float, default=2.0, help="ポーリング秒")
    ap.add_argument("--sim", action="store_true", help="SIMモードで起動（既定）")
    ap.add_argument("--live", action="store_true", help="LIVEスタブで起動")
    args = ap.parse_args()

    if args.live and args.sim:
        print("live/sim はどちらか一方を指定してください。sim優先で起動します。", file=sys.stderr)

    provider = SimProvider(symbol=args.symbol) if (args.sim or not args.live) else LiveProviderStub(symbol=args.symbol)

    app = App(provider=provider, symbol=args.symbol, poll=args.poll)
    app.protocol("WM_DELETE_WINDOW", app._on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
