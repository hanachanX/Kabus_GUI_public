# -*- coding: utf-8 -*-
"""
Kabus GUI V4 – Skeleton (V3互換レイヤ付き)
- UI骨子は UI.py をベースに再構成（左ペイン：銘柄コードを入力できます）
- import群/定数部はV3の方針を引継ぎ（必要最小限のみ配置。詳細は今後追加）
- V3→V4互換レイヤ（属性/メソッドのエイリアス）を実装
- SIM/LIVE 履歴タブ：成績行（Trades/Win%/P&L/Avg）と CSV/XLSX 保存を実装
- サマリー右上は「保有＆株数のみ」（LIVE/SIM）

起動例：
    python -B Kabus_gui_v4_skeleton.py
"""
from __future__ import annotations
from spoofing import SpoofDetector

# ==== 標準・サードパーティ（V3方針を継承。必要最小限） ====
import os
import sys
import csv
import time
import math
import json
import datetime as dt
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import argparse
import requests  # HTTP (後で実装)
import websocket  # websocket-client (後で実装)
import threading


# ==== Tk / ttk ====
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# V3互換スタブ（日本語コメント付き）
try:
    from v4_compat_stubs_ja import CompatV3StubsJA
except Exception:
    # 手元にファイルがない場合でもビルド可能にするための空クラス
    class CompatV3StubsJA:
        pass

# ======================================================
# 定数（V3からの引継ぎ：必要に応じて今後拡張）
# ======================================================
APP_TITLE = "kabuS – Board/Tape + Chart + Tabs + Filters + Screener (v4 skeleton)"
EXCHANGE = 1
SECURITY_TYPE = 1
DEFAULT_TICK_SIZE = 0.5

# UIスタイル
_STYLE_HDR = ("Meiryo UI", 28, "bold")
_STYLE_SUB = ("Meiryo UI", 12)
_STYLE_SMALL = ("Meiryo UI", 9)

# ======================================================
# ユーティリティ
# ======================================================

def _to_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.strip().replace(",", "").replace("¥", "")
            if x in ("", "-", "—"):
                return default
        return float(x)
    except Exception:
        return default


