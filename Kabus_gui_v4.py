# -*- coding: utf-8 -*-
# =========================
# kabuS V4 skeleton (mock)
# =========================

# ---- Imports ----
import os
import csv
import time
import math
import json
import pathlib as _pl
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional

import tkinter as tk
from tkinter import ttk

# ---- Constants ----
APP_NAME      = "kabuS V4"
APP_VERSION   = "4.0.0-skeleton"
DEFAULT_CODE  = "8136"          # Sanrio
DEFAULT_NAME  = "サンリオ"
DEFAULT_QTY   = 100
TICK_SIZE_DEF = 0.5

ROOT_DIR      = _pl.Path(__file__).resolve().parent
LOG_DIR       = ROOT_DIR / "logs"
SIM_DIR       = ROOT_DIR / "sim_logs"
MODEL_DIR     = ROOT_DIR / "ml" / "models"
for _d in (LOG_DIR, SIM_DIR, MODEL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# =============================================================================
# App
# =============================================================================
class App(tk.Tk):
    """V4 メインアプリ（UIは最小、移植しやすい骨子）"""

    # 既知の V3→V4 属性名マップ（__getattr__ の保険用）
    _V3_ATTR_MAP = {
        "tree_hist": "tree_sim",
        "lbl_stats": "lbl_stats_sim",
        "lbl_simpos": "lbl_pos_sim",
    }

    def __init__(self) -> None:
        super().__init__()
        # ---- window ----
        self.title(f"{APP_NAME} ({APP_VERSION})")
        self.geometry("1280x800")
        try:
            self.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass

        # ---- runtime states (V3互換っぽい名前を併記) ----
        self.symbol     = tk.StringVar(value=DEFAULT_CODE)
        self.symbol_name= DEFAULT_NAME
        self.last_price: Optional[float] = None
        self.prev_close: Optional[float] = None
        self.tick_size  = TICK_SIZE_DEF

        # 数量・デバッグ・ARM（実弾許可）
        self.default_qty_var = tk.IntVar(value=DEFAULT_QTY)
        self.debug_mode      = tk.IntVar(value=0)  # ←V3互換名のまま
        self.arm_mode        = tk.IntVar(value=0)

        # SIM/LIVE 成績の簡易保持
        self.sim_stats  = {"trades":0, "wins":0, "ticks_sum":0.0, "pnl_yen":0.0}
        self.live_stats = {"trades":0, "wins":0, "ticks_sum":0.0, "pnl_yen":0.0}

        # ツリービュー/ラベルへの参照（_build_ui で実体化）
        self.tree_live = None
        self.tree_sim  = None
        self.lbl_pos_live = None
        self.lbl_pos_sim  = None
        self.lbl_stats_live = None
        self.lbl_stats_sim  = None

        # ---- UI ----
        self._build_ui_v4()

        # ---- 互換レイヤを最後に注入（V3 名で呼ばれても動く）----
        self._install_v3_compat()

    # -------------------------------------------------------------------------
    # UI（最小）
    # -------------------------------------------------------------------------
    def _build_ui_v4(self) -> None:
        pw = ttk.Panedwindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)

        # LEFT: 操作
        left = ttk.Frame(pw, padding=8); pw.add(left, weight=1)
        lf = ttk.LabelFrame(left, text="操作 / 設定", padding=8)
        lf.pack(fill="x")
        ttk.Checkbutton(lf, text="ARM（実弾許可）", variable=self.arm_mode).pack(anchor="w")
        ttk.Checkbutton(lf, text="DEBUG（ログ詳細）", variable=self.debug_mode).pack(anchor="w", pady=(0,6))
        ttk.Button(lf, text="AUTO ON/OFF", command=lambda: None).pack(fill="x", pady=(0,6))
        ttk.Button(lf, text="設定保存", command=lambda: None).pack(fill="x")
        ttk.Button(lf, text="使い方 / HELP", command=self._open_help).pack(fill="x", pady=(6,0))

        lf2 = ttk.LabelFrame(left, text="メイン銘柄 / 株数", padding=8)
        lf2.pack(fill="x", pady=(8,8))
        ttk.Label(lf2, text=f"{DEFAULT_CODE}（{DEFAULT_NAME}）").pack(anchor="w")
        ttk.Spinbox(lf2, from_=100, to=100000, increment=100,
                    textvariable=self.default_qty_var, width=10, state="readonly").pack(anchor="w")

        # RIGHT: summary + tabs
        right = ttk.Frame(pw, padding=8); pw.add(right, weight=4)

        # Summary（保有＆株数のみ）
        hdr = ttk.Frame(right); hdr.pack(fill="x")
        left_h = ttk.Frame(hdr); left_h.pack(side="left", fill="x", expand=True)
        ttk.Label(left_h, text=f"{DEFAULT_CODE}  {DEFAULT_NAME}  —", font=("Meiryo UI", 26, "bold")).pack(anchor="w")
        ttk.Label(left_h, text="— (+0.00%)", foreground="#188038").pack(anchor="w")

        right_h = ttk.Frame(hdr); right_h.pack(side="right")
        live_card = ttk.LabelFrame(right_h, text="LIVE"); live_card.grid(row=0, column=0, padx=(0,6))
        self.lbl_pos_live = ttk.Label(live_card, text="保有：—"); self.lbl_pos_live.grid(row=0, column=0, sticky="w")
        ttk.Label(live_card, text=f"株数：{self.default_qty_var.get()}").grid(row=1, column=0, sticky="w")

        sim_card = ttk.LabelFrame(right_h, text="SIM"); sim_card.grid(row=0, column=1)
        self.lbl_pos_sim = ttk.Label(sim_card, text="保有：—"); self.lbl_pos_sim.grid(row=0, column=0, sticky="w")
        ttk.Label(sim_card, text=f"株数：{self.default_qty_var.get()}").grid(row=1, column=0, sticky="w")

        ttk.Label(right, text="pushes=0    VWAP:—  SMA25:—  MACD:—/—  RSI:—").pack(fill="x", pady=(6,8))

        # Tabs
        nb = ttk.Notebook(right); nb.pack(fill="both", expand=True)

        # LIVE履歴
        tab_live = ttk.Frame(nb); nb.add(tab_live, text="LIVE履歴")
        bar_l = ttk.Frame(tab_live); bar_l.pack(fill="x", padx=6, pady=(8,2))
        self.lbl_stats_live = ttk.Label(bar_l, text="LIVE: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")
        self.lbl_stats_live.pack(side="left")
        ttk.Button(bar_l, text="CSV保存",  command=self.export_live_history_csv).pack(side="right", padx=(6,0))
        ttk.Button(bar_l, text="XLSX保存", command=self.export_live_history_xlsx).pack(side="right")

        self.tree_live = ttk.Treeview(
            tab_live,
            columns=("t","sym","side","qty","entry","exit","ticks","pnl","reason"),
            show="headings", height=12
        )
        self.tree_live.pack(fill="both", expand=True, padx=6, pady=6)
        for k,txt,w,a in (("t","時刻",130,"w"),("sym","銘柄",60,"center"),("side","売買",60,"center"),
                          ("qty","数量",70,"e"),("entry","建値",90,"e"),("exit","決済",90,"e"),
                          ("ticks","tick",60,"e"),("pnl","損益(円)",90,"e"),("reason","理由",180,"w")):
            self.tree_live.heading(k, text=txt); self.tree_live.column(k, width=w, anchor=a)

        # SIM履歴
        tab_sim = ttk.Frame(nb); nb.add(tab_sim, text="SIM履歴")
        bar_s = ttk.Frame(tab_sim); bar_s.pack(fill="x", padx=6, pady=(8,2))
        self.lbl_stats_sim = ttk.Label(bar_s, text="SIM:  Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")
        self.lbl_stats_sim.pack(side="left")
        ttk.Button(bar_s, text="CSV保存",  command=self.export_sim_history_csv).pack(side="right", padx=(6,0))
        ttk.Button(bar_s, text="XLSX保存", command=self.export_sim_history_xlsx).pack(side="right")

        self.tree_sim = ttk.Treeview(
            tab_sim,
            columns=("t","sym","side","qty","entry","exit","ticks","pnl","reason"),
            show="headings", height=12
        )
        self.tree_sim.pack(fill="both", expand=True, padx=6, pady=6)
        for k,txt,w,a in (("t","時刻",130,"w"),("sym","銘柄",60,"center"),("side","売買",60,"center"),
                          ("qty","数量",70,"e"),("entry","建値",90,"e"),("exit","決済",90,"e"),
                          ("ticks","tick",60,"e"),("pnl","損益(円)",90,"e"),("reason","理由",180,"w")):
            self.tree_sim.heading(k, text=txt); self.tree_sim.column(k, width=w, anchor=a)

        # そのほかのタブ（資金・注文・ログ等）は必要に応じて増やしてください

    # -------------------------------------------------------------------------
    # 互換レイヤ（V3 の名前で呼ばれても動くように）
    # -------------------------------------------------------------------------
    def _install_v3_compat(self) -> None:
        from types import MethodType
        # 属性 alias（存在しなければ生やす）
        attr_alias = {
            "tree_hist": "tree_sim",
            "lbl_stats": "lbl_stats_sim",
            "lbl_simpos": "lbl_pos_sim",
        }
        for old, new in attr_alias.items():
            if not hasattr(self, old) and hasattr(self, new):
                setattr(self, old, getattr(self, new))

        # メソッド alias（V3名 → V4実装）
        def _bind(fn): return MethodType(fn, self)
        method_alias = {
            "export_sim_history_csv":  _bind(lambda self: self._export_tree_csv(self.tree_sim,  "sim_history.csv")),
            "export_sim_history_xlsx": _bind(lambda self: self._export_tree_xlsx(self.tree_sim, "sim_history.xlsx")),
            "export_live_history_csv": _bind(lambda self: self._export_tree_csv(self.tree_live,  "live_history.csv")),
            "export_live_history_xlsx":_bind(lambda self: self._export_tree_xlsx(self.tree_live, "live_history.xlsx")),
            "_update_sim_stats_from_tree":  _bind(lambda self: self._update_stats_from_tree("SIM")),
            "_update_live_stats_from_tree": _bind(lambda self: self._update_stats_from_tree("LIVE")),
            # 旧 save_* 互換
            "save_live_csv":  _bind(lambda self: self._export_tree_csv(self.tree_live, "live_history.csv")),
            "save_live_xlsx": _bind(lambda self: self._export_tree_xlsx(self.tree_live, "live_history.xlsx")),
            "save_sim_csv":   _bind(lambda self: self._export_tree_csv(self.tree_sim,  "sim_history.csv")),
            "save_sim_xlsx":  _bind(lambda self: self._export_tree_xlsx(self.tree_sim,  "sim_history.xlsx")),
        }
        for old, fn in method_alias.items():
            if not hasattr(self, old):
                setattr(self, old, fn)

    # __getattr__（限定的に：既知の V3 名だけ）
    def __getattr__(self, name):
        tgt = self._V3_ATTR_MAP.get(name)
        if tgt and hasattr(self, tgt):
            return getattr(self, tgt)
        raise AttributeError(name)

    # -------------------------------------------------------------------------
    # 既存関数（名前だけのスタブ）— ここに V3 の中身を移植していきます
    # -------------------------------------------------------------------------
    # UI/表示
    def _open_help(self): pass
    def _update_summary(self): pass
    def _update_price_bar(self): pass
    def _update_simpos(self): pass
    def _update_simpos_summary(self): pass

    # SIM 売買
    def _sim_open(self, side: str): pass
    def _sim_close(self, exit_price: float, reason="MANUAL"): pass
    def _sim_close_market(self, reason="MANUAL", force=False): pass
    def _sim_reverse(self): pass
    def _append_sim_history(self, **row): pass

    # 成績集計・保存
    def _update_stats_from_tree(self, mode: str): pass
    def _export_tree_csv(self, tree, filename: str): pass
    def _export_tree_xlsx(self, tree, filename: str): pass
    def export_live_history_csv(self): pass
    def export_live_history_xlsx(self): pass
    def export_sim_history_csv(self): pass
    def export_sim_history_xlsx(self): pass

    # LIVE 側
    def update_orders(self): pass
    def _fill_orders(self, rows: List[Dict[str, Any]]): pass

    # 学習ログ/ML
    def start_training_log(self): pass
    def stop_training_log(self): pass
    def _log_training_row(self, side_hint: str="", label: int=0, skip_reason: Optional[str]=None): pass

    # スクリーニング
    def start_scan(self): pass
    def stop_scan(self): pass
    def set_main_from_scan_selection(self): pass

    # ネットワーク/価格取得
    def _base_url(self) -> str: return "http://localhost:18080/kabusapi"
    def _snapshot_symbol_once(self): pass

    # ユーティリティ
    def _apply_startup_options(self, args=None): pass
    def _boot_seq(self): pass
    def _log(self, cat: str, msg: str): print(f"[{cat}] {msg}")
    def _log_exc(self, cat: str, e: Exception): print(f"[{cat}] ERR: {e}")
    def ui_call(self, fn, *a, **k):  # UIスレッドに投げるラッパ（V3互換の置き場）
        try: self.after(0, lambda: fn(*a, **k))
        except Exception: pass


# =============================================================================
# main
# =============================================================================
def _parse_cli_args():
    # 省略（必要なら argparse を入れてください）
    class _D: auto_start = False
    return _D()

def main():
    print(__file__)
    args = _parse_cli_args()
    app = App()
    try:
        app._apply_startup_options(args)
    except Exception as e:
        app._log("CFG", f"起動オプション適用エラー: {e}")
    if getattr(args, "auto_start", False):
        import threading
        threading.Thread(target=app._boot_seq, daemon=True).start()
    app.mainloop()

if __name__ == "__main__":
    main()