# ======================================================
# アプリ本体
# ======================================================
class App(CompatV3StubsJA, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(1100, 680)
        self.after(0, lambda: self.state("zoomed"))

        # --- 主要状態変数（V3の命名を意識） ---
        self.symbol_var = tk.StringVar(value="8136")  # 左ペインで銘柄コードを入力
        self.auto_enabled = tk.BooleanVar(value=False)
        self.debug_mode = tk.BooleanVar(value=False)
        self.is_production = tk.BooleanVar(value=True)  # :18080 / :18081
        self.api_password = tk.StringVar()              # CLIから注入（表示欄は設けない）
        self.token = ""
        self.real_trade = tk.BooleanVar(value=False)  # ARM（実弾許可）
        self.qty = tk.IntVar(value=100)
        self._active_code = None              # 現在の銘柄コード（4桁）
        self._prev_close_by_code = {}         # 銘柄別の前日終値キャッシュ
        self._last_price_by_code = {}         # 銘柄別の現在値キャッシュ
        self._my_orders = {}        # {(side, price): qty} 画面上に載せたい自分の指値
        self._working   = {}        # {order_id: {"side":..,"price":..,"qty":..}} 監視対象
        self._polling_orders = False
        # AUTOフラグとスレッド
        self.auto_on = False
        self._auto_th = None
        self._auto_stop = threading.Event()
        self._auto_lock = threading.Lock()
        # AUTO設定（最初に dict を作る！）
        self.auto_cfg = {
            "qty": 100,
            "tp_ticks": 7,
            "sl_ticks": 5,
            "max_spread": 2.0,
            "min_abs_imb": 0.20,
            "require_inv": True,
            "poll_ms": 200,
            "tick_size": 1.0,
            # トレーリング
            "trail_on": False,
            "trail_ticks": 5,
            "trail_step_ticks": 1,
            "trail_arm_ticks": 3,
            "trail_to_be": True,
            "be_offset_ticks": 0,
        }
        self.spoof_cfg = {
            'enabled': True, 'window_ms': 3000, 'buffer_points': 150, 'k_big': 3.5,
            'min_lifespan_ms': 80, 'flash_max_ms': 800, 'layer_levels': 5, 'layer_need': 3,
            'layer_drop_ms': 900, 'walk_window_ms': 1400, 'walk_steps_need': 3,
            'score_threshold': 0.70, 'suppress_weight': 0.20,
        }
        self.spoof = SpoofDetector(self.spoof_cfg)
        self._last_spoof_str = ""
        self._last_print_side = None
        # トレーリングの内部状態
        self._trail_peak = None   # ロング時: 最高値(bid)、ショート時: 最安値(ask)
        self._trail_armed = False
        # SIMポジション/注文
        @dataclass
        class SimPosition:
            side: str = ""     # "BUY"/"SELL"
            qty: int = 0
            avg: float = 0.0
            entry_ts: str = ""
            entry_reason: str = ""

        @dataclass
        class SimOrder:
            id: int
            side: str
            price: float
            qty: int
            ts: str
            kind: str          # "ENTRY"/"TP"/"SL"
            status: str = "OPEN"
        # 成績行用の StringVar
        self.var_simstats = tk.StringVar(value="SIM: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")
        self.var_livestats = tk.StringVar(value="LIVE: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")

        # 資金関連
        self.cash_stock_wallet = tk.StringVar(value="—")  # 現物余力（株式）
        self.cash_bank         = tk.StringVar(value="—")  # 預り金/現金
        self.margin_wallet     = tk.StringVar(value="—")  # 信用新規建可能額
        self.margin_rate       = tk.StringVar(value="—")  # 委託保証金率(%)

        # サマリー（保有＆株数）
        self.var_live_pos = tk.StringVar(value="—")
        self.var_live_qty = tk.StringVar(value=str(self.qty.get()))
        self.var_sim_pos = tk.StringVar(value="—")
        self.var_sim_qty = tk.StringVar(value=str(self.qty.get()))

        self.sound_on = tk.BooleanVar(value=True)         # 「音」チェックの状態
        self.var_last_trade = tk.StringVar(value="約定：—")  # 手動スキャの「約定」表示



        # --- UI構築 ---
        self._build_ui()

        # --- 互換レイヤをインストール ---
        self._install_compat_aliases()

        # --- 起動時のセルフチェック（未定義メソッド）---
        missing = [
            n for n in [
                "start_training_log",  # V3で参照されがちなメソッド例
                "start_scan",
                "_open_help",
            ]
            if not callable(getattr(self, n, None))
        ]
        if missing:
            # スケルトンでは、ダイアログで告知しつつダミーを挿入
            messagebox.showwarning("未実装", f"未定義メソッド: {missing}\nダミーを仮挿入します。")
            for name in missing:
                setattr(self, name, lambda *a, **k: messagebox.showinfo("未実装", f"{name} は未実装です"))

        # __init__ などで（未定義なら）
        if not hasattr(self, "arm_var"):
            self.arm_var = tk.BooleanVar(value=False)  # デフォルトは実弾OFF

        self._update_sim_summary()
        # --- 1秒毎に成績ラベルを再集計（念のため）---
        self.after(1000, self._stats_heartbeat)

    # --------------------------------------------------
    # UI (UI.py を骨子に組み直し)
    # --------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Hdr.TLabel", font=_STYLE_HDR)
        style.configure("SubHdr.TLabel", font=_STYLE_SUB)
        style.configure("Small.TLabel", font=_STYLE_SMALL)
        style.configure("Group.TLabelframe.Label", font=("Meiryo UI", 10, "bold"))
        style.configure("Card.TLabelframe.Label", font=("Meiryo UI", 10, "bold"))
        style.configure("Card.TLabelframe", padding=8)

        # PanedWindow split
        pw = ttk.Panedwindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw, padding=(8, 8))
        right = ttk.Frame(pw, padding=(8, 8))
        pw.add(left, weight=1)
        pw.add(right, weight=4)

        # ===== LEFT: 操作/設定 =====
        lf_ctrl = ttk.LabelFrame(left, text="操作 / 設定", padding=8, style="Group.TLabelframe")
        lf_ctrl.pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(lf_ctrl, text="ARM（実弾許可）", variable=self.real_trade).pack(anchor="w")
        ttk.Checkbutton(lf_ctrl, text="DEBUG（ログ詳細）", variable=self.debug_mode).pack(anchor="w", pady=(2, 6))
        ttk.Checkbutton(lf_ctrl, text="AUTO ON/OFF", variable=self.auto_enabled).pack(anchor="w", pady=(0, 6))
        ttk.Button(lf_ctrl, text="設定保存",
           command=lambda: self._save_settings_dialog()).pack(fill="x")
        ttk.Button(lf_ctrl, text="使い方 / HELP", command=getattr(self, "_open_help", lambda: None)).pack(fill="x", pady=(6, 0))
        # 最低限の接続確認：トークン取得
        ttk.Button(lf_ctrl, text="トークン取得", command=self._get_token).pack(fill="x", pady=(6, 0))

        # --- 銘柄コード入力 ---
        lf_sym = ttk.LabelFrame(left, text="メイン銘柄（コード入力）", padding=8, style="Group.TLabelframe")
        lf_sym.pack(fill="x", pady=(8, 8))

        sym_row = ttk.Frame(lf_sym); sym_row.pack(fill="x")   # ← row をやめて固有名に
        ttk.Label(sym_row, text="コード:").pack(side="left")
        e = ttk.Entry(sym_row, textvariable=self.symbol_var, width=10)
        e.pack(side="left", padx=(6, 0))
        ttk.Button(sym_row, text="適用",
           command=lambda: self._apply_symbol()).pack(side="left", padx=(8, 0))
        try:
            e.bind("<Return>", lambda _e: self._apply_symbol())
        except Exception:
            pass

        # 数量
        lf_qty = ttk.LabelFrame(left, text="数量（デフォルト株数）", padding=8, style="Group.TLabelframe")
        lf_qty.pack(fill="x", pady=(0, 8))
        sp = ttk.Spinbox(lf_qty, from_=100, to=100000, increment=100, width=10, textvariable=self.qty)
        sp.pack(anchor="w")

        # スナップショット行（親を明示。rowは使わない）
        snap_row = ttk.Frame(lf_qty)            # ← lf_qty内にボタンを置くならこれが親
        snap_row.pack(fill="x", pady=(6, 0))

        import threading
        ttk.Button(
            snap_row,
            text="スナップショット取得",
            command=lambda: threading.Thread(target=self._snapshot_combo, daemon=True).start()
        ).pack(side="left", padx=(6, 0))

        # 補助フィルタ（ダミー）
        lf_filters = ttk.LabelFrame(left, text="補助フィルタ（メイン監視）", padding=8, style="Group.TLabelframe")
        lf_filters.pack(fill="x", pady=(0, 8))
        for txt in ("VWAP（順張り）", "SMA25/5m（順張り）", "MACD(12,26,9)", "RSI(14)", "Swing（高値切下げ/安値切上げ）"):
            ttk.Checkbutton(lf_filters, text=txt).pack(anchor="w")

        # ===== RIGHT: サマリー =====
        summary = ttk.Frame(right)
        summary.pack(fill="x")

        hdr_l = ttk.Frame(summary)
        hdr_l.pack(side="left", anchor="w", fill="x", expand=True)
        #ttk.Label(hdr_l, text="(銘柄名は後で実装)", style="Hdr.TLabel").pack(anchor="w")
        #ttk.Label(hdr_l, text="—", style="SubHdr.TLabel").pack(anchor="w")
        self.lbl_price  = tk.Label(hdr_l, font=("Meiryo UI", 24, "bold"))
        self.lbl_change = tk.Label(hdr_l, font=("Meiryo UI", 11))
        self.lbl_price.pack(anchor="w")
        self.lbl_change.pack(anchor="w")

        hdr_r = ttk.Frame(summary)
        hdr_r.pack(side="right", anchor="e")
        card_live = ttk.LabelFrame(hdr_r, text="LIVE", style="Card.TLabelframe")
        card_live.grid(row=0, column=0, sticky="e", padx=(0, 8))
        ttk.Label(card_live, textvariable=self.var_live_pos).grid(row=0, column=0, sticky="w")
        ttk.Label(card_live, textvariable=self.var_live_qty).grid(row=1, column=0, sticky="w")

        card_sim = ttk.LabelFrame(hdr_r, text="SIM", style="Card.TLabelframe")
        card_sim.grid(row=0, column=1, sticky="e")
        ttk.Label(card_sim, textvariable=self.var_sim_pos).grid(row=0, column=0, sticky="w")
        ttk.Label(card_sim, textvariable=self.var_sim_qty).grid(row=1, column=0, sticky="w")

        # インジ行（ダミー）
        self.lbl_misc = ttk.Label(right, text="pushes=0    VWAP:—   SMA25:—   MACD:—/—   RSI:—", style="Small.TLabel")
        self.lbl_misc.pack(fill="x", pady=(6, 8))

        # ===== Tabs =====
        tabs = ttk.Notebook(right)
        tabs.pack(fill="both", expand=True)
        self.main_nb = tabs
        self._build_tab_risk()  # ← _build_tab_risk は self.main_nb を使うのでそのままでOK
        

        # --- 板・歩み値 ---
        tab_tape = ttk.Frame(tabs); tabs.add(tab_tape, text="板・歩み値")

        # 上部バー：更新ボタン＆クリア
        bar = ttk.Frame(tab_tape); bar.pack(fill="x", padx=6, pady=(8,4))
        ttk.Button(bar, text="更新（板）",
                command=lambda: threading.Thread(target=self._snapshot_board, daemon=True).start()
        ).pack(side="left")
        ttk.Button(bar, text="クリア（歩み）",
                command=self._clear_tape
        ).pack(side="left", padx=(6,0))
        # デモ板・手動テスト
        self.demo_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="デモ板", variable=self.demo_on, command=self._toggle_demo).pack(side="left", padx=(12,0))
        ttk.Button(bar, text="1tick進める", command=self._demo_step_once).pack(side="left", padx=(6,0))
        ttk.Button(bar, text="強制約定(最良)", command=self._demo_force_fill).pack(side="left", padx=(6,0))

        # メトリクス行：Sp/Inv/Imbalance/成行き(1m)
        self.var_spread = tk.StringVar(value="Sp: —")
        self.var_inv    = tk.StringVar(value="Inv: 0")
        self.var_imbal  = tk.StringVar(value="Imb: —")
        self.var_mkt    = tk.StringVar(value="成行: 買0 / 売0 (1m)")

        self.metrics_bar = ttk.Frame(tab_tape); self.metrics_bar.pack(fill="x", padx=6)

        # 左側：Sp/Inv/Imb
        ttk.Label(self.metrics_bar, textvariable=self.var_spread).pack(side="left", padx=(0,12))
        ttk.Label(self.metrics_bar, textvariable=self.var_inv).pack(side="left", padx=(0,12))
        ttk.Label(self.metrics_bar, textvariable=self.var_imbal).pack(side="left", padx=(0,12))

        # 右側グループ（右寄せの塊を作る）
        self.metrics_right = ttk.Frame(self.metrics_bar); self.metrics_right.pack(side="right")

        # 見せ板バッジ（右側）
        self.lbl_spoof = ttk.Label(self.metrics_right, text="見せ板: なし", width=18, anchor='e')
        self.lbl_spoof.pack(side="right", padx=(12,0))

        # 成行(1m)
        ttk.Label(self.metrics_right, textvariable=self.var_mkt, style="Small.TLabel").pack(side="right")

        # 左右分割：左=歩み値 / 右=ラダー
        split = ttk.Panedwindow(tab_tape, orient="horizontal"); split.pack(fill="both", expand=True, padx=6, pady=(6,8))

        # 左（歩み値）
        lf = ttk.Frame(split); split.add(lf, weight=1)
        self.tree_tape = ttk.Treeview(lf, columns=("time","side","qty","price"), show="headings", height=16)
        vs_tape = ttk.Scrollbar(lf, orient="vertical", command=self.tree_tape.yview)
        self.tree_tape.configure(yscrollcommand=vs_tape.set)
        self.tree_tape.grid(row=0, column=0, sticky="nsew"); vs_tape.grid(row=0, column=1, sticky="ns")
        lf.rowconfigure(0, weight=1); lf.columnconfigure(0, weight=1)
        for k, txt, w, a in (("time","時刻",70,"center"), ("side","種別",50,"center"),
                            ("qty","枚数",80,"e"), ("price","価格",90,"e")):
            self.tree_tape.heading(k, text=txt); self.tree_tape.column(k, width=w, anchor=a)

        # 右（ラダー：AskQty | 価格 | BidQty）
        rf = ttk.Frame(split); split.add(rf, weight=2)
        self.ladder_cv = tk.Canvas(rf, bg="white", highlightthickness=0)
        self.ladder_cv.grid(row=0, column=0, sticky="nsew")
        rf.rowconfigure(0, weight=1); rf.columnconfigure(0, weight=1)
        # Canvas作成直後：
        self._ladder_width = 0
        self.ladder_cv.bind("<Configure>", self._on_ladder_resize)
        self.ladder_cv.bind("<Double-1>",        self._on_canvas_dbl_order)
        self.ladder_cv.bind("<Double-Button-1>", self._on_canvas_dbl_order)  # X11 対策


        # レイアウト既定（未定義なら）
        if not hasattr(self, "_LADDER_ROW_H"):   self._LADDER_ROW_H   = 22
        if not hasattr(self, "_LADDER_W_ASKMY"): self._LADDER_W_ASKMY = 60
        if not hasattr(self, "_LADDER_W_ASK"):   self._LADDER_W_ASK   = 90
        if not hasattr(self, "_LADDER_W_PRICE"): self._LADDER_W_PRICE = 90
        if not hasattr(self, "_LADDER_W_BID"):   self._LADDER_W_BID   = 90
        if not hasattr(self, "_LADDER_W_BIDMY"): self._LADDER_W_BIDMY = 60

        # 自分の指値保持（未定義なら）
        if not hasattr(self, "_my_orders"):
            self._my_orders = {}   # {(side, price): qty}
        '''
        self.ladder = ttk.Treeview(
            rf,
            columns=("ask_my","ask","price","bid","bid_my"),
            show="headings",
            height=28
        )
        vs_ladder = ttk.Scrollbar(rf, orient="vertical", command=self.ladder.yview)
        self.ladder.configure(yscrollcommand=vs_ladder.set)
        self.ladder.grid(row=0, column=0, sticky="nsew"); vs_ladder.grid(row=0, column=1, sticky="ns")
        rf.rowconfigure(0, weight=1); rf.columnconfigure(0, weight=1)
        
        # 見出しと幅
        self.ladder.heading("ask_my", text="[自分]");  self.ladder.column("ask_my", width=70, anchor="e")
        self.ladder.heading("ask",    text="ASK");     self.ladder.column("ask",    width=90, anchor="e")
        self.ladder.heading("price",  text="価格");     self.ladder.column("price",  width=100, anchor="center")
        self.ladder.heading("bid",    text="BID");     self.ladder.column("bid",    width=90, anchor="e")
        self.ladder.heading("bid_my", text="[自分]");  self.ladder.column("bid_my", width=70, anchor="w")

        # 色（自分列は緑・行のハイライトも用意）
        self.ladder.tag_configure("myask",  background="#e8f7e8")   # 自分の売りがある価格
        self.ladder.tag_configure("mybid",  background="#e8f7e8")   # 自分の買いがある価格
        self.ladder.tag_configure("myboth", background="#def3de")
        self.style = ttk.Style(self)
        self.style.map("Treeview", foreground=[("!disabled", "black")])  # 既定

        # 色（そのまま使い回しOK）
        self.ladder.tag_configure("ask",   foreground="#d26100")   # 売り側
        self.ladder.tag_configure("bid",   foreground="#1462ac")   # 買い側
        self.ladder.tag_configure("over",  foreground="#d26100")
        self.ladder.tag_configure("under", foreground="#1462ac")
        self.ladder.tag_configure("mid",   background="#e6f2ff")
        self.ladder.tag_configure("myask", background="#e8f7e8")  # 淡い緑
        self.ladder.tag_configure("mybid", background="#e8f7e8")
        self.ladder.tag_configure("myboth", background="#def3de")
        '''
        # --- 資金 ---
        tab_funds = ttk.Frame(tabs); tabs.add(tab_funds, text="資金")

        # 先頭に［更新］ボタン
        bar_funds = ttk.Frame(tab_funds); bar_funds.pack(fill="x", padx=6, pady=(8, 4))
        ttk.Button(bar_funds, text="更新", command=lambda: threading.Thread(target=self.update_wallets, daemon=True).start()
                ).pack(side="left")

        # 値表示（StringVarにバインド）
        grid = ttk.Frame(tab_funds); grid.pack(fill="x", padx=12, pady=8)
        def row(r, left, var):
            ttk.Label(grid, text=left).grid(row=r, column=0, sticky="w", pady=2, padx=(0,8))
            ttk.Label(grid, textvariable=var, width=16, anchor="e", style="Hdr.TLabel").grid(row=r, column=1, sticky="e")
        row(0, "現物余力(株式)", self.cash_stock_wallet)
        row(1, "預り金/現金",     self.cash_bank)
        row(2, "信用新規建可能額", self.margin_wallet)
        row(3, "委託保証金率",     self.margin_rate)
        for c in (0,1): grid.columnconfigure(c, weight=1)

        # --- 建玉 ---
        tab_pos = ttk.Frame(tabs); tabs.add(tab_pos, text="建玉")
        bar_pos = ttk.Frame(tab_pos); bar_pos.pack(fill="x", padx=6, pady=(8, 4))
        ttk.Button(bar_pos, text="更新", command=lambda: threading.Thread(target=self.update_positions, daemon=True).start()
          ).pack(side="left")
        self.tree_pos = ttk.Treeview(tab_pos, columns=("sym","name","side","qty","entry","pnl"), show="headings", height=10)
        self.tree_pos.pack(fill="both", expand=True, padx=6, pady=6)
        for k, txt, w, a in (
            ("sym", "銘柄", 80, "center"), ("name", "名称", 160, "w"), ("side", "売買", 60, "center"),
            ("qty", "数量", 80, "e"), ("entry", "建値", 90, "e"), ("pnl", "評価損益", 100, "e"),
        ):
            self.tree_pos.heading(k, text=txt); self.tree_pos.column(k, width=w, anchor=a)

        # --- 注文 ---
        tab_ord = ttk.Frame(tabs); tabs.add(tab_ord, text="注文")
        self.tree_ord = ttk.Treeview(tab_ord, columns=("id","sym","name","side","qty","price","st"), show="headings", height=12)
        self.tree_ord.pack(fill="both", expand=True, padx=6, pady=6)
        for k, txt, w, a in (
            ("id","ID",120,"w"),("sym","銘柄",60,"center"),("name","名称",160,"w"),
            ("side","売買",60,"center"),("qty","数量",80,"e"),("price","価格",90,"e"),("st","状態",100,"w"),
        ):
            self.tree_ord.heading(k, text=txt); self.tree_ord.column(k, width=w, anchor=a)

        # --- LIVE履歴 ---
        tab_live = ttk.Frame(tabs); tabs.add(tab_live, text="LIVE履歴")
        bar_live = ttk.Frame(tab_live); bar_live.pack(fill="x", pady=(6, 2), padx=6)
        ttk.Label(bar_live, textvariable=self.var_livestats, style="Small.TLabel").pack(side="left")
        ttk.Frame(bar_live).pack(side="left", padx=8)
        ttk.Button(bar_live, text="CSV保存", command=lambda: self._export_tree_csv(self.tree_live)).pack(side="right", padx=(6, 0))
        ttk.Button(bar_live, text="XLSX保存", command=lambda: self._export_tree_xlsx(self.tree_live)).pack(side="right")
        self.tree_live = ttk.Treeview(tab_live, columns=("t","sym","side","qty","entry","exit","ticks","pnl","reason"), show="headings", height=12)
        self.tree_live.pack(fill="both", expand=True, padx=6, pady=6)
        for k, txt, w, a in (
            ("t","時刻",130,"w"),("sym","銘柄",60,"center"),("side","売買",60,"center"),
            ("qty","数量",70,"e"),("entry","建値",90,"e"),("exit","決済",90,"e"),
            ("ticks","tick",60,"e"),("pnl","損益(円)",90,"e"),("reason","理由",160,"w"),
        ):
            self.tree_live.heading(k, text=txt); self.tree_live.column(k, width=w, anchor=a)

        # --- SIM履歴 ---
        tab_sim = ttk.Frame(tabs); tabs.add(tab_sim, text="SIM履歴")
        bar_sim = ttk.Frame(tab_sim); bar_sim.pack(fill="x", pady=(6, 2), padx=6)
        ttk.Label(bar_sim, textvariable=self.var_simstats, style="Small.TLabel").pack(side="left")
        ttk.Frame(bar_sim).pack(side="left", padx=8)
        ttk.Button(bar_sim, text="CSV保存", command=lambda: self._export_tree_csv(self.tree_sim)).pack(side="right", padx=(6, 0))
        ttk.Button(bar_sim, text="XLSX保存", command=lambda: self._export_tree_xlsx(self.tree_sim)).pack(side="right")
        self.tree_sim = ttk.Treeview(tab_sim, columns=("t","sym","side","qty","entry","exit","ticks","pnl","reason"), show="headings", height=12)
        self.tree_sim.pack(fill="both", expand=True, padx=6, pady=6)
        for k, txt, w, a in (
            ("t","時刻",130,"w"),("sym","銘柄",60,"center"),("side","売買",60,"center"),
            ("qty","数量",70,"e"),("entry","建値",90,"e"),("exit","決済",90,"e"),
            ("ticks","tick",60,"e"),("pnl","損益(円)",90,"e"),("reason","理由",160,"w"),
        ):
            self.tree_sim.heading(k, text=txt); self.tree_sim.column(k, width=w, anchor=a)

        # --- スクリーニング ---
        tab_scan = ttk.Frame(tabs); tabs.add(tab_scan, text="スクリーニング")
        bar = ttk.Frame(tab_scan); bar.pack(fill="x", pady=(8, 2), padx=6)
        ttk.Button(bar, text="スクリーニング開始", command=getattr(self, "start_scan", lambda: None)).pack(side="left")
        ttk.Button(bar, text="停止", command=getattr(self, "stop_scan", lambda: None)).pack(side="left", padx=(6, 0))
        self.tree_scan = ttk.Treeview(tab_scan, columns=("sym","name","score","note"), show="headings", height=12)
        self.tree_scan.pack(fill="both", expand=True, padx=6, pady=6)
        for k, txt, w, a in (
            ("sym","銘柄",70,"center"),("name","名称",160,"w"),("score","Score",70,"e"),("note","備考",300,"w"),
        ):
            self.tree_scan.heading(k, text=txt); self.tree_scan.column(k, width=w, anchor=a)

        # --- 資金管理 ---
        tab_rm = ttk.Frame(tabs); tabs.add(tab_rm, text="資金管理")
        frm = ttk.Frame(tab_rm); frm.pack(fill="x", padx=10, pady=10)
        colL = ttk.Frame(frm); colL.pack(side="left", fill="x", expand=True)
        ttk.Label(colL, text="日次損失上限（円）").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(colL, width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(colL, text="1トレード最大損失（円）").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(colL, width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(colL, text="連敗クールダウン（回）").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Spinbox(colL, from_=1, to=10, width=10, state="readonly").grid(row=2, column=1, sticky="w")
        ttk.Label(colL, text="クールダウン時間（分）").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Spinbox(colL, from_=1, to=60, width=10, state="readonly").grid(row=3, column=1, sticky="w")

        colR = ttk.Frame(frm); colR.pack(side="left", fill="x", expand=True, padx=(24, 0))
        ttk.Label(colR, text="同時ポジション上限（本）").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Spinbox(colR, from_=1, to=10, width=10, state="readonly").grid(row=0, column=1, sticky="w")
        ttk.Label(colR, text="寄付きエントリー停止（分）").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Spinbox(colR, from_=0, to=60, width=10, state="readonly").grid(row=1, column=1, sticky="w")
        ttk.Label(colR, text="日次停止ON").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Checkbutton(colR).grid(row=2, column=1, sticky="w")
        ttk.Button(tab_rm, text="資金管理の設定を保存", command=self._save_rm_settings_dialog).pack(anchor="e", padx=10, pady=(8, 10))

        # --- ログ ---
        import tkinter.scrolledtext as tkst

        tab_logs = ttk.Frame(tabs); tabs.add(tab_logs, text="ログ")
        # 理由（最新）
        self.var_reason = tk.StringVar(value="—")
        lf_reason = ttk.LabelFrame(tab_logs, text="理由（最新）", padding=6, style="Group.TLabelframe")
        lf_reason.pack(fill="x", padx=6, pady=(6, 0))
        ttk.Label(lf_reason, textvariable=self.var_reason, style="Small.TLabel",
                wraplength=1100, justify="left").pack(anchor="w")

        # （既存）ログ本文
        # 縦スクロール付きのテキスト
        self.log_box = tkst.ScrolledText(tab_logs, wrap="none", height=12, font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(6,0))
        # 横スクロールバー
        self.hbar = ttk.Scrollbar(tab_logs, orient="horizontal", command=self.log_box.xview)
        self.hbar.pack(fill="x", padx=6, pady=(0,6))
        self.log_box.configure(xscrollcommand=self.hbar.set)


        # 縦スクロール付きのテキスト
        self.log_box = tkst.ScrolledText(tab_logs, wrap="none", height=12, font=("Consolas", 10))
        self.log_box.pack(fill="both", expand=True, padx=6, pady=(6,0))

        # 横スクロールバー（ScrolledText は横が無いので別付け）
        self.hbar = ttk.Scrollbar(tab_logs, orient="horizontal", command=self.log_box.xview)
        self.hbar.pack(fill="x", padx=6, pady=(0,6))
        self.log_box.configure(xscrollcommand=self.hbar.set)


        # ---- 手動スキャル（ラダーの下に） ----
        pane = ttk.LabelFrame(rf, text="手動スキャル", padding=6)
        pane.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        pane.columnconfigure(0, weight=1)   # 左側を伸縮
        pane.columnconfigure(1, weight=0)

        # 変数の保険（未作成なら用意）
        if not hasattr(self, "scalp_qty"):
            self.scalp_qty = tk.IntVar(value=100)
        if not hasattr(self, "sound_on"):
            self.sound_on = tk.BooleanVar(self, value=True)
        if not hasattr(self, "var_last_trade"):
            self.var_last_trade = tk.StringVar(self, value="約定：—")
        if not hasattr(self, "var_fill"):
            self.var_fill = tk.StringVar(self, value="—")

        # --- 上段：右側に「約定：…」を表示 ---
        hdr = ttk.Frame(pane); hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        hdr.columnconfigure(0, weight=1)
        # 右寄せで最新約定を表示
        ttk.Label(hdr, textvariable=self.var_last_trade).pack(side="right", padx=(0, 0))

        # --- 中段：数量・±・Bid/Ask・音 ---
        row1 = ttk.Frame(pane); row1.grid(row=1, column=0, sticky="w", pady=(2, 4))
        ttk.Label(row1, text="数量").pack(side="left")
        e_qty = ttk.Entry(row1, textvariable=self.scalp_qty, width=8)
        e_qty.pack(side="left", padx=(4, 6))

        # ＋／−ボタン
        ttk.Button(row1, text="＋", width=3,
                command=lambda: self._inc_qty(+100)).pack(side="left")
        ttk.Button(row1, text="−", width=3,
                command=lambda: self._inc_qty(-100)).pack(side="left", padx=(4, 12))

        # マウスホイールで数量増減
        def _wheel_qty(evt):
            delta = 0
            if getattr(evt, "delta", 0) != 0:      # Windows
                delta = 100 if evt.delta > 0 else -100
            elif getattr(evt, "num", None) in (4,):  # Linux 上
                delta = +100
            elif getattr(evt, "num", None) in (5,):  # Linux 下
                delta = -100
            if delta:
                self._inc_qty(delta)
                return "break"

        e_qty.bind("<MouseWheel>", _wheel_qty)   # Windows
        e_qty.bind("<Button-4>", _wheel_qty)     # Linux 上
        e_qty.bind("<Button-5>", _wheel_qty)     # Linux 下

        # 指値ボタン
        import threading
        ttk.Button(
            row1, text="Bidに指値(買)", width=14,
            command=lambda: threading.Thread(
                target=self._place_limit_at_best, args=("BUY",), daemon=True
            ).start()
        ).pack(side="left")

        ttk.Button(
            row1, text="Askに指値(売)", width=14,
            command=lambda: threading.Thread(
                target=self._place_limit_at_best, args=("SELL",), daemon=True
            ).start()
        ).pack(side="left", padx=(6, 0))

        # 音チェック
        ttk.Checkbutton(row1, text="音", variable=self.sound_on).pack(side="left", padx=(12, 0))

        # --- 下段：成行決済・全取消・ステータス ---
        row2 = ttk.Frame(pane); row2.grid(row=2, column=0, sticky="ew")
        ttk.Button(
            row2, text="成行決済(フラット)", width=16,
            command=lambda: threading.Thread(
                target=self._manual_flatten_market, daemon=True
            ).start()
        ).pack(side="left")

        ttk.Button(
            row2, text="全取消(作業注文)", width=16,
            command=lambda: threading.Thread(
                target=self._cancel_all_working, daemon=True
            ).start()
        ).pack(side="left", padx=(6, 0))

        ttk.Label(row2, textvariable=self.var_fill, style="Small.TLabel").pack(side="left", padx=(12, 0))


    # --- 板の列幅修正 ---
    def _ladder_column_widths(self):
        """今の Canvas 幅に合わせて 5 列幅を返す（左右バランス良く）。"""
        cv = getattr(self, "ladder_cv", None)
        base_min = (40, 70, 70, 70, 40)        # 最低幅
        if cv is None:
            return (60, 90, 90, 90, 60)
        # 幅を確実に取得
        cw = int(cv.winfo_width())
        if cw <= 1:
            try:
                cv.update_idletasks()
                cw = int(cv.winfo_width())
            except Exception:
                cw = sum(base_min)
        # 比率: [自ASK]=1.0, ASK=1.6, 価格=1.2, BID=1.6, [自BID]=1.0
        weights = (1.0, 1.6, 1.2, 1.6, 1.0)
        sw = sum(weights)
        cols = [max(int(cw * w / sw), m) for w, m in zip(weights, base_min)]
        diff = cw - sum(cols)
        if diff:  # 端数は価格列に寄せる
            cols[2] += diff
        return tuple(cols)  # (W1,W2,W3,W4,W5)


    def _on_ladder_resize(self, ev):
        try:
            # 直近の行で再描画（列幅を現在幅で再計算）
            if getattr(self, "_ladder_rows", None):
                self._render_ladder(self._ladder_rows)
        except Exception as e:
            self._log_exc("UI", e)


    # --------------------------------------------------
    # 互換レイヤ（V3 → V4）
    # --------------------------------------------------
    def _install_compat_aliases(self) -> None:
        """V3で参照される属性/メソッド名をV4実体にマップする。
        追加があれば本関数内の辞書に1行追加する運用でOK。
        """
        # 属性名エイリアス（V3 → V4 実体）
        attr_alias: Dict[str, str] = {
            "tree_hist": "tree_sim",          # SIM履歴テーブル
            "lbl_stats": "var_simstats",      # 成績行（SIM）
            "lbl_simpos": "var_sim_pos",      # SIMサマリー（保有）
        }
        for old, new in attr_alias.items():
            if hasattr(self, old):
                continue
            if hasattr(self, new):
                setattr(self, old, getattr(self, new))

        # メソッドエイリアス（V3 → V4 実装）
        def _wrap_export_csv_sim():
            return self._export_tree_csv(self.tree_sim)

        def _wrap_export_xlsx_sim():
            return self._export_tree_xlsx(self.tree_sim)

        def _wrap_export_csv_live():
            return self._export_tree_csv(self.tree_live)

        def _wrap_export_xlsx_live():
            return self._export_tree_xlsx(self.tree_live)

        def _wrap_update_stats_sim():
            return self._update_stats_from_tree("SIM")

        def _wrap_update_stats_live():
            return self._update_stats_from_tree("LIVE")

        method_alias: Dict[str, Any] = {
            # V3 互換名 → V4 実体
            "export_sim_history_csv": _wrap_export_csv_sim,
            "export_sim_history_xlsx": _wrap_export_xlsx_sim,
            "export_live_history_csv": _wrap_export_csv_live,
            "export_live_history_xlsx": _wrap_export_xlsx_live,
            "_update_sim_stats_from_tree": _wrap_update_stats_sim,
            "_update_live_stats_from_tree": _wrap_update_stats_live,
            # 代表的な呼び出し口（中身は後で実装）
            "update_orders": self.update_orders,
            "_fill_orders": self._fill_orders,
            "_append_sim_history": self._append_sim_history,
            "_resolve_symbol_name": self._resolve_symbol_name,
        }
        for old, fn in method_alias.items():
            if not hasattr(self, old):
                setattr(self, old, fn)

    # ----------------------------
    # TP/SL　トレーリング
    # ----------------------------
    def _build_tab_risk(self):

        tab = ttk.Frame(self.main_nb); self.main_nb.add(tab, text="決済設定")

        frm = ttk.LabelFrame(tab, text="TP/SL（ticks）"); frm.pack(fill="x", padx=8, pady=(8,4))
        self.var_tp = tk.IntVar(value=self.auto_cfg["tp_ticks"])
        self.var_sl = tk.IntVar(value=self.auto_cfg["sl_ticks"])
        ttk.Label(frm, text="TP").grid(row=0, column=0, padx=6, pady=6, sticky="e")
        ttk.Spinbox(frm, from_=1, to=999, textvariable=self.var_tp, width=6).grid(row=0, column=1, padx=2, pady=6)
        ttk.Label(frm, text="SL").grid(row=0, column=2, padx=6, pady=6, sticky="e")
        ttk.Spinbox(frm, from_=1, to=999, textvariable=self.var_sl, width=6).grid(row=0, column=3, padx=2, pady=6)
        ttk.Label(frm, text=f"tick={self.auto_cfg['tick_size']}円").grid(row=0, column=4, padx=8, sticky="w")

        tr = ttk.LabelFrame(tab, text="トレーリング"); tr.pack(fill="x", padx=8, pady=(4,8))
        self.var_trail_on    = tk.BooleanVar(value=self.auto_cfg["trail_on"])
        self.var_trail_dist  = tk.IntVar(value=self.auto_cfg["trail_ticks"])
        self.var_trail_step  = tk.IntVar(value=self.auto_cfg["trail_step_ticks"])
        self.var_trail_arm   = tk.IntVar(value=self.auto_cfg["trail_arm_ticks"])
        self.var_trail_be    = tk.BooleanVar(value=self.auto_cfg["trail_to_be"])
        self.var_be_offset   = tk.IntVar(value=self.auto_cfg["be_offset_ticks"])

        ttk.Checkbutton(tr, text="トレーリング有効", variable=self.var_trail_on).grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Label(tr, text="距離(ticks)").grid(row=1, column=0, padx=6, pady=2, sticky="e")
        ttk.Spinbox(tr, from_=1, to=999, textvariable=self.var_trail_dist, width=6).grid(row=1, column=1, padx=2, pady=2)

        ttk.Label(tr, text="ステップ(ticks)").grid(row=1, column=2, padx=6, pady=2, sticky="e")
        ttk.Spinbox(tr, from_=1, to=100, textvariable=self.var_trail_step, width=6).grid(row=1, column=3, padx=2, pady=2)

        ttk.Label(tr, text="発動(建値から ticks)").grid(row=2, column=0, padx=6, pady=2, sticky="e")
        ttk.Spinbox(tr, from_=0, to=999, textvariable=self.var_trail_arm, width=6).grid(row=2, column=1, padx=2, pady=2)

        ttk.Checkbutton(tr, text="建値まで引上げ(BE)", variable=self.var_trail_be).grid(row=2, column=2, padx=6, pady=2, sticky="w")
        ttk.Label(tr, text="BEオフセット(ticks)").grid(row=2, column=3, padx=6, pady=2, sticky="e")
        ttk.Spinbox(tr, from_=0, to=50, textvariable=self.var_be_offset, width=6).grid(row=2, column=4, padx=2, pady=2)

        btns = ttk.Frame(tab); btns.pack(fill="x", padx=8, pady=(0,8))
        ttk.Button(btns, text="適用", command=self._apply_risk_from_ui).pack(side="left", padx=6)
        ttk.Button(btns, text="OCO再設定（現在の建玉）", command=self._refresh_oco_from_settings).pack(side="left", padx=6)


    def _apply_risk_from_ui(self):
        try:
            self.auto_cfg["tp_ticks"]        = int(self.var_tp.get())
            self.auto_cfg["sl_ticks"]        = int(self.var_sl.get())
            self.auto_cfg["trail_on"]        = bool(self.var_trail_on.get())
            self.auto_cfg["trail_ticks"]     = int(self.var_trail_dist.get())
            self.auto_cfg["trail_step_ticks"]= int(self.var_trail_step.get())
            self.auto_cfg["trail_arm_ticks"] = int(self.var_trail_arm.get())
            self.auto_cfg["trail_to_be"]     = bool(self.var_trail_be.get())
            self.auto_cfg["be_offset_ticks"] = int(self.var_be_offset.get())
            self._log("SET", f"決済設定を適用: TP={self.auto_cfg['tp_ticks']} SL={self.auto_cfg['sl_ticks']} "
                            f"TRAIL={'ON' if self.auto_cfg['trail_on'] else 'OFF'} "
                            f"dist={self.auto_cfg['trail_ticks']} step={self.auto_cfg['trail_step_ticks']} "
                            f"arm={self.auto_cfg['trail_arm_ticks']} BE={self.auto_cfg['trail_to_be']}(+{self.auto_cfg['be_offset_ticks']})")
        except Exception as e:
            try:
                self._log_exc("_apply_risk_from_ui", e)
            except TypeError:
                self._log_exc(e, where="_apply_risk_from_ui")

    def _refresh_oco_from_settings(self):
        """ 現在の建玉に対してTP/SLの未約定注文を最新設定で差し替え（SIM） """
        try:
            p = self._sim_pos
            if p.qty == 0: 
                self._log("AUTO", "OCO再設定: 建玉なし"); return

            # 既存のOPENなTP/SL指値をキャンセル扱いにして、新しく置き直し
            for o in self._sim_orders:
                if o.status=="OPEN" and o.kind in ("TP","SL"): o.status="CANCELLED"

            t = self.auto_cfg["tp_ticks"] * self.auto_cfg["tick_size"]
            s = self.auto_cfg["sl_ticks"] * self.auto_cfg["tick_size"]
            if p.side == "BUY":
                self._place_sim_limit("SELL", p.avg + t, p.qty, "TP")
                self._place_sim_limit("SELL", p.avg - s, p.qty, "SL")
            else:
                self._place_sim_limit("BUY",  p.avg - t, p.qty, "TP")
                self._place_sim_limit("BUY",  p.avg + s, p.qty, "SL")

            # トレーリング状態もリセット
            self._trail_peak = None; self._trail_armed = False
            self._log("AUTO", "OCO再設定済み（TP/SLを最新設定に更新）")
        except Exception as e:
            try:
                self._log_exc("refresh_oco_from_settings", e)
            except TypeError:
                # もし _log_exc(e, where=...) 型の実装ならこちら
                self._log_exc(e, where="refresh_oco_from_settings")

    def _find_open_order(self, kind: str):
        for o in self._sim_orders:
            if o.status == "OPEN" and o.kind == kind:
                return o
        return None

    def _update_trailing(self, q):
        """ 建玉がある時にSL指値を価格進行に合わせて更新（SIM） """
        try:
            if not self.auto_cfg["trail_on"]: return
            p = self._sim_pos
            if p.qty == 0: return

            tick = self.auto_cfg["tick_size"]
            dist = self.auto_cfg["trail_ticks"] * tick
            step = self.auto_cfg["trail_step_ticks"] * tick
            arm  = self.auto_cfg["trail_arm_ticks"] * tick
            be_on= self.auto_cfg["trail_to_be"]
            be_of= self.auto_cfg["be_offset_ticks"] * tick

            sl = self._find_open_order("SL")
            if not sl: return  # SLがなければ何もしない

            # 有利方向の“到達点”を更新
            if p.side == "BUY":
                ref = q["bid"] or q["last"] or 0.0
                if ref <= 0: return
                self._trail_peak = ref if (self._trail_peak is None) else max(self._trail_peak, ref)
                gained = (self._trail_peak - p.avg)
                if gained >= arm: 
                    self._trail_armed = True
                if not self._trail_armed: 
                    return

                # 目標SL（距離=distを保つ）。BEモードが有効かつ十分進んだら建値(or +offset)を下限に
                target_sl = self._trail_peak - dist
                if be_on:
                    target_sl = max(target_sl, p.avg + be_of)

                # ステップ更新：現在のSLより step 以上 上がる時だけ更新
                if target_sl > sl.price + step:
                    old = sl.price
                    sl.price = target_sl
                    self._auto_log("トレーリング更新", f"SL {old:.1f}→{sl.price:.1f}", q, self._derive_metrics(q))

            else:  # SELL
                ref = q["ask"] or q["last"] or 0.0
                if ref <= 0: return
                self._trail_peak = ref if (self._trail_peak is None) else min(self._trail_peak, ref)
                gained = (p.avg - self._trail_peak)
                if gained >= arm:
                    self._trail_armed = True
                if not self._trail_armed:
                    return

                target_sl = self._trail_peak + dist
                if be_on:
                    target_sl = min(target_sl, p.avg - be_of)

                if target_sl < sl.price - step:
                    old = sl.price
                    sl.price = target_sl
                    self._auto_log("トレーリング更新", f"SL {old:.1f}→{sl.price:.1f}", q, self._derive_metrics(q))
        except Exception as e:
            try:
                self._log_exc("_update_trailing", e)
            except TypeError:
                self._log_exc(e, where="_update_trailing")





    #---------------
    # 資金　建玉
    #---------------
    def update_wallets(self):
        """資金情報をAPIから取得してUIへ反映"""
        if not getattr(self, "token", ""):
            return self._log("warning", "Token未取得です（先にトークン取得）")

        base = self._base_url(); h = {"X-API-KEY": self.token}
        endpoints = ("/wallet/cash", "/wallet/margin", "/wallet/stock")

        merged, merged_ci = {}, {}  # 大文字/小文字を吸収して統合
        for p in endpoints:
            url = base + p
            try:
                r = requests.get(url, headers=h, timeout=8)
                self._log("HTTP", f"GET {url} -> {r.status_code} {(r.text or '')[:200]}...")
                r.raise_for_status()
                j = r.json() if r.text else {}
                if isinstance(j, dict):
                    for k, v in j.items():
                        merged[k] = v; merged_ci[k.lower()] = v
            except Exception as e:
                self._log_exc("HTTP", e)

        def pick_ci(keys, default=0.0):
            for k in keys:
                if k in merged and merged[k] is not None:
                    return self._pick_num(merged, k, default=default)
                lk = k.lower()
                if lk in merged_ci and merged_ci[lk] is not None:
                    v = merged_ci[lk]
                    try:
                        if isinstance(v, str): v = v.replace(",", "")
                        return float(v)
                    except Exception:
                        pass
            return default

        # 候補キーはV3と同様に広めに
        stock_wallet = pick_ci(["StockAccountWallet","StockAccountBalance","StockBalance","CashStock","CashStockBalance"], 0.0)
        cash_bank    = pick_ci(["CashDeposits","Cash","Deposit","AvailableAmount","BankBalance","Collateral.Cash"], 0.0)
        margin_avail = pick_ci(["MarginAvailable","MarginAccountWallet","MarginAccountBalance","MarginAvail","BuyingPower","MarginBuyingPower"], 0.0)
        margin_rate  = pick_ci(["ConsignmentDepositRate","CashOfConsignmentDepositRate","DepositKeepRate","DepositkeepRate","MarginRequirement","RequiredMarginRate","MarginRate"], 0.0)

        # UI反映（UIスレッドで）
        self.ui_call(lambda: self.cash_stock_wallet.set(f"{int(stock_wallet):,}"))
        self.ui_call(lambda: self.cash_bank.set(f"{int(cash_bank):,}"))
        self.ui_call(lambda: self.margin_wallet.set(f"{int(margin_avail):,}"))
        # APIにより 0.45 のような値もあるため % 表示を統一
        if margin_rate > 1.0:   # たとえば 45 (％) で来る実装用
            self.ui_call(lambda: self.margin_rate.set(f"{margin_rate:.1f}%"))
        else:                   # 0.45 のような小数なら *100
            self.ui_call(lambda: self.margin_rate.set(f"{margin_rate*100:.1f}%"))

            def sum_ci(keys):
                s, hit = 0.0, False
                for k in keys:
                    lk = k.lower()
                    val = merged.get(k, None)
                    if val is None: val = merged_ci.get(lk, None)
                    if val is not None:
                        try: s += float(val); hit = True
                        except: pass
                return (s if hit else 0.0), hit

            # 現物余力(株式)
            stock_wallet = pick_ci(["StockAccountWallet","StockAccountBalance","StockBalance","CashStock","CashStockBalance"], 0.0)
            aux_sum, aux_hit = sum_ci(["AuKCStockAccountWallet","AuJbnStockAccountWallet"])
            if aux_hit:
                stock_wallet = aux_sum
                self._log("HTTP", f"wallet: using AuKC+AuJbn sum = {stock_wallet}")

            # 預り金/現金
            cash_bank = pick_ci(["CashDeposits","Cash","Deposit","AvailableAmount","BankBalance","Collateral.Cash"], 0.0)

            # 信用新規建可能額
            margin_avail = pick_ci(["MarginAvailable","MarginAccountWallet","MarginAccountBalance","MarginAvail","BuyingPower","MarginBuyingPower"], 0.0)

            # 委託保証金率（％）
            margin_rate = pick_ci(["ConsignmentDepositRate","CashOfConsignmentDepositRate","DepositKeepRate","DepositkeepRate","MarginRequirement","RequiredMarginRate","MarginRate"], 0.0)

            # UI反映
            self.ui_call(self.cash_stock_wallet.set, f"{int(stock_wallet):,}")
            self.ui_call(self.cash_bank.set,         f"{int(cash_bank):,}")
            self.ui_call(self.margin_wallet.set,     f"{int(margin_avail):,}")
            self.ui_call(self.margin_rate.set,       f"{margin_rate*100:.1f}%")


    def update_positions(self):
        """ /positions を取得して TreeView に反映 """
        if not getattr(self, "token", ""):
            return self._log("warning", "Token未取得です（先にトークン取得）")
        url = self._base_url() + "/positions"
        try:
            r = requests.get(url, headers={"X-API-KEY": self.token}, timeout=10)
            self._log("HTTP", f"GET {url} -> {r.status_code} {(r.text or '')[:200]}...")
            r.raise_for_status()
            j = r.json()
            rows = j if isinstance(j, list) else (j.get("Positions") or [])
            self.ui_call(lambda: self._fill_positions(rows))
        except Exception as e:
            self._log_exc("HTTP", e)



    def _fill_positions(self, rows):
        """
        TreeViewへ描画。建値・P/Lが0にならないように堅牢化。
        - 建値: 複数候補キーを探索
        - P/L: 既存キーが無ければ 現在値×数量で算出（買=+ / 売=-）
        - 現在値: 無ければ /board を叩く（銘柄別キャッシュ）
        """
        import re

        QTY_KEYS   = ["HoldQty", "HoldingQty", "HoldQuantity", "Qty", "Quantity",
                    "LeavesQty", "LeavesQuantity", "QtyRemaining", "Leaves",
                    "LongQty", "ShortQty"]
        ENTRY_KEYS = ["Price", "HoldPrice", "AveragePrice", "AvgPrice",
                    "ExecutionPrice", "ExecutionAvgPrice", "UnitPrice",
                    "ContractPrice", "EntryPrice", "OpenPrice"]
        PL_KEYS    = ["ProfitLoss", "ValuationProfitLoss", "ValuationPL",
                    "UnrealizedProfitLoss", "UnrealizedPL", "PL", "GainLoss"]

        def detect_qty(p):
            # 既知キー
            for k in QTY_KEYS:
                v = self._to_float(p.get(k))
                if v is not None and abs(v) > 0:
                    return int(round(abs(v))), k
            # 総当り（qty/quantity を含むキー）
            for k, v in p.items():
                if isinstance(k, str) and re.search(r"qty|quantity", k, re.I):
                    fv = self._to_float(v)
                    if fv is not None and abs(fv) > 0:
                        return int(round(abs(fv))), k
            return 0, None

        def detect_entry(p):
            for k in ENTRY_KEYS:
                v = self._to_float(p.get(k))
                if v is not None and v > 0:
                    return v, k
            return None, None

        def detect_current(p, sym):
            # ポジションオブジェクト内 or /board
            for k in ("CurrentPrice", "NowPrice", "ValuationPrice", "CurrentValuationPrice"):
                v = self._to_float(p.get(k))
                if v is not None and v > 0:
                    return v, k
            cp = self._get_current_price_for_symbol(sym)
            return (cp, "board") if cp is not None else (None, None)

        def detect_pl(p):
            for k in PL_KEYS:
                v = self._to_float(p.get(k))
                if v is not None:
                    return int(round(v)), k
            return None, None

        def detect_side(p):
            s = str(p.get("Side", "")).strip().upper()
            if s in ("2", "BUY", "B", "LONG", "買"):  return "買"
            if s in ("1", "SELL", "S", "SHORT", "売"): return "売"
            return "買"

        try:
            # クリア
            for iid in self.tree_pos.get_children():
                self.tree_pos.delete(iid)

            # デバッグ：キー一覧
            if rows:
                sample = rows[0]
                keys = ", ".join(list(sample.keys()))
                self._log("DEBUG", f"/positions keys: {keys}")

            for p in rows or []:
                sym   = str(p.get("Symbol", ""))  # 例 "7203"
                name  = self._resolve_symbol_name(sym) or str(p.get("SymbolName", ""))
                side  = detect_side(p)

                qty,   qty_key   = detect_qty(p)
                entry, entry_key = detect_entry(p)
                pl,    pl_key    = detect_pl(p)

                # 現在値（PL算出のため）
                cur, cur_key = detect_current(p, sym)

                # PLが無ければ算出
                if pl is None and entry is not None and qty > 0 and cur is not None:
                    if side == "買":
                        pl = int(round((cur - entry) * qty))
                    else:
                        pl = int(round((entry - cur) * qty))
                    pl_key = f"computed:{cur_key or 'cur'}"

                # デバッグ（各キーの出どころは1度だけ表示）
                if qty_key:   self._log("DEBUG", f"qty via '{qty_key}' = {qty}", dedup_key=f"qtykey:{qty_key}")
                if entry_key: self._log("DEBUG", f"entry via '{entry_key}' = {entry}", dedup_key=f"entrykey:{entry_key}")
                if pl_key:    self._log("DEBUG", f"pl via '{pl_key}' = {pl}",       dedup_key=f"plkey:{pl_key}")

                self.tree_pos.insert(
                    "", "end",
                    values=(sym, name, side, qty, entry if entry is not None else 0.0, pl if pl is not None else 0)
                )
        except Exception as e:
            self._log_exc("UI", e)


    def _to_float(self, v):
        if v is None:
            return None
        try:
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            return float(v)
        except Exception:
            return None

    # ----------- ARM GUARD ------------------

    def _is_live_permitted(self) -> bool:
        """ARM（実弾許可）トグルの状態を返す。未設定なら False。"""
        try:
            v = getattr(self, "arm_var", None)
            return bool(v.get()) if v is not None else False
        except Exception:
            return False

 
    # ----------- 板　歩み値 ------------------
    def _inc_qty(self, delta:int):
        try:
            v = int(self.scalp_qty.get() or 0)
            v = max(0, v + int(delta))
            self.scalp_qty.set(v)
        except Exception:
            self.scalp_qty.set(0)


    def _extract_levels(self, j: dict, depth: int = 10):
        """/board JSON から (sell_levels, buy_levels, over, under) を抽出
        - sell_levels: [(price, qty)]  Sell1..SellN（Sell1=最良売）
        - buy_levels : [(price, qty)]  Buy1..BuyN（Buy1=最良買）
        """
        sells, buys = [], []
        # 形式A: Sell1..Sell10 / Buy1..Buy10
        for i in range(1, depth+1):
            s = j.get(f"Sell{i}") or j.get(f"Ask{i}")
            b = j.get(f"Buy{i}")  or j.get(f"Bid{i}")
            if isinstance(s, dict):
                sells.append((self._to_float(s.get("Price")), self._to_float(s.get("Qty"))))
            if isinstance(b, dict):
                buys.append((self._to_float(b.get("Price")),  self._to_float(b.get("Qty"))))
        # 形式B: "Asks":[{Price,Qty},...], "Bids":[...]
        if not sells and isinstance(j.get("Asks"), list):
            sells = [(self._to_float(x.get("Price")), self._to_float(x.get("Qty"))) for x in j["Asks"][:depth]]
        if not buys  and isinstance(j.get("Bids"), list):
            buys  = [(self._to_float(x.get("Price")), self._to_float(x.get("Qty"))) for x in j["Bids"][:depth]]
        # None除去
        sells = [(p or 0.0, q or 0.0) for (p,q) in sells if p is not None]
        buys  = [(p or 0.0, q or 0.0) for (p,q) in buys  if p is not None]
        # OVER/UNDER
        over  = self._to_float(j.get("OverSellQty")  or j.get("OverSellQuantity")  or 0) or 0
        under = self._to_float(j.get("UnderBuyQty")  or j.get("UnderBuyQuantity")  or 0) or 0
        return sells, buys, int(over), int(under)

    def _build_ladder_rows(self, sells, buys, over:int, under:int):
        rows = []
        if (over or 0) > 0:
            rows.append((f"{int(over):,}", "OVER", "", "ask"))
        for p, q in reversed(sells):
            rows.append((f"{int(q):,}", f"{p:,.1f}", "", "ask"))
        for p, q in buys:
            rows.append(("", f"{p:,.1f}", f"{int(q):,}", "bid"))
        if (under or 0) > 0:
            rows.append(("", "UNDER", f"{int(under):,}", "bid"))
        return rows


    def _render_ladder(self, base_rows):
        """
        base_rows: [(ask_txt, price_txt, bid_txt, tag)]
        Canvasに [自ASK|ASK|価格|BID|自BID] を色分けで描画
        （自分=黒、ASK=オレンジ、BID=ブルー、価格=黒）
        """
        cv = getattr(self, "ladder_cv", None)
        if cv is None:
            return self._log("UI", "ladder_cv が未生成です")

        # 行高
        H = getattr(self, "_LADDER_ROW_H", 22)

        # 列幅（現在のCanvas幅にフィット）
        W1, W2, W3, W4, W5 = self._ladder_column_widths()

        # 色
        ASK_CLR = "#d26100"; BID_CLR = "#1462ac"; FG = "#222222"; MY = "#000000"; GRID = "#dddddd"

        my = getattr(self, "_my_orders", {}) or {}

        self._ladder_rows = list(base_rows or [])
        self._ladder_price_at_row = {}
        cv.delete("all")

        def fnum(s):
            try:    return float(str(s).replace(",", "").strip())
            except: return None

        y = 1
        for i, (ask_txt, price_txt, bid_txt, _tag) in enumerate(self._ladder_rows):
            p = fnum(price_txt)
            mya = my.get(("SELL", p), 0) if p is not None else 0
            myb = my.get(("BUY",  p), 0) if p is not None else 0
            ask_my = f"{mya:,}" if mya else ""
            bid_my = f"{myb:,}" if myb else ""

            x = 0
            cv.create_text(x+W1-4, y+H/2, text=ask_my,           anchor="e", fill=MY);     x += W1
            cv.create_text(x+W2-4, y+H/2, text=(ask_txt or ""),   anchor="e", fill=ASK_CLR); x += W2
            cv.create_text(x+W3/2, y+H/2, text=(price_txt or ""), anchor="c", fill=FG);     x += W3
            cv.create_text(x+4,    y+H/2, text=(bid_txt or ""),   anchor="w", fill=BID_CLR); x += W4
            cv.create_text(x+4,    y+H/2, text=bid_my,            anchor="w", fill=MY)

            self._ladder_price_at_row[i] = p
            y += H

        total_w = sum((W1, W2, W3, W4, W5))
        # 罫線
        cv.create_line(0, 0, total_w, 0, fill=GRID)
        cv.create_line(0, y-1, total_w, y-1, fill=GRID)
        xx = 0
        for w in (W1, W2, W3, W4, W5):
            cv.create_line(xx, 0, xx, y, fill=GRID); xx += w

        # キャンバス領域を幅いっぱいに（右側の無駄な余白を消す）
        cv.config(scrollregion=(0, 0, total_w, y))



    #def _update_ladder_widget(self, base_rows):
    #    """
    #    base_rows: [(ask_txt, price_txt, bid_txt, tag)]
    #    自分の発注を価格で突き合わせ、5列（ask_my, ask, price, bid, bid_my）で描画。
    #    """
    def _update_ladder_widget(self, base_rows):
        """互換：旧Treeview呼び出し名 → 新Canvas描画に委譲"""
        try:
            return self._render_ladder(base_rows)
        except Exception as e:
            self._log_exc("UI", e)

        '''
        try:
            for iid in self.ladder.get_children():
                self.ladder.delete(iid)

            def fnum(s):
                try: return float(str(s).replace(",", ""))
                except: return None

            for ask, price, bid, base_tag in base_rows:
                p = fnum(price)
                mya = self._my_orders.get(("SELL", p), 0) if p is not None else 0
                myb = self._my_orders.get(("BUY",  p), 0) if p is not None else 0

                ask_my = f"{mya:,}" if mya else ""
                bid_my = f"{myb:,}" if myb else ""

                tags = [base_tag]
                if mya and myb:   tags.append("myboth")
                elif mya:         tags.append("myask")
                elif myb:         tags.append("mybid")
                #self.ladder.insert("", "end", values=(...), tags=tuple(tags))

                self.ladder.insert("", "end", values=(ask_my, ask, price, bid, bid_my), tags=tuple(tags))
        except Exception as e:
            self._log_exc("UI", e)
        '''


    def _add_my_order(self, side:str, price:float, qty:int, order_id:str=None):
        """自分の指値を保持して描画に反映（SIM/LIVE共通）。"""
        if not hasattr(self, "_my_orders"): self._my_orders = {}
        key = (side, float(price))
        self._my_orders[key] = self._my_orders.get(key, 0) + int(qty)
        if order_id:
            if not hasattr(self, "_working_by_price"): self._working_by_price = {}
            self._working_by_price[key] = order_id
        self._redraw_ladder_from_last_board()

    def _remove_my_order(self, side:str, price:float, qty:int=None):
        """自己注文の削除/減算（qty=None で完全削除）。"""
        if not hasattr(self, "_my_orders"): return
        key = (side, float(price))
        if key not in self._my_orders: return
        if qty is None:
            self._my_orders.pop(key, None)
        else:
            self._my_orders[key] = max(0, int(self._my_orders[key]) - int(qty))
            if self._my_orders[key] == 0:
                self._my_orders.pop(key, None)
        # 紐づく OrderId も忘れる
        if hasattr(self, "_working_by_price"):
            self._working_by_price.pop(key, None)
        self._redraw_ladder_from_last_board()

    def _redraw_ladder_from_last_board(self):
        """最後に取得した板JSONから再描画。"""
        try:
            j = getattr(self, "_last_board_json", None)
            if not j: return
            depth = max(1, min(10, int(getattr(self, "book_depth", None).get() if hasattr(self,"book_depth") else 10)))
            sells, buys, over, under = self._extract_levels(j, depth=depth)
            rows = self._build_ladder_rows(sells, buys, over, under)
            self.ui_call(self._render_ladder, rows)
        except Exception:
            pass



    def _place_limit_at_best(self, side: str):
        """side='BUY' なら Bid1 に、'SELL' なら Ask1 に指値を置く"""
        try:
            if not getattr(self, "token", ""):
                return self._log("warning", "Token未取得です")

            # 直近の板から価格を得る（無ければ /board）
            j = getattr(self, "_last_board_json", None)
            if not j:
                base = getattr(self, "_get_base_code", lambda: (self.symbol_var.get() or "").strip())()
                code_ex = base if "@" in (self.symbol_var.get() or "") else f"{base}@{getattr(self,'EXCHANGE',1)}"
                j = self._http_get("BOARD", f"/board/{code_ex}")
                self._last_board_json = j

            sells, buys, over, under = self._extract_levels(j, depth=min(getattr(self, "book_depth", tk.IntVar(value=10)).get(), 10))
            if side == "BUY":
                if not buys: return self._log("warning", "Bidが取得できていません")
                price = buys[0][0]
            else:
                if not sells: return self._log("warning", "Askが取得できていません")
                price = sells[0][0]

            qty = int(self.scalp_qty.get() or 0)
            if qty <= 0: return self._log("warning", "数量が0です")

            # 実弾許可がOFFなら画面に載せるだけ
            if not self._is_live_permitted():
                self._log("SIM", f"{side} 指値 {price} x{qty}")
                self._add_my_order(side, price, qty)   # 画面にマーク
                return

            payload = {
                "Symbol": (self.symbol_var.get() or "").strip(),
                "Exchange": 1,
                "Side": "2" if side == "BUY" else "1",   # 2=買,1=売
                "Qty": qty,
                "FrontOrderType": 20,  # 指値
                "Price": float(price),
                "ExpireDay": 0,
                "CashMargin": 1, "DelivType": 2, "FundType": "AA", "AccountType": 2,
            }
            url = self._base_url() + "/sendorder"
            self._log("HTTP", f"POST {url} {payload}")
            r = requests.post(url, headers={"X-API-KEY": self.token}, json=payload, timeout=10)
            self._log("HTTP", f"status={r.status_code} resp={(r.text or '')[:200]}...")
            r.raise_for_status()
            j = r.json()
            oid = j.get("OrderId") or j.get("Result")
            if oid:
                self._working[oid] = {"side": side, "price": float(price), "qty": qty}
                self._add_my_order(side, float(price), qty)   # ラダーにマーク
                self._start_order_polling()
                self._log("LIVE", f"発注受理: {side} {price} x{qty} OrderId={oid}")
        except Exception as e:
            self._log_exc("HTTP", e)


    def _start_order_polling(self):
        if self._polling_orders: return
        self._polling_orders = True
        self.after(800, self._poll_orders_once)

    def _poll_orders_once(self):
        """/orders をポーリング。約定数量を検知して画面と音を更新。"""
        try:
            if not self._working:
                self._polling_orders = False
                return
            url = self._base_url() + "/orders"
            r = requests.get(url, headers={"X-API-KEY": self.token}, timeout=8)
            if not r.ok:
                self.after(1000, self._poll_orders_once); return
            arr = r.json()
            rows = arr if isinstance(arr, list) else (arr.get("Orders") or [])
            # {order_id: (cum_qty, price)} を抽出（キー揺れ吸収）
            nowmap = {}
            for o in rows or []:
                oid = str(o.get("OrderId") or o.get("Id") or "")
                cum = self._to_float(o.get("CumQty") or o.get("ExecutionQty") or o.get("ExecutedQty") or 0) or 0.0
                px  = self._to_float(o.get("Price")  or o.get("ExecutionPrice") or o.get("AvgPrice") or 0) or 0.0
                if oid: nowmap[oid] = (cum, px)

            done_ids = []
            for oid, meta in list(self._working.items()):
                prev_cum = meta.get("cum", 0.0)
                cur_cum, px = nowmap.get(oid, (prev_cum, meta.get("price", 0.0)))
                if cur_cum > prev_cum:
                    # 約定（増分）
                    filled = int(round(cur_cum - prev_cum))
                    side   = meta["side"]; price = px or meta.get("price", 0.0)
                    # 自分の板マークを減らす
                    key = (side, float(meta.get("price", 0.0)))
                    if key in self._my_orders:
                        self._my_orders[key] = max(0, self._my_orders[key] - filled)
                        if self._my_orders[key] == 0: self._my_orders.pop(key, None)
                    # UI：音＆表示
                    self._on_filled(side, filled, price)
                    # 更新
                    meta["cum"] = cur_cum
                    self._working[oid] = meta
                # 完全約定や失効の検知（State など）— 任意で拡張
                st = str((o for o in rows if str(o.get("OrderId") or "")==oid).__next__().get("State","")) if oid in nowmap else ""
                if cur_cum >= meta["qty"] or st.upper() in ("CANCELED","REJECTED","EXPIRED","DONE","COMPLETED"):
                    done_ids.append(oid)

            for oid in done_ids:
                self._working.pop(oid, None)

            # ラダー再描画
            try:
                sells, buys, over, under = self._extract_levels(getattr(self, "_last_board_json", {}), depth=min(getattr(self, "book_depth", tk.IntVar(value=10)).get(), 10))
                rows = self._build_ladder_rows(sells, buys, over, under)
                self.ui_call(self._update_ladder_widget, rows)
            except Exception:
                pass

            # 次回
            self.after(800, self._poll_orders_once if self._working else lambda: setattr(self, "_polling_orders", False))
        except Exception as e:
            self._polling_orders = False
            self._log_exc("HTTP", e)

    def _beep(self, kind: str = "fill"):
        """約定音。sound_on が True のときだけ鳴らす。"""
        try:
            if hasattr(self, "sound_on") and not self.sound_on.get():
                return
            try:
                import winsound
                freq = 1047 if kind == "fill" else 784  # Do6 / G5
                winsound.Beep(freq, 120)
            except Exception:
                # 非Windowsなど → 端末ベル（失敗は無視）
                print("\a", end="")
        except Exception:
            pass

    def _note_trade(self, qty: int, price: float, side: str | None = None):
        """『約定：qty@price』をUIスレッドで反映（sideがあれば「買/売」を付ける）。"""
        try:
            if side:
                s = "買" if str(side).upper().startswith("B") else "売"
                txt = f"約定：{s} {int(qty):,}@{float(price):,.1f}"
            else:
                txt = f"約定：{int(qty):,}@{float(price):,.1f}"
            self.after(0, lambda: self.var_last_trade.set(txt))
        except Exception:
            pass


        
    def _best_prices_from_last_board(self):
        """直近の /board JSON から (bid1, ask1) を返す。無ければ (None, None)。"""
        try:
            j = getattr(self, "_last_board_json", None)
            if not j: return (None, None)
            sells, buys, *_ = self._extract_levels(j, depth=1)
            bid1 = buys[0][0]  if buys  else None
            ask1 = sells[0][0] if sells else None
            return (bid1, ask1)
        except Exception:
            return (None, None)


    def _apply_fill_to_scalper(self, side: str, qty: int, price: float):
        """SIM用：ローカルのネット枚数と平均建値を更新（BUY=+、SELL=-）。"""
        try:
            sgn = +1 if str(side).upper().startswith("B") else -1
            prev = int(getattr(self, "_scalp_net_qty", 0) or 0)
            new  = prev + sgn * int(qty)

            if prev == 0:
                self._scalp_avg_entry = float(price)
            elif (prev > 0 and new > 0) or (prev < 0 and new < 0):
                w_prev = abs(prev)
                self._scalp_avg_entry = (self._scalp_avg_entry * w_prev + float(price) * abs(qty)) / (w_prev + abs(qty))
            elif (prev > 0 and new < 0) or (prev < 0 and new > 0):
                # 反転したら新規側で再スタート
                self._scalp_avg_entry = float(price)

            if new == 0:
                self._scalp_avg_entry = None

            self._scalp_net_qty = new
        except Exception:
            pass



    def _on_filled(self, side: str, qty: int, price: float):
        """約定時の統一処理：表示更新→自分板減算→建玉更新→音→（SIMならローカル集計）"""
        try:
            # ★ 先に（SIM時）ローカル集計も更新
            if not self._is_live_permitted():
                self._apply_fill_to_scalper(side, qty, price)

            # 『約定：〜』表示
            self._note_trade(qty, price, side)  # side 省略可
            # ラダーの自分枚数を減算（あれば）
            try: self._remove_my_order("BUY" if str(side).upper().startswith("B") else "SELL", float(price), int(qty))
            except: pass
            # 建玉UI
            try: self._refresh_positions_ui()
            except: pass
            # 音
            self._beep("fill")
            self._log("DEBUG", f"filled: {side} {qty} @ {price:,.1f}")

            # フラットなら『約定：—』へ戻す（保険）
            try:
                if hasattr(self, "_clear_trade_if_flat"):
                    self._clear_trade_if_flat()
            except Exception:
                pass
            self._cleanup_working_if_empty()
        except Exception as e:
            self._log_exc("UI", e)


    

    def _refresh_positions_ui(self):
        """建玉タブのTreeview + 手動スキャ表示を同時に更新。"""
        try:
            arr = self._fetch_positions()

            # --- 建玉タブ Treeview 更新 ---
            tv = getattr(self, "tree_pos", None)
            if tv:
                for iid in tv.get_children():
                    tv.delete(iid)
                for it in arr:
                    sym  = str(it.get("Symbol") or "")
                    name = it.get("SymbolName") or ""
                    side = "買" if str(it.get("Side")) in ("2","BUY","買") else "売"
                    qty  = int(it.get("HoldQty") or 0)
                    price= float(it.get("Price") or 0)
                    pnl  = int(float(it.get("ValuationProfitLoss") or 0))
                    tv.insert("", "end", values=(sym, name, side, f"{qty:,}", f"{price:,.1f}", f"{pnl:,}"))

            # --- 手動スキャの表示（信用のみ、現物除外） ---
            q, avg = self._current_symbol_margin_position()
            self._update_manual_scalper_panel(q, avg)

            # Inv（ネット枚数ラベルなどがあれば）も更新
            if hasattr(self, "var_inv"):
                self.var_inv.set(f"Inv: {q}")

            self._clear_trade_if_flat()

        except Exception as e:
            self._log_exc("HTTP", e)




    def _manual_flatten_market(self):
        """現在のネット建玉を成行でクローズ（買超なら売成、売超なら買成）。
        現物は除外し、信用（またはSIMローカル）だけを対象にします。
        SIM時は即時に『約定：qty@price』を更新して音も鳴らします。
        """
        try:
            # --- トークン/銘柄チェック ---
            if not getattr(self, "token", ""):
                return self._log("warning", "Token未取得です")
            code = (self.symbol_var.get() or "").strip()
            if not code:
                return self._log("warning", "銘柄コードが未設定です")

            # --- ネット数量（信用優先、無ければ従来の集計 or SIMローカル）---
            net = 0
            if hasattr(self, "_current_symbol_margin_position"):
                try:
                    net, _avg = self._current_symbol_margin_position()  # 信用のみ（現物除外）
                except Exception:
                    net = 0
            if net == 0:
                # 従来の関数にフォールバック（現物を含む可能性あり）
                try:
                    net = int(self._get_net_position_qty() or 0)
                except Exception:
                    net = 0
            # SIMローカルのネット（強制約定などで使う）
            local_net = int(getattr(self, "_scalp_net_qty", 0) or 0)

            if net == 0 and local_net == 0:
                return self._log("AUTO", "決済対象の（信用）建玉はありません")

            # 反対売買側／数量の決定（信用>SIMローカルの優先度）
            if net != 0:
                side = "SELL" if net > 0 else "BUY"
                qty  = abs(int(net))
            else:
                side = "SELL" if local_net > 0 else "BUY"
                qty  = abs(int(local_net))

            # --- SIM：即時反映 ---
            if not self._is_live_permitted():
                self._log("DEBUG", f"flatten check: net={net}, local_net={local_net}")
                # 信用ネット（サーバ）とローカルネットの両方を確認
                net = 0
                if hasattr(self, "_current_symbol_margin_position"):
                    try:
                        net, _ = self._current_symbol_margin_position()
                    except Exception:
                        net = 0
                local_net = int(getattr(self, "_scalp_net_qty", 0) or 0)

                if net == 0 and local_net == 0:
                    return self._log("AUTO", "決済対象の（信用）建玉はありません")

                if net != 0:
                    side = "SELL" if net > 0 else "BUY"
                    qty  = abs(int(net))
                else:
                    side = "SELL" if local_net > 0 else "BUY"
                    qty  = abs(int(local_net))

                # 価格はL1優先
                price = None
                if hasattr(self, "_best_prices_from_last_board"):
                    bid1, ask1 = self._best_prices_from_last_board()
                    price = (ask1 if side == "BUY" else bid1)
                if price is None:
                    price = float(getattr(self, "last_price", 0.0) or 0.0)

                self._log("SIM", f"成行決済（ダミー） {side} x{qty} @ {price:,.1f}")
                try:
                    self._on_filled(side, qty, float(price))  # ← ここで表示・音・ローカル集計が一括
                except Exception as e:
                    self._log_exc("SIM", e)

                # 板上の自分注文はクリアしておく（ブロック誤発動防止）
                try: self._cancel_all_working()
                except Exception: pass
                return

            # --- LIVE：成行発注 → ポーリングで約定検知 ---
            import requests
            ex = int(getattr(self, "EXCHANGE", 1) or 1)
            payload = {
                "Symbol": code,
                "Exchange": ex,
                "Side": "2" if side == "BUY" else "1",
                "Qty": int(qty),
                "FrontOrderType": 120,  # ← 環境に合わせた“成行”コードを使用（従来値を維持）
                "Price": 0,
                "ExpireDay": 0,
                "CashMargin": 1, "DelivType": 2, "FundType": "AA", "AccountType": 2,
            }
            url = self._base_url() + "/sendorder"
            self._log("HTTP", f"POST {url} {payload}")
            r = requests.post(url, headers={"X-API-KEY": self.token}, json=payload, timeout=10)
            self._log("HTTP", f"status={r.status_code} resp={(r.text or '')[:200]}...")
            r.raise_for_status()
            self._log("LIVE", f"成行決済送信: {side} x{qty}")

            # 受付後：板に残っている自分指値は不要なので全取消（ブロック原因の残骸を掃除）
            try:
                self._cancel_all_working()
            except Exception:
                pass

            # 約定検知はポーリングで行い、確定したら _on_filled(...) が呼ばれる想定
            self._start_order_polling()

        except Exception as e:
            self._log_exc("HTTP", e)


    def _clear_trade_if_flat(self):
        """
        現在銘柄がフラット（信用ネット=0）かつ作業注文なし、かつSIMローカルも0なら
        『約定：—』へ戻す。
        """
        try:
            net, _ = self._current_symbol_margin_position() if hasattr(self, "_current_symbol_margin_position") else (0, None)
        except Exception:
            net = 0
        local = int(getattr(self, "_scalp_net_qty", 0) or 0)
        has_work = bool(getattr(self, "_working", {})) or bool(getattr(self, "_my_orders", {}))
        if int(net or 0) == 0 and local == 0 and not has_work:
            try:
                self.ui_call(self.var_last_trade.set, "約定：—")
            except Exception:
                pass


    def _cancel_all_working(self):
        """受付済みの指値をすべて取消。SIMは自動で消去。"""
        try:
            # LIVE: /cancel を順繰りに叩く
            if self._is_live_permitted() and getattr(self, "_working_by_price", None):
                url = self._base_url() + "/cancelorder"
                for (side, price), oid in list(self._working_by_price.items()):
                    try:
                        r = requests.put(url, headers={"X-API-KEY": self.token}, json={"OrderId": oid}, timeout=10)
                        self._log("HTTP", f"PUT {url} oid={oid} status={r.status_code} resp={(r.text or '')[:160]}...")
                    except Exception as e:
                        self._log_exc("HTTP", e)
            # SIM: 全部消す
            if hasattr(self, "_my_orders"):
                self._my_orders.clear()
            if hasattr(self, "_working_by_price"):
                self._working_by_price.clear()
            self._redraw_ladder_from_last_board()
            self._log("AUTO", "全指値を取消しました")
            self._cleanup_working_if_empty()
        except Exception as e:
            self._log_exc("HTTP", e)



    def _render_ladder(self, base_rows):
        """[自ASK|ASK|価格|BID|自BID] を Canvas に色分け描画（自分=黒, ASK=橙, BID=青）。"""
        cv = getattr(self, "ladder_cv", None)
        if cv is None:
            return self._log("UI", "ladder_cv が未生成です")
        H  = getattr(self, "_LADDER_ROW_H", 22)
        W1, W2, W3, W4, W5 = self._ladder_column_widths()

        ASK, BID, FG, MY, GRID = "#d26100", "#1462ac", "#222", "#000", "#ddd"
        my = getattr(self, "_my_orders", {}) or {}

        self._ladder_rows = list(base_rows or [])
        self._ladder_price_at_row = {}
        cv.delete("all")

        def fnum(s):
            try:    return float(str(s).replace(",", "").strip())
            except: return None

        y = 1
        for i, (ask_txt, price_txt, bid_txt, _tag) in enumerate(self._ladder_rows):
            p = fnum(price_txt)
            mya = my.get(("SELL", p), 0) if p is not None else 0
            myb = my.get(("BUY",  p), 0) if p is not None else 0
            ask_my = f"{mya:,}" if mya else ""
            bid_my = f"{myb:,}" if myb else ""

            x = 0
            cv.create_text(x+W1-4, y+H/2, text=ask_my,           anchor="e", fill=MY);  x += W1
            cv.create_text(x+W2-4, y+H/2, text=(ask_txt or ""),   anchor="e", fill=ASK); x += W2
            cv.create_text(x+W3/2, y+H/2, text=(price_txt or ""), anchor="c", fill=FG);  x += W3
            cv.create_text(x+4,    y+H/2, text=(bid_txt or ""),   anchor="w", fill=BID); x += W4
            cv.create_text(x+4,    y+H/2, text=bid_my,            anchor="w", fill=MY)

            self._ladder_price_at_row[i] = p
            y += H

        total_w = sum((W1, W2, W3, W4, W5))
        cv.create_line(0, 0, total_w, 0, fill=GRID)
        cv.create_line(0, y-1, total_w, y-1, fill=GRID)
        xx = 0
        for w in (W1, W2, W3, W4, W5):
            cv.create_line(xx, 0, xx, y, fill=GRID); xx += w
        # 幅固定はしない。スクロール領域だけ合わせる
        cv.config(scrollregion=(0, 0, max(total_w, cv.winfo_width()), y))


    def _on_canvas_dbl_order(self, ev):
        try:
            H = getattr(self, "_LADDER_ROW_H", 22)
            W1, W2, W3, W4, W5 = self._ladder_column_widths()

            row = max(0, int(ev.y // H))
            price = (getattr(self, "_ladder_price_at_row", {}) or {}).get(row)
            if price is None:
                return self._log("DEBUG", f"canvas dbl: price=None row={row}")

            b1, b2, b3, b4 = W1, W1+W2, W1+W2+W3, W1+W2+W3+W4
            x = ev.x
            if x < b1:         col = 0
            elif x < b2:       col = 1
            elif x < b3:       col = 2
            elif x < b4:       col = 3
            else:              col = 4

            side = "SELL" if col in (0,1) else ("BUY" if col in (3,4) else (self.order_side.get() if hasattr(self,"order_side") else "BUY"))

            try: qty = int(self.scalp_qty.get())
            except: qty = 0
            if qty <= 0:
                return self._log("warning", "数量が0です（＋/−で増やしてください）")

            # 単一建玉の厳守
            if not self._enforce_single_position():
                return

            self._log("DEBUG", f"canvas dbl: row={row} col={col} side={side} price={price} qty={qty}")
            import threading
            threading.Thread(target=self._place_limit_at_price, args=(side, float(price), qty), daemon=True).start()
        except Exception as e:
            self._log_exc("UI", e)



    def _on_ladder_dbl_order(self, ev):
        """ラダーのダブルクリック→その価格に即・指値発注"""
        try:
            iid = self.ladder.identify_row(ev.y)
            col = self.ladder.identify_column(ev.x)  # "#1"=ask_my, "#2"=ask, "#3"=price, "#4"=bid, "#5"=bid_my
            if not iid:
                return self._log("DEBUG", "dbl: 行なし（ヘッダ/余白）")

            vals = self.ladder.item(iid, "values")
            ask_my, ask, price, bid, bid_my = (tuple(vals) + ("","","","",""))[:5]
            self._log("DEBUG", f"dbl: row={iid} col={col} vals={vals}")

            px_s = str(price or "").replace(",", "").strip()
            try:
                px = float(px_s)
            except Exception:
                return self._log("DEBUG", f"dbl: 数値価格でないため無視 price='{price}'")

            # 列→サイド判定
            if col in ("#1", "#2"):    # ASK側
                side = "SELL"
            elif col in ("#4", "#5"):  # BID側
                side = "BUY"
            else:                      # 価格列は現在の選択サイド（なければBUY）
                side = (self.order_side.get() if hasattr(self, "order_side") else "BUY")

            # 数量
            try:
                qty = int(self.scalp_qty.get())
            except Exception:
                qty = 0
            if qty <= 0:
                return self._log("warning", "数量が0です（＋/−やホイールで増やしてください）")

            self._log("DEBUG", f"dbl: 発注 side={side} price={px} qty={qty}")
            threading.Thread(target=self._place_limit_at_price, args=(side, px, qty), daemon=True).start()
        except Exception as e:
            self._log_exc("UI", e)


    def _place_limit_at_price(self, side: str, price: float, qty: int):
        """side(BUY/SELL) を price に qty 指値。単一建玉ルールを厳守。"""
        try:
            code = (self.symbol_var.get() or "").strip()
            if not code: return self._log("warning", "銘柄コード未設定")
            if qty <= 0:  return self._log("warning", "数量が0です")

            # 単一建玉の厳守（ここでも再チェック）
            if not self._enforce_single_position():
                return

            if not self._is_live_permitted():
                # SIM: 画面反映のみ
                self._log("SIM", f"{side} 指値 {price} x{qty}")
                self._add_my_order(side, float(price), int(qty))
                return

            # LIVE: /sendorder
            payload = {
                "Symbol": code, "Exchange": 1,
                "Side": "2" if side=="BUY" else "1",
                "Qty": int(qty),
                "FrontOrderType": 20,  # 指値
                "Price": float(price),
                "ExpireDay": 0,
                "CashMargin": 1, "DelivType": 2, "FundType": "AA", "AccountType": 2,
            }
            url = self._base_url() + "/sendorder"
            self._log("HTTP", f"POST {url} {payload}")
            r = requests.post(url, headers={"X-API-KEY": self.token}, json=payload, timeout=10)
            self._log("HTTP", f"status={r.status_code} resp={(r.text or '')[:200]}...")
            r.raise_for_status()
            j = r.json()
            oid = j.get("OrderId") or j.get("Result")
            if not hasattr(self, "_working"): self._working = {}
            self._working[oid] = {"side": side, "price": float(price), "qty": int(qty), "cum": 0}
            self._add_my_order(side, float(price), int(qty), order_id=oid)
            self._start_order_polling()  # 既存のポーリングで _on_filled を呼ぶ想定
            self._log("LIVE", f"発注受理: {side} {price} x{qty} OrderId={oid}")
        except Exception as e:
            self._log_exc("HTTP", e)


    def _setup_ladder_bindings(self):
        """ラダーのイベントバインドを現在の実装に合わせて設定"""
        try:
            if hasattr(self, "ladder_cv"):
                self.ladder_cv.bind("<Double-1>",        self._on_canvas_dbl_order)
                self.ladder_cv.bind("<Double-Button-1>", self._on_canvas_dbl_order)
                self._log("DEBUG", "ladder: Canvas bindings enabled")
            elif hasattr(self, "ladder"):
                # もしTreeview版に戻す場合の保険
                self.ladder.bind("<Double-1>",        self._on_ladder_dbl_order, add="+")
                self.ladder.bind("<Double-Button-1>", self._on_ladder_dbl_order, add="+")
                self._log("DEBUG", "ladder: Treeview bindings enabled")
            else:
                self._log("warning", "ladder widget not present (no bindings)")
        except Exception as e:
            self._log_exc("UI", e)

    # ===== 単一発注制限 ======
    def _has_working_order(self) -> bool:
        """未約定の作業注文（SIMローカル/実注文の双方）を検出。"""
        return (
            bool(getattr(self, "_working", {})) or
            bool(getattr(self, "_working_by_price", {})) or
            bool(getattr(self, "_my_orders", {}))
        )

    def _has_open_position(self) -> bool:
        """
        単一建玉判定用：現物（キャッシュ）は除外し、
        “現在のメイン銘柄”の信用ポジションのネット枚数だけを見る。
        """
        try:
            net_qty, _ = self._current_symbol_margin_position()
            return int(net_qty or 0) != 0
        except Exception:
            return False


    def _enforce_single_position(self) -> bool:
        """単一建玉モード：既存の建玉 or 受付済み注文があれば新規を拒否。"""
        reason = self._why_block_single()
        if reason:
            self._log("warning", f"単一建玉モード：新規発注をブロックしました（理由: {reason}）")
            return False
        return True

    def _cleanup_working_if_empty(self):
        """自分の作業注文テーブルが空ならクリーンアップ（ブロック誤発動防止）。"""
        try:
            mo = getattr(self, "_my_orders", {})
            wb = getattr(self, "_working_by_price", {})
            wk = getattr(self, "_working", {})
            if isinstance(mo, dict) and not mo:
                setattr(self, "_my_orders", {})
            if isinstance(wb, dict) and not wb:
                setattr(self, "_working_by_price", {})
            if isinstance(wk, dict) and not wk:
                setattr(self, "_working", {})
        except Exception:
            pass


    # --------------------------------------------------
    # ログ出力（[TAG]で統一表示）
    # --------------------------------------------------
    def _append_log(self, line: str):
        try:
            box = getattr(self, "log_box", None)
            if box:
                box.insert("end", line + "\n")
                box.see("end")
        except Exception:
            pass

    def _nowstr_full(self) -> str:
        import time
        return time.strftime("%H:%M:%S")

    def _log(self, tag: str, msg: str, *, dedup_key: str | None = None):
        """
        [HH:MM:SS] [TAG] message をログテキストに追記。
        dedup_key が同じものは短時間（0.5秒）重複を抑止。
        """
        import time, threading
        ts = self._nowstr_full()
        line = f"[{ts}] [{tag}] {msg}"
        key = dedup_key or f"{tag}:{msg}"

        now = time.time()
        if not hasattr(self, "_log_memo"):
            self._log_memo = {}
        last = self._log_memo.get(key, 0.0)
        if now - last < 0.5:
            return
        self._log_memo[key] = now

        def _do():
            try:
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
            except Exception:
                try:
                    print(line)
                except Exception:
                    pass

        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.ui_call(_do)


    def _log_exc(self, tag, e):
        import time, traceback
        self._append_log(f"[{time.strftime('%H:%M:%S')}] [{tag}] {e}")
        tb = traceback.format_exc().strip().splitlines()[-1]
        self._append_log(f"[{time.strftime('%H:%M:%S')}] [{tag}] {tb}")


    # 歩み値：記録・1分集計（以前のものがなければ追加）
    def _append_tape(self, side:str, qty:int, price:float):
        import time
        self._last_print_side = ('B' if side == "買成" else 'S')  # ← 追加
        ts = time.strftime("%H:%M:%S")
        if not hasattr(self, "tape"): self.tape = []
        self.tape.append((time.time(), side, int(qty), float(price)))
        if len(self.tape) > 200: self.tape = self.tape[-200:]
        # 表示
        self.ui_call(self.tree_tape.insert, "", "end", values=(ts, side, int(qty), f"{price:,.1f}"))
        self.ui_call(self.tree_tape.see, "end")
        self._recompute_tape_stats()

    def _recompute_tape_stats(self):
        import time
        if not hasattr(self, "tape"): 
            self.var_mkt.set("成行: 買0 / 売0 (1m)"); return
        cutoff = time.time() - 60
        buy  = sum(q for (t,s,q,_) in self.tape if t>=cutoff and s=="買成")
        sell = sum(q for (t,s,q,_) in self.tape if t>=cutoff and s=="売成")
        self.ui_call(self.var_mkt.set, f"成行: 買{buy} / 売{sell} (1m)")

    def _update_tape_from_l1(self, prev, bidp, bidq, askp, askq):
        """Best価格が不変で枚数が減った分を成行約定として推定"""
        try:
            if prev:
                if bidp is not None and prev.get("bidp")==bidp and bidq is not None and prev.get("bidq") is not None:
                    if bidq < prev["bidq"]:
                        self._append_tape("売成", int(prev["bidq"] - bidq), float(bidp))
                if askp is not None and prev.get("askp")==askp and askq is not None and prev.get("askq") is not None:
                    if askq < prev["askq"]:
                        self._append_tape("買成", int(prev["askq"] - askq), float(askp))
            self._prev_l1 = {"bidp":bidp, "bidq":bidq, "askp":askp, "askq":askq}
        except Exception as e:
            self._log_exc("DRV", e)

    def _clear_tape(self):
        try:
            for iid in self.tree_tape.get_children():
                self.tree_tape.delete(iid)
            self.tape = []
            self.var_mkt.set("成行: 買0 / 売0 (1m)")
        except Exception:
            pass

    def _get_net_position_qty(self) -> int:
        """建玉ツリーから Inv を合算（買+ / 売-）"""
        try:
            s = 0
            for iid in getattr(self, "tree_pos", []).get_children():
                vals = self.tree_pos.item(iid, "values")
                side = str(vals[2]); qty = int(float(vals[3]))
                s += qty if side in ("買","BUY","Long") else -qty
            return s
        except Exception:
            return 0

        def _get_current_price_for_symbol(self, sym: str):
            """銘柄の現在値を返す（キャッシュ→アクティブ銘柄→/board）。"""
            base = sym.split("@")[0] if sym else ""
            # 1) 銘柄別キャッシュ
            if hasattr(self, "_last_price_by_code") and base in self._last_price_by_code:
                return self._last_price_by_code[base]
            # 2) 画面のアクティブ銘柄と一致
            if base and base == getattr(self, "_active_code", None) and getattr(self, "last_price", None) is not None:
                return self.last_price
            # 3) /board で取得
            try:
                code_ex = sym if ("@" in sym) else f"{base}@{getattr(self,'EXCHANGE',1)}"
                j = self._http_get("BOARD", f"/board/{code_ex}")
                last = self._pick(j, "CurrentPrice", "LastPrice", "NowPrice")
                if last is not None:
                    p = self._to_float(last)
                    if p is not None:
                        if not hasattr(self, "_last_price_by_code"): self._last_price_by_code = {}
                        self._last_price_by_code[base] = p
                        return p
            except Exception as e:
                self._log_exc("HTTP", e)
            return None



    # --------------------------------------------------
    # V3準拠：UIスレッド実行ヘルパ
    # --------------------------------------------------
    def ui_after(self, ms: int, fn, *args, **kwargs):
        """Tkのafterラッパ。UIスレッドで遅延実行（引数対応）。"""
        try:
            self.after(ms, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    def ui_call(self, fn, *args, **kwargs):
        """他スレッドからUIスレッドへ安全に処理を投げる（引数対応）。"""
        try:
            import threading
            if threading.current_thread() is threading.main_thread():
                return fn(*args, **kwargs)
            else:
                self.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    # -----------------
    # デモ
    # -----------------
    def _toggle_demo(self):
        if self.demo_on.get():
            self._log("DEBUG", "デモ板: 開始"); self._demo_loop()
        else:
            self._log("DEBUG", "デモ板: 停止")

    def _demo_loop(self):
        if not self.demo_on.get(): return
        self._demo_step_once()
        self.after(800, self._demo_loop)

    def _demo_step_once(self):
        """Bestの枚数減少 & たまに1tick移動して、UIを通常経路で更新"""
        try:
            j = getattr(self, "_last_board_json", None)
            if not j:
                # まだ板を持っていなければ一度だけ実データを取る（失敗しても続行）
                try:
                    base = self._get_base_code() if hasattr(self, "_get_base_code") else (self.symbol_var.get() or "").strip()
                    code_ex = base if "@" in (self.symbol_var.get() or "") else f"{base}@{getattr(self,'EXCHANGE',1)}"
                    j = self._http_get("BOARD", f"/board/{code_ex}")
                    self._last_board_json = j
                except Exception:
                    return

            depth = min(getattr(self, "book_depth", tk.IntVar(value=10)).get(), 10)
            sells, buys, over, under = self._extract_levels(j, depth=depth)
            if not sells or not buys: return

            # 1) Best枚数をランダムに減少（疑似歩み値に反映）
            side = random.choice(("ask","bid"))
            if side=="ask" and sells:
                p,q = sells[0]; dq = max(1, int((q or 50)*random.uniform(0.1,0.5)))
                sells[0] = (p, max(0, (q or 0) - dq))
                self._append_tape("買成", dq, p)
            elif side=="bid" and buys:
                p,q = buys[0]; dq = max(1, int((q or 50)*random.uniform(0.1,0.5)))
                buys[0] = (p, max(0, (q or 0) - dq))
                self._append_tape("売成", dq, p)

            # 2) ときどき1tick移動（簡易：tick=1.0想定）
            if random.random() < 0.25:
                tick = 1.0
                up = random.choice((True, False))
                sells = [((p+tick) if up else (p-tick), q) for (p,q) in sells]
                buys  = [((p+tick) if up else (p-tick), q) for (p,q) in buys]

            # 3) JSONに反映（SellN/BuyN形式のみ書き戻し）
            for i,(p,q) in enumerate(reversed(sells), start=1):
                j[f"Sell{i}"] = {"Price": float(p), "Qty": float(q)}
            for i,(p,q) in enumerate(buys, start=1):
                j[f"Buy{i}"]  = {"Price": float(p), "Qty": float(q)}
            self._last_board_json = j

            # 4) いつもの描画経路を呼ぶ
            rows = self._build_ladder_rows(sells, buys, over, under)
            self.ui_call(self._update_ladder_widget, rows)

            bid1p,bid1q = buys[0]
            ask1p,ask1q = sells[0]
            self._update_tape_from_l1(getattr(self, "_prev_l1", None), bid1p, bid1q, ask1p, ask1q)

            # Sp/Imbも更新
            if bid1p is not None and ask1p is not None:
                self.var_spread.set(f"Sp: {ask1p - bid1p:,.1f}")
                tot = (bid1q or 0)+(ask1q or 0)
                self.var_imbal.set(f"Imb: {((bid1q or 0)-(ask1q or 0))/tot*100:+.1f}%") if tot>0 else self.var_imbal.set("Imb: —")
            st = self.spoof.update(
                ts_ms=int(time.time()*1000),
                best_bid=bid1p, best_ask=ask1p, best_bidq=bid1q, best_askq=ask1q,
                levels={'B': buys, 'S': sells},
                last_trade={'side': getattr(self, '_last_print_side', None)}
            )
            self.lbl_spoof.configure(text=f"見せ板: {self.spoof.format_badge(st)}")   
        except Exception as e:
            self._log_exc("DRV", e)

    def _demo_force_fill(self):
        """自分の注文のうち『最良価格』を1件だけ約定させる（デモ用）"""
        try:
            if not getattr(self, "_my_orders", {}):
                return self._log("AUTO", "自分の指値がありません")
            sells = sorted([(p,q) for (side,p),q in self._my_orders.items() if side=="SELL"], key=lambda x:-x[0])
            buys  = sorted([(p,q) for (side,p),q in self._my_orders.items() if side=="BUY"],  key=lambda x:-x[0])
            if sells and buys:
                p, q = (sells[0] if sells[0][0] >= buys[0][0] else buys[0])
                side = "SELL" if sells and sells[0][0] >= (buys[0][0] if buys else -1) else "BUY"
            else:
                (p, q), side = (sells[0], "SELL") if sells else (buys[0], "BUY")
            fill = min(int(q), max(100, int(self.scalp_qty.get() or 100)))
            key = (side, float(p))
            self._my_orders[key] = max(0, self._my_orders[key]-fill)
            if self._my_orders[key]==0: self._my_orders.pop(key, None)

            # ラダー再描画（既存JSONから）
            try:
                j = getattr(self, "_last_board_json", {})
                sells_lv, buys_lv, over, under = self._extract_levels(j, depth=min(getattr(self,"book_depth", tk.IntVar(value=10)).get(), 10))
                rows = self._build_ladder_rows(sells_lv, buys_lv, over, under)
                self.ui_call(self._update_ladder_widget, rows)
            except Exception:
                pass

            # ★ 約定表示＆音（ここが肝心）
            self._on_filled(side, fill, float(p))
        except Exception as e:
            self._log_exc("UI", e)



    # --------------------------------------------------
    # HTTP/WS URL と トークン取得（18080/18081）
    # --------------------------------------------------
    def _http_get(self, tag: str, path: str, timeout=8):
        """GETしてJSONを返す。URL/Status/先頭キーをログ"""
        base = self._base_url()
        url  = base + path if path.startswith("/") else base + "/" + path
        headers = {"X-API-KEY": self.token} if getattr(self, "token", "") else {}
        self._log("HTTP", f"[{tag}] GET {url}")
        r = requests.get(url, headers=headers, timeout=timeout)
        try:
            j = r.json() if r.text else {}
            keys = ", ".join(list(j.keys())[:5])
        except Exception:
            j, keys = {}, "(not json)"
        self._log("HTTP", f"[{tag}] status={r.status_code} keys=[{keys}] body={(r.text or '')[:200]}...")
        r.raise_for_status()
        return j

    # --- ヘルパー：dictから安全に値を拾う ---
    def _pick(self, obj, *keys, default=None):
        def get_path(d, path):
            cur = d
            for p in path.split("."):
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return None
            return cur
        for k in keys:
            if obj is None: continue
            v = get_path(obj, k) if (isinstance(k, str) and "." in k) else (obj.get(k) if isinstance(obj, dict) else None)
            if v is not None: return v
        return default

    def _pick_num(self, obj, *keys, default=0.0):
        v = self._pick(obj, *keys, default=None)
        if v is None: return default
        try:
            # "1,234.0" / "1234" / 1234 / 1234.0 に対応
            if isinstance(v, str): v = v.replace(",", "").strip()
            return float(v)
        except Exception:
            return default

    # --- ネットポジ（Inv）取得：self.tree_pos から合算、なければ0 ---
    def _get_net_position_qty(self) -> int:
        try:
            s = 0
            for iid in getattr(self, "tree_pos", []).get_children():
                vals = self.tree_pos.item(iid, "values")
                # values=(sym,name,side,qty,entry,pnl)
                side = str(vals[2]); qty = int(float(vals[3]))
                s += qty if side in ("買","BUY","Long") else -qty
            return s
        except Exception:
            return getattr(self, "net_position_qty", 0) if hasattr(self, "net_position_qty") else 0

    # --- 板JSONから Best×5 を抽出（Bids/Asks or Buy/Sell系を吸収） ---
    def _extract_book_levels(self, j: dict, depth: int = 5):
        bids, asks = [], []
        # 形式1: "Bids":[{"Price":...,"Qty":...},...], "Asks":[...]
        jb = j.get("Bids") or j.get("Buy") or []
        ja = j.get("Asks") or j.get("Sell") or []
        if isinstance(jb, list) and isinstance(ja, list) and jb and ja:
            # Asksは価格昇順のことが多いのでBestを先頭と仮定
            bids = [(self._to_float(x.get("Price")), self._to_float(x.get("Qty"))) for x in jb[:depth]]
            asks = [(self._to_float(x.get("Price")), self._to_float(x.get("Qty"))) for x in ja[:depth]]
        else:
            # 形式2: Buy1/Buy2..., Sell1/Sell2...
            for i in range(1, depth+1):
                b = j.get(f"Buy{i}") or j.get(f"Bid{i}")
                a = j.get(f"Sell{i}") or j.get(f"Ask{i}")
                if isinstance(b, dict):
                    bids.append((self._to_float(b.get("Price")), self._to_float(b.get("Qty"))))
                if isinstance(a, dict):
                    asks.append((self._to_float(a.get("Price")), self._to_float(a.get("Qty"))))
        # None除去
        bids = [(p or 0.0, q or 0.0) for (p,q) in bids]
        asks = [(p or 0.0, q or 0.0) for (p,q) in asks]
        return bids, asks

    # --- 板ウィジェットへ反映 & メトリクス計算 ---
    def _update_board_widgets(self, bids, asks):
        try:
            # クリア
            for iid in self.tree_book.get_children():
                self.tree_book.delete(iid)
            # 表示：Ask(上からBest→第5)、区切り、Bid(Best→第5)
            for p,q in asks[:5]:
                self.tree_book.insert("", "end", values=("ASK", f"{p:,.1f}", f"{int(q):,}"))
            self.tree_book.insert("", "end", values=("", "—", "—"))
            for p,q in bids[:5]:
                self.tree_book.insert("", "end", values=("BID", f"{p:,.1f}", f"{int(q):,}"))
            # メトリクス
            bid1p, bid1q = (bids[0] if bids else (None, None))
            ask1p, ask1q = (asks[0] if asks else (None, None))
            if bid1p is not None and ask1p is not None:
                spread = ask1p - bid1p
                self.var_spread.set(f"Sp: {spread:,.1f}")
                if (bid1q or 0) + (ask1q or 0) > 0:
                    imb = ((bid1q or 0) - (ask1q or 0)) / ((bid1q or 0) + (ask1q or 0)) * 100.0
                    self.var_imbal.set(f"Imb: {imb:+.1f}%")
                else:
                    self.var_imbal.set("Imb: —")
            self.var_inv.set(f"Inv: {self._get_net_position_qty()}")
        except Exception as e:
            self._log_exc("UI", e)

    # --- 疑似歩み値：Bestの枚数減少を成行き約定として推定＆記録 ---
    def _append_tape(self, side:str, qty:int, price:float):
        import time
        ts_disp = time.strftime("%H:%M:%S")
        now = time.time()
        if not hasattr(self, "tape"): self.tape = []  # [(ts, side, qty, price)]
        self.tape.append((now, side, int(qty), float(price)))
        # 200件保つ
        if len(self.tape) > 200: self.tape = self.tape[-200:]
        # 表示
        self.ui_call(self.tree_tape.insert, "", "end", values=(ts_disp, side, int(qty), f"{price:,.1f}"))
        self.ui_call(self.tree_tape.see, "end")
        # 1分集計更新
        self._recompute_tape_stats()

    def _recompute_tape_stats(self):
        import time
        if not hasattr(self, "tape"): 
            self.var_mkt.set("成行: 買0 / 売0 (1m)"); return
        cutoff = time.time() - 60.0
        buy = sum(q for (t,side,q,_) in [(t,s,q,p) for (t,s,q,p) in self.tape] if t>=cutoff and side=="買成")
        sell= sum(q for (t,side,q,_) in [(t,s,q,p) for (t,s,q,p) in self.tape] if t>=cutoff and side=="売成")
        self.ui_call(self.var_mkt.set, f"成行: 買{buy} / 売{sell} (1m)")

    def _update_tape_from_l1(self, prev, bidp, bidq, askp, askq):
        # 価格不変で枚数が減った分を“成行き約定”として計上
        try:
            if prev:
                if bidp is not None and prev.get("bidp")==bidp and bidq is not None and prev.get("bidq") is not None:
                    if bidq < prev["bidq"]:
                        self._append_tape("売成", int(prev["bidq"] - bidq), float(bidp))
                if askp is not None and prev.get("askp")==askp and askq is not None and prev.get("askq") is not None:
                    if askq < prev["askq"]:
                        self._append_tape("買成", int(prev["askq"] - askq), float(askp))
            self._prev_l1 = {"bidp":bidp, "bidq":bidq, "askp":askp, "askq":askq}
        except Exception as e:
            self._log_exc("DRV", e)

    def _clear_tape(self):
        try:
            for iid in self.tree_tape.get_children():
                self.tree_tape.delete(iid)
            self.tape = []
            self.var_mkt.set("成行: 買0 / 売0 (1m)")
        except Exception:
            pass

    # ====== 建玉 =====
    def _fetch_positions(self):
        """/positions を返す（失敗時は空配列）。"""
        try:
            import requests
            url = self._base_url() + "/positions"
            r = requests.get(url, headers={"X-API-KEY": self.token}, timeout=10)
            r.raise_for_status()
            js = r.json()
            return js if isinstance(js, list) else []
        except Exception:
            return []

    def _current_symbol_margin_position(self):
        """
        戻り値: (net_qty, avg_entry)
        - net_qty : BUYを+、SELLを- のネット枚数（信用のみ、現物は除外）
        - avg_entry: ネット枚数に対する加重平均建値（abs(net_qty)>0 のとき）
        """
        arr  = self._fetch_positions() if hasattr(self, "_fetch_positions") else []
        code = (self.symbol_var.get() or "").strip()
        net, notional = 0, 0.0

        for it in arr:
            if code and str(it.get("Symbol")) != code:
                continue
            if not self._is_margin_position(it):
                continue  # ★ 現物は除外
            qty   = int(it.get("HoldQty") or 0)
            if qty == 0:
                continue
            price = float(it.get("Price") or 0.0)
            side  = str(it.get("Side"))
            sgn   = +1 if side in ("2", "BUY", "買") else -1
            net      += sgn * qty
            notional += sgn * qty * price

        avg = (abs(notional) / abs(net)) if net else None
        return net, avg

    def _why_block_single(self) -> str:
        """
        単一建玉モードでブロックする理由を返す。
        ""               -> ブロックしない
        "working_order"  -> 未約定の作業注文あり
        "margin_pos:+N"  -> 信用ネット +N 枚（現物は除外）
        "local_pos:+N"   -> SIMローカル（_scalp_net_qty）が +N
        """
        if self._has_working_order():
            return "working_order"
        try:
            net, _ = self._current_symbol_margin_position()
            if int(net or 0) != 0:
                return f"margin_pos:{int(net)}"
        except Exception:
            pass
        local = int(getattr(self, "_scalp_net_qty", 0) or 0)
        if local != 0:
            return f"local_pos:{local}"
        return ""


    def _is_margin_position(self, it: dict) -> bool:
        """
        kabuステの /positions は現物と信用で項目差があることが多い。
        現物を“除外”するための保守的な判定。
        - MarginTradeType が存在（Noneでない） → 信用とみなす
        - Leverage が真 → 信用とみなす
        - それ以外 → 現物とみなす
        必要に応じて手元のAPI応答に合わせて条件を拡張してください。
        """
    def _is_margin_position(self, it: dict) -> bool:
        """
        現物/信用のゆるめの判定。環境差吸収のため複数キーを参照。
        True -> 信用として扱う / False -> 現物として除外
        """
        try:
            # kabu API でよく見かけるフィールド群
            cm  = it.get("CashMargin")          # 1=現物, 2/3=信用（環境により差あり）
            mtt = it.get("MarginTradeType")     # 1/2 が信用
            lev = it.get("Leverage")            # 真なら信用系として扱う
            mt  = str(it.get("MarginType", "")).lower()

            if cm in (2, 3):              # 信用新規/返済っぽい値
                return True
            if mtt in (1, 2):             # 信用
                return True
            if bool(lev):
                return True
            if mt in ("general", "system", "margin"):
                return True
            return False                   # 上記に該当しなければ現物扱い
        except Exception:
            return False

    def _update_manual_scalper_panel(self, qty: int, avg: float):
        """手動スキャUIの表示（枚数/建値）を更新。存在する変数にだけ反映。"""
        side_txt = "—"
        if qty > 0:  side_txt = "買"
        elif qty < 0: side_txt = "売"

        # LIVEカード（右上）に反映
        if hasattr(self, "var_live_qty"):
            self.var_live_qty.set(f"数量: {abs(qty):,}" if qty else "数量: 0")
        if hasattr(self, "var_live_pos"):
            self.var_live_pos.set(f"{side_txt} 建値: {avg:,.1f}" if qty else "—")

        # 手動スキャ専用の変数があればそちらにも
        if hasattr(self, "var_scalp_qty"):
            self.var_scalp_qty.set(f"{abs(qty):,}" if qty else "0")
        if hasattr(self, "var_scalp_entry"):
            self.var_scalp_entry.set(f"{avg:,.1f}" if qty else "—")




    def _precheck_station(self) -> bool:
        '''
        kabuステーション（外部API）が起動しているか簡易チェック。
        #- :18080（--production）/ :18081（--sandbox）にHTTP接続できるかを見る。
        #- 401/403等でも「接続できた」扱い（起動判定OK）。ConnectionErrorは未起動とみなす。
        '''
        if requests is None:
            self._log("WARNING", "requests が未インストールです（pip install requests）")
            return False

        url = self._base_url() + "/positions"  # トークン不要の疎通確認として使用（認証エラーでもOK）
        try:
            r = requests.get(url, timeout=3)
            self._log("DRV", f"kabuステーション応答 status={r.status_code}")
            return True
        except requests.exceptions.ConnectionError:
            self._log(
                "WARNING",
                "kabuステーション（外部API）が起動していません。"
                "アプリを起動し『外部API』を有効にしてください（ポート: "
                + ("18080" if self.is_production.get() else "18081") + "）。"
            )
            try:
                from tkinter import messagebox
                messagebox.showwarning(
                    "kabuステーション未起動",
                    "kabuステーション（外部API）が見つかりませんでした。\n"
                    "アプリを起動し『外部API』を有効にしてから再試行してください。"
                )
            except Exception:
                pass
            return False
        except Exception as e:
            self._log_exc("HTTP", e)
            return False

    def _get_token(self):
        """POST /token に APIパスワードを送り、Token を取得→ログ出力。未起動時は [warning] で通知。"""
        if requests is None:
            self._log("WARNING", "requests が未インストールです（pip install requests）")
            return

        # ★ 事前に kabuステーション起動チェック
        if not self._precheck_station():
            return

        try:
            url = self._base_url() + "/token"
            payload = {"APIPassword": self.api_password.get().strip()}
            self._log("HTTP", f"POST {url} payload={payload}")
            r = requests.post(url, headers={"Content-Type": "application/json"},
                            data=json.dumps(payload), timeout=8)
            self._log("HTTP", f"status={r.status_code} resp={r.text[:200]}...")
            r.raise_for_status()
            self.token = r.json().get("Token", "")
            if self.token:
                self._log("HTTP", f"Token OK: {self.token[:8]}...")
            else:
                self._log("warning", "Token 取得失敗：応答に Token がありません")
        except Exception as e:
            self._log_exc("HTTP", e)

    # -----------------
    # AUTO
    # -----------------

    # ====== 既存：SIM履歴の行追加（v3日本語化） ======
    def _append_sim_history(self, fill: dict):
        """
        fill 例:
        {
            "約定時刻": "2025-08-30 10:12:34",
            "銘柄": "7203",
            "サイド": "買",
            "数量": 100,
            "建値": 7710.0,
            "決済時刻": "2025-08-30 10:13:10",
            "決済値": 7717.0,
            "損益": 700,
            "理由": "TP成立（imb=+0.34, INV, spread=0.5）"
        }
        """
        try:
            # ツリーが未構築でも落とさない
            tree = getattr(self, "tree_sim", None)
            if tree:
                # 無ければカラム作成（日本語）
                if not tree["columns"]:
                    cols = ("約定時刻","銘柄","サイド","数量","建値","決済時刻","決済値","損益","理由")
                    tree.configure(columns=cols, show="headings")
                    for c,w in (("約定時刻",160),("銘柄",90),("サイド",60),("数量",60),
                                ("建値",80),("決済時刻",160),("決済値",80),("損益",80),("理由",220)):
                        tree.heading(c, text=c); tree.column(c, width=w, anchor="center")
                vals = (fill.get("約定時刻",""), fill.get("銘柄",""), fill.get("サイド",""),
                        fill.get("数量",0), fill.get("建値",0.0), fill.get("決済時刻",""),
                        fill.get("決済値",0.0), fill.get("損益",0), fill.get("理由",""))
                tree.insert("", "end", values=vals)
            # 集計ラベル更新（v4関数があれば使用）
            upd = getattr(self, "_update_stats_from_tree", None)
            if upd: upd("SIM")
        except Exception:
            self._log_exc("append_sim_history")

    # ====== 補助: サマリーのポジション表示更新 ======
    def _update_sim_pos_label(self):
        try:
            if not self.lbl_pos_sim: return
            p = self._sim_pos
            if p.qty == 0:
                self.lbl_pos_sim.config(text="—")
            else:
                jp_side = "買" if p.side == "BUY" else "売"
                self.lbl_pos_sim.config(text=f"{jp_side} {p.qty}＠{p.avg:.1f}")
        except Exception:
            self._log_exc("update_sim_pos_label")

    # ====== 既存メトリクス取得（無ければ簡易算出） ======
    def _current_quote(self):
        # 既存属性がある前提。無ければ 0 扱い
        bid = float(getattr(self, "best_bid", 0) or 0)
        ask = float(getattr(self, "best_ask", 0) or 0)
        bq  = float(getattr(self, "best_bidq", 0) or 0)
        aq  = float(getattr(self, "best_askq", 0) or 0)
        last= float(getattr(self, "last_price", 0) or 0)
        sym = getattr(self, "current_symbol", "") or getattr(self, "symbol", "")
        return {"bid":bid, "ask":ask, "bidq":bq, "askq":aq, "last":last, "sym":sym}

    def _derive_metrics(self, q):
        # 既存に self._derive_book_metrics があればそれを尊重
        try:
            if hasattr(self, "_derive_book_metrics"):
                return self._derive_book_metrics(q["bid"], q["ask"], q["bidq"], q["askq"], q["last"])
        except Exception:
            pass
        # 簡易
        spread = (q["ask"] - q["bid"]) if (q["ask"] and q["bid"]) else 0.0
        inv = (q["ask"] <= q["bid"]) if (q["ask"] and q["bid"]) else False
        imb = None
        if (q["bidq"] + q["askq"]) > 0:
            imb = (q["bidq"] - q["askq"]) / (q["bidq"] + q["askq"])  # -1..+1
        return {"spread": spread, "_inv": inv, "imbalance": imb}

    # ====== ログ整形（日本語） ======
    def _auto_log(self, action: str, reason: str, q=None, m=None):
        # action: "見送り" / "指値発注" / "約定" / "決済(TP/SL)" など
        try:
            parts = [action]
            if reason: parts.append(f"理由: {reason}")
            if q:
                parts.append(f"bid={q['bid']:.1f} ask={q['ask']:.1f} spread={m['spread']:.2f}")
                if m.get('imbalance') is not None: parts.append(f"imb={m['imbalance']:+.2f}")
                parts.append("INV" if m.get("_inv") else "非INV")
            self._log("AUTO", " | ".join(parts))
        except Exception:
            self._log_exc("auto_log")

    # ====== 発注・約定（SIM） ======
    def _place_sim_limit(self, side:str, price:float, qty:int, kind:str):
        oid = self._sim_next_oid; self._sim_next_oid += 1
        o = SimOrder(id=oid, side=side, price=price, qty=qty, ts=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind=kind)
        self._sim_orders.append(o)
        return o

    def _try_fill_orders(self, q):
        """ ベスト気配で指値がヒットしたら即約定 """
        filled = []
        for o in self._sim_orders:
            if o.status != "OPEN": continue
            if o.side == "BUY":
                # 買い指値は ask <= 価格 で約定
                if q["ask"] and q["ask"] <= o.price:
                    o.status = "FILLED"; filled.append(o)
            else:
                # 売り指値は bid >= 価格 で約定
                if q["bid"] and q["bid"] >= o.price:
                    o.status = "FILLED"; filled.append(o)
        for o in filled:
            self._on_order_filled(o, q)

    def _on_order_filled(self, o: SimOrder, q):
        if o.kind == "ENTRY":
            # オープン
            self._open_position(o.side, o.qty, o.price)
            # OCO（TP/SL）を建てる
            t = self.auto_cfg["tp_ticks"] * self.auto_cfg["tick_size"]
            s = self.auto_cfg["sl_ticks"] * self.auto_cfg["tick_size"]
            if o.side == "BUY":
                self._place_sim_limit("SELL", o.price + t, o.qty, "TP")
                self._place_sim_limit("SELL", o.price - s, o.qty, "SL")
            else:
                self._place_sim_limit("BUY", o.price - t, o.qty, "TP")
                self._place_sim_limit("BUY", o.price + s, o.qty, "SL")
            self._auto_log("約定（建玉）", "", q, self._derive_metrics(q))
        else:
            # 決済
            reason = "TP成立" if o.kind == "TP" else "SL成立"
            self._close_position(o.qty, o.price, reason)

    def _open_position(self, side, qty, price):
        p = self._sim_pos
        p.side = side
        p.qty = qty
        p.avg = float(price)
        p.entry_ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.after(0, self._update_sim_pos_label)
        # ★ここでサマリー反映（StringVar／ラベル）
        self.after(0, self._update_sim_summary)

    def _close_position(self, qty, price, reason):
        p = self._sim_pos
        if p.qty == 0: return
        # P&L 円換算（1円=1tick想定）
        sign = +1 if p.side == "BUY" else -1
        pnl = int((price - p.avg) * sign * qty)
        # SIM履歴（日本語1行）
        row = {
            "約定時刻": p.entry_ts,
            "銘柄": getattr(self, "current_symbol", "") or getattr(self, "symbol", ""),
            "サイド": "買" if p.side=="BUY" else "売",
            "数量": qty,
            "建値": p.avg,
            "決済時刻": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "決済値": float(price),
            "損益": pnl,
            "理由": f"{reason}（{p.entry_reason}）" if p.entry_reason else reason,
        }
        self.after(0, self._append_sim_history, row)
        # ポジション解消
        self._sim_pos = SimPosition()

        # 建っているOCOは全取消
        for o in self._sim_orders:
            if o.status=="OPEN" and o.kind in ("TP","SL"): o.status="CANCELLED"
        self.after(0, self._update_sim_pos_label)
        # ★ここでサマリー反映（“—”に戻る）
        self.after(0, self._update_sim_summary)

    def _update_sim_pos_label(self):
        # 互換：新実装へ集約
        self._update_sim_summary()

    # ====== AUTO意思決定（見送りロジックは日本語でログ） ======
    def _auto_decide(self, q, m):
        """
        戻り値: (action, side, price, reason)
        action: "SKIP" / "ENTRY"
        """
        # 既にポジションがあれば新規は発注しない（OCO待ち）
        if self._sim_pos.qty != 0:
            return ("SKIP", None, None, "既に建玉あり（新規停止）")

        # フィルタ群
        if m["spread"] <= 0:
            return ("SKIP", None, None, "板不整合（スプレッド0以下）")

        if m["spread"] > self.auto_cfg["max_spread"]:
            return ("SKIP", None, None, f"見送り：スプレッド広い({m['spread']:.2f} > {self.auto_cfg['max_spread']:.2f})")

        imb = m.get("imbalance", None)
        if imb is None:
            return ("SKIP", None, None, "見送り：板数量データ不足")

        if abs(imb) < self.auto_cfg["min_abs_imb"]:
            return ("SKIP", None, None, f"見送り：板バランス弱い(|imb|={abs(imb):.2f} < {self.auto_cfg['min_abs_imb']:.2f})")

        if self.auto_cfg["require_inv"] and not m.get("_inv", False):
            return ("SKIP", None, None, "見送り：INV不成立（食い込み無し）")

        # 方向決定：imb>0は買い優位、imb<0は売り優位
        if imb > 0:
            side = "BUY"; price = q["bid"]  # 指値は現行bid（参加）
            reason = f"買い優位（imb={imb:+.2f}, spread={m['spread']:.2f}" + (", INV" if m["_inv"] else "") + ")"
        else:
            side = "SELL"; price = q["ask"]
            reason = f"売り優位（imb={imb:+.2f}, spread={m['spread']:.2f}" + (", INV" if m["_inv"] else "") + ")"

        return ("ENTRY", side, price, reason)

    # ====== AUTO ループ ======
    def toggle_auto(self):
        if self.auto_on:
            self._auto_stop.set()
            if self._auto_th: self._auto_th.join(timeout=1.5)
            self.auto_on = False
            self._auto_log("AUTO停止", "", None, {})
            return
        self._auto_stop.clear()
        self.auto_on = True
        self._auto_th = threading.Thread(target=self._auto_loop, daemon=True); self._auto_th.start()
        self._auto_log("AUTO開始", "", None, {})

    def _auto_loop(self):
        try:
            while not self._auto_stop.is_set():
                time.sleep(self.auto_cfg["poll_ms"]/1000.0)
                q = self._current_quote()
                if not (q["bid"] and q["ask"]): continue
                m = self._derive_metrics(q)

                # 指値のヒット監視（常に先に判定）
                with self._auto_lock:
                    self._try_fill_orders(q)

                # 新規判断
                act, side, price, reason = self._auto_decide(q, m)
                if act == "SKIP":
                    # ノイズ抑制：INVやスプレッド等の条件が同じ場合は間引き可（dedup_key）
                    self._auto_log("見送り", reason, q, m)
                    continue
                # act == "ENTRY" のときだけ
                if act == "ENTRY":
                    allow, _adj, msg = self.spoof.apply_gate(
                        proposed_side=('B' if side=='BUY' else 'S'),
                        entry_confidence=1.0
                    )
                    if not allow:
                        self._auto_log("見送り", f"見せ板ゲート: {msg}", q, m)
                        continue
                    if msg:
                        self._auto_log("AUTO", f"見せ板ゲート: {msg}", q, m)

                # 指値発注
                with self._auto_lock:
                    o = self._place_sim_limit(side, price, self.auto_cfg["qty"], "ENTRY")
                    # エントリー根拠（ポジションに覚えさせる）
                    self._sim_pos.entry_reason = reason
                self._auto_log("指値発注", f"{'買' if side=='BUY' else '売'} {self.auto_cfg['qty']}＠{price:.1f} | {reason}", q, m)

                # 発注直後にヒットしていれば執行
                with self._auto_lock:
                    self._try_fill_orders(q)
        except Exception as e:
            try:
                self._log_exc("_auto_loop", e)
            except TypeError:
                self._log_exc(e, where="_auto_loop")




    def _base_url(self) -> str:
        return f"http://localhost:{18080 if self.is_production.get() else 18081}/kabusapi"

    def _ws_url(self) -> str:
        return f"ws://localhost:{18080 if self.is_production.get() else 18081}/kabusapi/websocket"

    def _get_token(self):
        """POST /token に APIパスワードを送り、Token を取得→ログ出力。"""
        if requests is None:
            self._log("HTTP", "requests が未インストールです（pip install requests）"); return
        try:
            url = self._base_url()+"/token"
            payload = {"APIPassword": self.api_password.get().strip()}
            self._log("HTTP", f"POST {url} payload={payload}")
            r = requests.post(url, headers={"Content-Type":"application/json"}, data=json.dumps(payload), timeout=8)
            self._log("HTTP", f"status={r.status_code} resp={r.text[:200]}...")
            r.raise_for_status()
            self.token = r.json().get("Token", "")
            if self.token:
                self._log("HTTP", f"Token OK: {self.token[:8]}...")
            else:
                self._log("HTTP", "Token 取得失敗：応答に Token がありません")
        except Exception as e:
            self._log_exc("HTTP", e)

    def _apply_startup_options(self, args):
        """CLIの値を UI変数 & 内部変数へ流し込む"""
        # ← ここで環境変数から入った args.api_pass を self.api_password へ代入します
        if getattr(args, "api_pass", None):
            self.api_password.set(args.api_pass)  # ★ 質問の1行はここ
            self._log("CFG", "APIパスワード: 起動時オプション/環境変数から設定")

        # 参考：他の起動フラグも反映
        if getattr(args, "production", False):
            self.is_production.set(True)
        if getattr(args, "sandbox", False):
            self.is_production.set(False)
        if getattr(args, "real", False):
            self.real_trade.set(True)
        if getattr(args, "debug", False):
            self.debug_mode.set(True)
        if getattr(args, "symbol", None):
            self.symbol_var.set(args.symbol.strip())

        # デバッグ用の見える化
        #self._log("CFG", f"env→api_pass len={len(args.api_pass) if args.api_pass else 0}")
        #self._log("CFG", f"self.api_password len={len(self.api_password.get() or '')}")

    # --------------------------------------------------
    # 互換メソッド（V3名で呼ばれる想定の最低限の中身）
    # --------------------------------------------------
    def _append_sim_history(self, ts: Optional[str] = None, sym: Optional[str] = None,
                             side: str = "BUY", qty: int = 100, entry: float = 0.0,
                             exit_px: float = 0.0, ticks: float = 0.0, pnl: float = 0.0,
                             reason: str = "ENTER") -> None:
        """SIM履歴への1行追記（V3の列構成に合わせて挿入）"""
        if ts is None:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
        sym = sym or self.symbol_var.get().strip()
        self.tree_sim.insert("", "end", values=(ts, sym, side, qty, entry, exit_px, ticks, pnl, reason))
        self._update_stats_from_tree("SIM")

    def _resolve_symbol_name(self, code: str) -> str:
        if not code: return ""
        if hasattr(self, "_name_cache") and code in self._name_cache:
            return self._name_cache[code]
        # 必要ならその場で /symbol を叩いて取得（省略可）
        try:
            j = self._http_get("SYMBOL", f"/symbol/{code}@{getattr(self,'EXCHANGE',1)}")
            name = j.get("SymbolName") or j.get("IssueName") or ""
            if name:
                if not hasattr(self, "_name_cache"): self._name_cache = {}
                self._name_cache[code] = name
            return name
        except Exception:
            return ""

    def update_orders(self) -> None:
        """HTTP /orders 取得→_fill_orders へ。スケルトンではダミー。"""
        rows = []  # 実装時は REST から取得
        self._fill_orders(rows)

    def _fill_orders(self, rows: List[Dict[str, Any]]) -> None:
        try:
            for iid in self.tree_ord.get_children():
                self.tree_ord.delete(iid)
            for o in rows:
                sym = str(o.get("Symbol", ""))
                name = self._resolve_symbol_name(sym) or str(o.get("SymbolName", ""))
                side = "買" if int(o.get("Side", 2)) == 2 else "売"
                qty = int(float(o.get("Qty", 0)))
                price = float(o.get("Price", 0.0))
                st = str(o.get("State", o.get("Status", "")))
                oid = str(o.get("ID", o.get("OrderId", "")))
                self.tree_ord.insert("", "end", values=(oid, sym, name, side, qty, price, st))
        finally:
            self._update_stats_from_tree("LIVE")  # LIVE履歴から集計する想定

    # --------------------------------------------------
    # 成績集計（SIM/LIVE 共通）
    # --------------------------------------------------
    def _get_tree(self, mode: str = "SIM"):
        if mode == "LIVE":
            return getattr(self, "tree_live", None)
        return getattr(self, "tree_sim", None)

    def _rows_from_tree(self, mode: str = "SIM") -> List[Dict[str, Any]]:
        tv = self._get_tree(mode)
        if tv is None:
            return []
        rows: List[Dict[str, Any]] = []
        for iid in tv.get_children(""):
            v = tv.item(iid, "values")
            rows.append({
                "time": v[0] if len(v) > 0 else "",
                "sym": v[1] if len(v) > 1 else "",
                "side": v[2] if len(v) > 2 else "",
                "qty": _to_float(v[3] if len(v) > 3 else 0),
                "entry": _to_float(v[4] if len(v) > 4 else 0),
                "exit": _to_float(v[5] if len(v) > 5 else 0),
                "ticks": _to_float(v[6] if len(v) > 6 else 0),
                "pnl": _to_float(v[7] if len(v) > 7 else 0),
                "reason": v[8] if len(v) > 8 else "",
            })
        return rows

    def _update_stats_from_tree(self, mode: str = "SIM") -> None:
        rows = self._rows_from_tree(mode)
        # EXIT行のみ集計（ENTER除外）
        ex = [r for r in rows if isinstance(r.get("reason"), str) and ("EXIT" in r.get("reason"))]
        n = len(ex)
        wins = sum(1 for r in ex if r.get("ticks", 0) > 0 or r.get("pnl", 0) > 0)
        ticks_sum = sum(r.get("ticks", 0) for r in ex)
        pnl_sum = sum(r.get("pnl", 0) for r in ex)
        wr = (wins / n * 100.0) if n else 0.0
        avg = (ticks_sum / n) if n else 0.0
        text = f"Trades: {n} | Win: {wins} ({wr:.1f}%) | P&L: ¥{int(pnl_sum):,} | Avg: {avg:.2f}t"
        if mode == "LIVE":
            self.var_livestats.set("LIVE: " + text)
        else:
            self.var_simstats.set("SIM: " + text)

    def _stats_heartbeat(self) -> None:
        try:
            self._update_stats_from_tree("SIM")
            self._update_stats_from_tree("LIVE")
        finally:
            self.after(1000, self._stats_heartbeat)

    def _update_summary(self):
        base = self._get_base_code()
        name = self._resolve_symbol_name(base)
        price = self._last_price_by_code.get(base, getattr(self, "last_price", None))
        prev  = self._prev_close_by_code.get(base, getattr(self, "prev_close", None))

        if price is None:
            self.lbl_price.config(text=f"{base} {name} —", fg="black")
            self.lbl_change.config(text=" — ", fg="black")
            return

        self.lbl_price.config(text=f"{base} {name} {price:,.1f}", fg="black")
        if prev:
            diff = price - prev
            pct  = diff / prev * 100.0
            col  = "green" if diff >= 0 else "red"
            self.lbl_change.config(text=f"{diff:+.1f} ({pct:+.2f}%)", fg=col)
        else:
            self.lbl_change.config(text=" — ", fg="black")


    # ----- 銘柄関連 -----

    def _ensure_summary_labels(self):
        import tkinter as tk, tkinter.ttk as ttk
        if getattr(self, "lbl_price", None) and getattr(self, "lbl_change", None):
            return
        # 右上に summary コンテナが無いなら作る
        parent = getattr(self, "right_summary", None)
        if not parent:
            parent = ttk.Frame(self.right) if hasattr(self, "right") else ttk.Frame(self)
            parent.pack(fill="x", pady=(4,0))
            self.right_summary = parent

    def _snapshot_combo(self):
        """/symbol → /board を順に叩いてサマリー更新"""
        try:
            self._ensure_summary_labels()
            if not getattr(self, "token", ""):
                return self._log("warning", "Token未取得です（先にトークン取得）")
            self._snapshot_symbol_once()
            self._snapshot_board()
        except Exception as e:
            self._log_exc("HTTP", e)

    def _symbol_code_with_ex(self) -> str:
        code = (self.symbol_var.get() or "").strip()
        if not code: return ""
        return code if "@" in code else f"{code}@{getattr(self, 'EXCHANGE', 1)}"

    def _snapshot_symbol_once(self):
        """一度 /symbol で名称などを取得→キャッシュ"""
        code_ex = self._symbol_code_with_ex()
        if not code_ex: return
        j = self._http_get("SYMBOL", f"/symbol/{code_ex}")
        name = j.get("SymbolName") or j.get("IssueName") or ""
        if name:
            if not hasattr(self, "_name_cache"): self._name_cache = {}
            self._name_cache[(code_ex.split("@")[0])] = name
        # 上下限などの更新があれば呼ぶ（未実装ならそのまま）
        try: self._update_limits_from_symbol(j)
        except Exception: pass
        # ひとまずサマリー描画（価格は後続の/boardで）
        self.ui_call(self._update_summary)

    def _snapshot_board(self):
        """
        スナップショット取得：
        1) /board から板・価格を取得
        2) サマリー（現在値/前日比など）更新
        3) ラダー（Canvas）描画
        4) Sp/Imb/Inv 更新
        5) Best 枚数の変化から疑似歩み値を更新
        """
        try:
            # --- トークン/銘柄チェック ---
            if not getattr(self, "token", ""):
                return self._log("HTTP", "Token未取得です（[Token] を先に実行してください）")
            base_code = (self.symbol_var.get() or "").strip()
            if not base_code:
                return self._log("HTTP", "銘柄コードが未設定です（左ペインで設定してください）")

            # 取引所指定（未指定なら東証=1）
            ex = getattr(self, "EXCHANGE", 1)
            code_ex = base_code if "@" in base_code else f"{base_code}@{ex}"

            # --- /board 取得 ---
            j = self._http_get("BOARD", f"/board/{code_ex}")
            if not isinstance(j, dict):
                return self._log("HTTP", f"/board 異常応答: {type(j)}")
            self._last_board_json = j  # 後続の再描画に使う

            # --- サマリー用：現在値・前日終値 ---
            last = j.get("CurrentPrice")
            if last is not None:
                try: self.last_price = float(last)
                except Exception: pass

            if getattr(self, "prev_close", None) is None:
                prev = j.get("PreviousClose") or j.get("BasePrice")
                if prev is not None:
                    try: self.prev_close = float(prev)
                    except Exception: pass

            if hasattr(self, "_update_summary"):
                self.ui_call(self._update_summary)

            # --- 板レベル抽出（API上限10まで）---
            depth = 10
            try:
                depth_var = getattr(self, "book_depth", None)
                depth = int(depth_var.get()) if depth_var is not None else 10
            except Exception:
                depth = 10
            depth = max(1, min(10, depth))

            sells, buys, over, under = self._extract_levels(j, depth=depth)

            # --- ラダー描画（Canvas）---
            # base_rows: [(ask_txt, price_txt, bid_txt, tag)]
            base_rows = self._build_ladder_rows(sells, buys, over, under)
            if hasattr(self, "_render_ladder"):
                self.ui_call(self._render_ladder, base_rows)

            # --- メトリクス更新（Spread / Imbalance / Inventory）---
            bid1p, bid1q = (buys[0]  if buys  else (None, None))
            ask1p, ask1q = (sells[0] if sells else (None, None))

            if bid1p is not None and ask1p is not None:
                try: self.ui_call(self.var_spread.set, f"Sp: {ask1p - bid1p:,.1f}")
                except Exception: pass

                tot = (bid1q or 0) + (ask1q or 0)
                if tot > 0:
                    imb = ((bid1q or 0) - (ask1q or 0)) / tot * 100.0
                    try: self.ui_call(self.var_imbal.set, f"Imb: {imb:+.1f}%")
                    except Exception: pass
                else:
                    try: self.ui_call(self.var_imbal.set, "Imb: —")
                    except Exception: pass
            else:
                try:
                    self.ui_call(self.var_spread.set, "Sp: —")
                    self.ui_call(self.var_imbal.set, "Imb: —")
                except Exception:
                    pass

            # Inv（建玉ネット枚数）
            try:
                inv = self._get_net_position_qty()
                self.ui_call(self.var_inv.set, f"Inv: {inv}")
            except Exception:
                pass

            # --- 疑似歩み値（Best枚数の減少を成行約定として記録）---
            try:
                self._update_tape_from_l1(getattr(self, "_prev_l1", None), bid1p, bid1q, ask1p, ask1q)
            except Exception:
                pass

        except Exception as e:
            self._log_exc("HTTP", e)




    def _get_base_code(self) -> str:
        """Entryの値から4桁コードを抽出（'8136@1' → '8136'）"""
        import re
        raw = (self.symbol_var.get() or "").strip()
        if "@" in raw: raw = raw.split("@", 1)[0]
        m = re.search(r"\b(\d{4})\b", raw)
        return m.group(1) if m else raw

    def _reset_quote_state(self):
        """銘柄切替時に前回の見値をリセット"""
        self.last_price = None
        self.prev_close = None
        self.last_snapshot = {}
        self.last_quote = {}

    # --------------------------------------------------
    # API銘柄登録
    #---------------------------------------------------
    def _register_symbols(self, symbols):
        """symbols=[('7203',1),('6758',1)] を登録"""
        if not getattr(self, "token", ""): return self._log("warning","Token未取得")
        payload = {"Symbols":[{"Symbol":s,"Exchange":ex} for s,ex in symbols]}
        url = self._base_url()+"/register"
        self._log("HTTP", f"PUT {url} {payload}")
        r = requests.put(url, headers={"Content-Type":"application/json","X-API-KEY":self.token},
                        data=json.dumps(payload), timeout=6)
        self._log("HTTP", f"status={r.status_code} resp={(r.text or '')[:200]}...")
        r.raise_for_status()

    def _unregister_all(self):
        url = self._base_url()+"/unregister/all"
        self._log("HTTP", f"PUT {url}")
        r = requests.put(url, headers={"X-API-KEY":self.token}, timeout=6)
        self._log("HTTP", f"status={r.status_code} resp={(r.text or '')[:200]}...")
        r.raise_for_status()
    # --------------------------------------------------
    # 書き出し（CSV / XLSX）
    # --------------------------------------------------
    def _export_tree_csv(self, tv: ttk.Treeview) -> None:
        if tv is None:
            messagebox.showinfo("保存", "出力できるデータがありません。")
            return
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=f"trades_{ts}.csv",
            title="履歴の保存先を選択",
        )
        if not path:
            return
        cols = ["time", "sym", "side", "qty", "entry", "exit", "ticks", "pnl", "reason"]
        rows = []
        for iid in tv.get_children(""):
            v = tv.item(iid, "values")
            rows.append({
                "time": v[0] if len(v) > 0 else "",
                "sym": v[1] if len(v) > 1 else "",
                "side": v[2] if len(v) > 2 else "",
                "qty": v[3] if len(v) > 3 else 0,
                "entry": v[4] if len(v) > 4 else 0,
                "exit": v[5] if len(v) > 5 else 0,
                "ticks": v[6] if len(v) > 6 else 0,
                "pnl": v[7] if len(v) > 7 else 0,
                "reason": v[8] if len(v) > 8 else "",
            })
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})
        messagebox.showinfo("保存", f"CSV保存: {path}")

    def _export_tree_xlsx(self, tv: ttk.Treeview) -> None:
        if tv is None:
            messagebox.showinfo("保存", "出力できるデータがありません。")
            return
        try:
            import pandas as pd
        except Exception:
            messagebox.showwarning("依存ライブラリ", "pandas がありません。pip install pandas")
            return
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"trades_{ts}.xlsx",
            title="履歴の保存先を選択",
        )
        if not path:
            return
        rows = []
        for iid in tv.get_children(""):
            v = tv.item(iid, "values")
            rows.append(v)
        df = pd.DataFrame(rows, columns=["time","sym","side","qty","entry","exit","ticks","pnl","reason"])
        df.to_excel(path, index=False)
        messagebox.showinfo("保存", f"XLSX保存: {path}")

    # --------------------------------------------------
    # その他UIイベント
    # --------------------------------------------------

    def _save_settings_dialog(self):
        self._log("CFG", "設定保存は未実装です（後で実装）")

    def _apply_symbol(self):
        import threading
        base = self._get_base_code()
        if base != self._active_code:
            self._active_code = base
            self._reset_quote_state()
            self._log("CFG", f"銘柄切替: {base}")
            # キャッシュがあれば即反映（再描画だけ先に）
            if base in self._prev_close_by_code:
                self.prev_close = self._prev_close_by_code[base]
            if base in self._last_price_by_code:
                self.last_price = self._last_price_by_code[base]
            self.ui_call(self._update_summary)
        # 取得は明示ボタン/Enterで行う。適用でも走らせたいなら次の行を有効化:
        threading.Thread(target=self._snapshot_combo, daemon=True).start()

    def _save_rm_settings_dialog(self) -> None:
        messagebox.showinfo("資金管理", "資金管理の保存は後で実装します。")

    def _log_ui(self, msg: str) -> None:
        try:
            self.log_box.insert("end", (msg,))
            self.log_box.see("end")
        except Exception:
            pass

    # ------------------
    # サマリー
    # ------------------


    def _update_sim_summary(self):
        """SIMサマリー（保有＆株数）を StringVar / ラベル に反映"""
        try:
            p = getattr(self, "_sim_pos", None)
            if not p: return
            if p.qty == 0:
                text = "—"
                qty  = str(self.qty.get()) if hasattr(self, "qty") else "0"
            else:
                side_jp = "買" if p.side == "BUY" else "売"
                text = f"{side_jp} {p.qty}＠{p.avg:.1f}"
                qty  = str(p.qty)

            # StringVar 優先（v4 UI）
            if hasattr(self, "var_sim_pos") and self.var_sim_pos:
                self.var_sim_pos.set(text)
            if hasattr(self, "var_sim_qty") and self.var_sim_qty:
                self.var_sim_qty.set(qty)

            # v3互換のラベルがある場合も更新（後方互換）
            if getattr(self, "lbl_pos_sim", None):
                self.lbl_pos_sim.config(text=text)
        except Exception as e:
            try:
                self._log_exc("_update_sim_summary", e)
            except TypeError:
                self._log_exc(e, where="_update_sim_summary")


    def _bind_qty_to_sim_summary(self):
        # qty(IntVar) がある前提。無ければスキップ
        try:
            if hasattr(self, "qty") and hasattr(self.qty, "trace_add"):
                def _on_qty_change(*_):
                    # 建玉なしのときだけ既定数量を表示
                    if getattr(self, "_sim_pos", None) and self._sim_pos.qty == 0:
                        if hasattr(self, "var_sim_qty") and self.var_sim_qty:
                            self.var_sim_qty.set(str(self.qty.get()))
                self.qty.trace_add("write", _on_qty_change)
        except Exception as e:
            try:
                self._log_exc("_bind_qty_to_sim_summary", e)
            except TypeError:
                self._log_exc(e, where="_bind_qty_to_sim_summary")


    # ========== 成績行（SIM/LIVE）集計：StringVarに反映 ==========
    def _fmt_yen(self, v):
        try:
            return f"¥{int(round(float(v))):,}"
        except Exception:
            return "¥0"

    def _safe_float(self, x, default=0.0):
        try:
            return float(x)
        except Exception:
            return default

    def _safe_int(self, x, default=0):
        try:
            return int(x)
        except Exception:
            return default

    def _update_stats_from_tree(self, mode: str):
        """
        mode: "SIM" or "LIVE"
        - SIM: tree_sim を前提（列： 約定時刻/銘柄/サイド/数量/建値/決済時刻/決済値/損益/理由）
        - LIVE: tree_live があれば同様に集計（無ければスキップ）
        出力:
        var_simstats / var_livestats を更新（無ければ v3互換ラベルを更新）
        """
        try:
            if mode == "SIM":
                tree = getattr(self, "tree_sim", None)
                var  = getattr(self, "var_simstats", None)
                lbl  = getattr(self, "lbl_stats_sim", None)  # 互換
                prefix = "SIM"
            else:
                tree = getattr(self, "tree_live", None)
                var  = getattr(self, "var_livestats", None)
                lbl  = getattr(self, "lbl_stats_live", None)  # あれば
                prefix = "LIVE"

            if not tree or not tree.get_children(""):
                text = f"{prefix}: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t"
                if var: var.set(text)
                if lbl: lbl.config(text=text)
                return

            tick = float(self.auto_cfg.get("tick_size", 1.0)) if hasattr(self, "auto_cfg") else 1.0

            n_trades = 0
            wins = 0
            pnl_sum = 0.0
            ticks_acc = 0.0  # 取引毎のtick差の平均用

            for iid in tree.get_children(""):
                vals = tree.item(iid, "values")
                # 期待順: [約定時刻, 銘柄, サイド(買/売), 数量, 建値, 決済時刻, 決済値, 損益, 理由]
                side_j = (vals[2] if len(vals) > 2 else "") or ""
                qty    = self._safe_int(vals[3] if len(vals) > 3 else 0, 0)
                entry  = self._safe_float(vals[4] if len(vals) > 4 else 0.0, 0.0)
                exitp  = self._safe_float(vals[6] if len(vals) > 6 else 0.0, 0.0)
                pnl    = self._safe_float(vals[7] if len(vals) > 7 else 0.0, 0.0)

                # 完結した行（決済値がある）だけ集計
                if exitp == 0 and pnl == 0:
                    continue

                n_trades += 1
                pnl_sum += pnl
                if pnl > 0:
                    wins += 1

                # 平均tick: サイドから符号を決定
                if tick > 0:
                    if side_j.startswith("買"):
                        t = (exitp - entry) / tick
                    elif side_j.startswith("売"):
                        t = (entry - exitp) / tick
                    else:
                        # サイド情報が無い場合は pnl と qty から推定（qty=0の場合は0）
                        t = (pnl / (qty if qty else 1)) / tick
                    ticks_acc += t

            if n_trades == 0:
                avg_ticks = 0.0
                winp = 0.0
            else:
                avg_ticks = ticks_acc / n_trades
                winp = (wins / n_trades) * 100.0

            text = f"{prefix}: Trades: {n_trades} | Win: {wins} ({winp:.1f}%) | P&L: {self._fmt_yen(pnl_sum)} | Avg: {avg_ticks:.2f}t"

            if var:
                var.set(text)
            if lbl:
                lbl.config(text=text)

        except Exception as e:
            try:
                self._log_exc("_update_stats_from_tree", e)
            except TypeError:
                self._log_exc(e, where="_update_stats_from_tree")


    def _update_live_summary(self, side:str=None, qty:int=None, avg:float=None):
        try:
            if side is None or qty is None or avg is None:
                text, n = "—", (self.qty.get() if hasattr(self,"qty") else 0)
            else:
                text = f"{'買' if side=='BUY' else '売'} {qty}＠{avg:.1f}"
                n = qty
            if hasattr(self, "var_live_pos") and self.var_live_pos:
                self.var_live_pos.set(text)
            if hasattr(self, "var_live_qty") and self.var_live_qty:
                self.var_live_qty.set(str(n))
        except Exception:
            self._log_exc("update_live_summary")


    # v3→v4 互換（既に作っていなければ）


    def _update_sim_stats_from_tree(self):
        self._update_stats_from_tree("SIM")



# ======================================================
# エントリポイント
# ======================================================

def main() -> None:
    app = App()
    app.mainloop()


# ======================================================
# CLI（V3準拠）と main の差し替え
# ======================================================

def _parse_cli_args():
    p = argparse.ArgumentParser(description="Kabus GUI 起動オプション")
    p.add_argument("--preset", choices=["標準","高ボラ","低ボラ","std","volatile","calm"], help="パラメータプリセット")
    p.add_argument("--symbol", help="初期銘柄（例: 7203@1）")
    p.add_argument("--qty", type=int, help="数量（株）")
    p.add_argument("--tp", type=int, help="利確 tick")
    p.add_argument("--sl", type=int, help="損切 tick")
    p.add_argument("--imb", type=float, help="Imbalance 閾値（例 0.35）")
    p.add_argument("--cooldown", type=int, help="クールダウン ms（例 400）")
    p.add_argument("--spread", type=int, help="許容スプレッド上限（tick）")
    p.add_argument("--size-ratio", type=float, help="Best枚数に対する使用率（0.1〜1.0）")
    p.add_argument("--production", action="store_true", help="本番(:18080)で起動")
    p.add_argument("--sandbox", action="store_true", help="検証(:18081)で起動（デフォルト）")
    p.add_argument("--real", action="store_true", help="実発注ONで起動（ARMしない限り送信しません）")
    p.add_argument("--ml", choices=["on","off"], help="MLゲート 有効/無効")
    p.add_argument("--debug", action="store_true", help="デバッグ（意思決定ログ）ON")
    p.add_argument("--auto-start", action="store_true", help="起動時に ①トークン まで自動実行")
    p.add_argument("--api-pass", help="APIパスワード（推奨しない）")
    p.add_argument("--api-pass-env", default="KABU_API_PASSWORD", help="APIパスワードを読む環境変数名（既定: KABU_API_PASSWORD）")
    args = p.parse_args()
    if not getattr(args, "api_pass", None):
        env_name = args.api_pass_env or "KABU_API_PASSWORD"
        args.api_pass = os.environ.get(env_name, "")
    return args


def main() -> None:
    args = _parse_cli_args()
    app = App()
    try:
        if hasattr(app, "_apply_startup_options"):
            app._apply_startup_options(args)
    except Exception as e:
        print("apply options error:", e)
    if getattr(args, "auto_start", False):
        import threading
        threading.Thread(target=getattr(app, "_boot_seq", lambda: None), daemon=True).start()
    app.mainloop()


if __name__ == "__main__":
    main()
