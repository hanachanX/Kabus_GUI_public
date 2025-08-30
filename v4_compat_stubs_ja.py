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

# ==== 標準・サードパーティ（V3方針を継承。必要最小限） ====
import os
import sys
import csv
import time
import math
import json
import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests  # HTTP (後で実装)
except Exception:
    requests = None  # スケルトンでは未使用

try:
    import websocket  # websocket-client (後で実装)
except Exception:
    websocket = None

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
        self.real_trade = tk.BooleanVar(value=False)  # ARM（実弾許可）
        self.qty = tk.IntVar(value=100)

        # 成績行用の StringVar
        self.var_simstats = tk.StringVar(value="SIM: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")
        self.var_livestats = tk.StringVar(value="LIVE: Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")

        # サマリー（保有＆株数）
        self.var_live_pos = tk.StringVar(value="—")
        self.var_live_qty = tk.StringVar(value=str(self.qty.get()))
        self.var_sim_pos = tk.StringVar(value="—")
        self.var_sim_qty = tk.StringVar(value=str(self.qty.get()))

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
        ttk.Button(lf_ctrl, text="設定保存", command=self._save_settings_dialog).pack(fill="x")
        ttk.Button(lf_ctrl, text="使い方 / HELP", command=getattr(self, "_open_help", lambda: None)).pack(fill="x", pady=(6, 0))

        # 銘柄コード入力
        lf_sym = ttk.LabelFrame(left, text="メイン銘柄（コード入力）", padding=8, style="Group.TLabelframe")
        lf_sym.pack(fill="x", pady=(8, 8))
        row = ttk.Frame(lf_sym); row.pack(fill="x")
        ttk.Label(row, text="コード:").pack(side="left")
        e = ttk.Entry(row, textvariable=self.symbol_var, width=10)
        e.pack(side="left", padx=(6, 0))
        ttk.Button(row, text="適用", command=self._apply_symbol).pack(side="left", padx=(8, 0))

        # 数量
        lf_qty = ttk.LabelFrame(left, text="数量（デフォルト株数）", padding=8, style="Group.TLabelframe")
        lf_qty.pack(fill="x", pady=(0, 8))
        sp = ttk.Spinbox(lf_qty, from_=100, to=100000, increment=100, width=10, textvariable=self.qty)
        sp.pack(anchor="w")

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
        ttk.Label(hdr_l, text="(銘柄名は後で実装)", style="Hdr.TLabel").pack(anchor="w")
        ttk.Label(hdr_l, text="—", style="SubHdr.TLabel").pack(anchor="w")

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

        # --- 板・歩み値（枠のみ）---
        tab_board = ttk.Frame(tabs); tabs.add(tab_board, text="板・歩み値")
        ttk.Label(tab_board, text="（板・歩み値は後で実装）", style="SubHdr.TLabel").pack(anchor="w", pady=8)

        # --- 資金 ---
        tab_funds = ttk.Frame(tabs); tabs.add(tab_funds, text="資金")
        ttk.Label(tab_funds, text="資金情報（後で実装）", style="SubHdr.TLabel").pack(anchor="w", pady=8)

        # --- 建玉 ---
        tab_pos = ttk.Frame(tabs); tabs.add(tab_pos, text="建玉")
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
        tab_log = ttk.Frame(tabs); tabs.add(tab_log, text="ログ")
        lf_reason = ttk.LabelFrame(tab_log, text="理由（最新）", padding=6, style="Group.TLabelframe")
        lf_reason.pack(fill="x", padx=6, pady=(8, 6))
        ttk.Label(lf_reason, text="（見せ板等の理由ログは後で実装）", style="Small.TLabel", wraplength=1100, justify="left").pack(anchor="w")
        lf_log = ttk.LabelFrame(tab_log, text="ログ", padding=6, style="Group.TLabelframe")
        lf_log.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self.log_box = ttk.Treeview(lf_log, columns=("msg",), show="headings", height=12)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.heading("msg", text="メッセージ"); self.log_box.column("msg", anchor="w")

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

    def _resolve_symbol_name(self, sym: str) -> str:
        """銘柄コード→名称の解決（スケルトンでは未実装。必要に応じてHTTP等で実装）"""
        return ""  # ここは後で実装（V3ではキャッシュ有）

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
    def _apply_symbol(self) -> None:
        code = self.symbol_var.get().strip()
        if not code:
            messagebox.showwarning("銘柄コード", "銘柄コードを入力してください")
            return
        # ここで板/スナップショット再取得等を行う想定
        self._log_ui(f"銘柄を {code} に切替（実装は後で）")

    def _save_settings_dialog(self) -> None:
        messagebox.showinfo("設定", "設定保存は後で実装します。")

    def _save_rm_settings_dialog(self) -> None:
        messagebox.showinfo("資金管理", "資金管理の保存は後で実装します。")

    def _log_ui(self, msg: str) -> None:
        try:
            self.log_box.insert("end", (msg,))
            self.log_box.see("end")
        except Exception:
            pass




"""
V3互換スタブ（日本語コメント付き）
- 目的：V3の関数群をV4へ“空実装”として用意。移植中の参照切れ・クラッシュを防止。
- 使い方：`CompatV3StubsJA` を App の継承に追加します（Appが実装を持っていればApp側が優先）。
- 注意：ここは仕様ドキュメントとしても機能するよう、カテゴリ別に整理し日本語コメントを詳しめに付記。
  実装が完了した関数から順次、App側へ本実装を移し替えてください。
  最終的にはこのスタブを削除してもビルドが通る状態が理想です。
生成日時：2025-08-29 10:10:00
"""

class CompatV3StubsJA:
    """V3の関数名をそのまま提供するためのミックスイン。
    App(CompatV3StubsJA, tk.Tk) のように先頭継承すると、
    Appに実装があるものが常に優先されます（MRO）。
    """
    # ライフサイクル  —  アプリの起動/終了、メインループ、例外処理などの生存期間管理。
    # スレッド/after呼び出しの窓口も含む。    
    def _loop(self, *args, **kwargs):
        """V3の同名機能のスタブ：loop"""
        pass

    def _ws_watchdog_loop(self, *args, **kwargs):
        """V3の同名機能のスタブ：ws watchdog loop"""
        pass

    def _auto_loop(self, *args, **kwargs):
        """V3の同名機能のスタブ：auto loop"""
        pass

    def _on_close(self, *args, **kwargs):
        """V3の同名機能のスタブ：on close"""
        pass

    # HTTP/WS接続  —  Kabu API などへのHTTPアクセス、WebSocket接続・登録、
    # スナップショット取得などの外部I/O入口。 
    def _base_url(self, *args, **kwargs):
        """HTTPアクセス用の基底URL生成"""
        pass

    def _ws_url(self, *args, **kwargs):
        """WebSocket接続用のURL生成"""
        pass

    def _set_ws_state(self, *args, **kwargs):
        """V3の同名機能のスタブ：set ws state"""
        pass

    def _rows_from_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：rows from tree"""
        pass

    def _sim_rows_from_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM rows from tree"""
        pass

    def _get_token(self, *args, **kwargs):
        """V3の同名機能のスタブ：get トークン"""
        pass

    def _register_symbol_safe(self, *args, **kwargs):
        """V3の同名機能のスタブ：登録 銘柄 safe"""
        pass

    def _http_get(self, *args, **kwargs):
        """V3の同名機能のスタブ：http get"""
        pass

    def _snapshot_combo(self, *args, **kwargs):
        """V3の同名機能のスタブ：スナップショット combo"""
        pass

    def _snapshot_board(self, *args, **kwargs):
        """V3の同名機能のスタブ：スナップショット board"""
        pass

    def _snapshot_symbol_once(self, *args, **kwargs):
        """V3の同名機能のスタブ：スナップショット 銘柄 once"""
        pass

    def _connect_ws(self, *args, **kwargs):
        """V3の同名機能のスタブ：接続 ws"""
        pass

    # WS健全性/フォールバック  —  WS断検知、リトライ、
    # HTTPポーリングへのフォールバックなどの可用性確保。    
    def _start_http_fallback(self, *args, **kwargs):
        """V3の同名機能のスタブ：開始 http fallback"""
        pass

    def _stop_http_fallback(self, *args, **kwargs):
        """V3の同名機能のスタブ：停止 http fallback"""
        pass

    # UI更新/描画  —  サマリー/ラベル/テーブル/チャート再描画や、プリセット適用などのUI反映系。   
    def _update_limits_from_symbol(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 値幅制限s from 銘柄"""
        pass

    def _update_special_from_board(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 特別気配 from board"""
        pass

    def ui_after(self, *args, **kwargs):
        """UIスレッドでの遅延実行（Tkのafterラッパー）"""
        pass

    def ui_call(self, *args, **kwargs):
        """UIスレッド実行の安全呼び出しラッパー"""
        pass

    def _define_presets(self, *args, **kwargs):
        """V3の同名機能のスタブ：define プリセットs"""
        pass

    def apply_preset(self, *args, **kwargs):
        """V3の同名機能のスタブ：適用 プリセット"""
        pass

    def _get_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：get tree"""
        pass

    def _get_sim_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：get SIM tree"""
        pass

    def _update_stats_from_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 成績 from tree"""
        pass

    def _update_sim_stats_from_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 SIM 成績 from tree"""
        pass

    def _build_history_panel(self, *args, **kwargs):
        """V3の同名機能のスタブ：build 履歴 panel"""
        pass

    def _build_ui(self, *args, **kwargs):
        """V3の同名機能のスタブ：build ui"""
        pass

    def _layout(self, *args, **kwargs):
        """V3の同名機能のスタブ：layout"""
        pass

    def _build_preset_menu(self, *args, **kwargs):
        """V3の同名機能のスタブ：build プリセット menu"""
        pass

    def _update_sim_labels(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 SIM labels"""
        pass

    def _update_metrics_ui(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 metrics ui"""
        pass

    def _update_sim_stats_label(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 SIM 成績 label"""
        pass

    def _update_simpos(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 SIMポジション"""
        pass

    def _update_price_bar(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 price bar"""
        pass

    def _update_bars_and_indicators(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 bars and indicators"""
        pass

    def _update_summary(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 summary"""
        pass

    def _update_dom_tables(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 dom tables"""
        pass

    def _update_summary_title(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 summary title"""
        pass

    def _draw_chart_if_open(self, *args, **kwargs):
        """V3の同名機能のスタブ：draw チャート if open"""
        pass

    def _update_simpos_summary(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 SIMポジション summary"""
        pass

    def update_wallets(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 wallets"""
        pass

    def update_positions(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 建玉s"""
        pass

    def update_orders(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 注文s"""
        pass

    def update_live_history(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 LIVE 履歴"""
        pass

    def update_preset_names(self, *args, **kwargs):
        """V3の同名機能のスタブ：更新 プリセット 名称s"""
        pass

    def set_main_from_scan_selection(self, *args, **kwargs):
        """V3の同名機能のスタブ：set main from スクリーニング selection"""
        pass

    # SIM  —  SIM取引（建玉/約定/履歴/成績）に関する操作・集計。    
    def export_sim_history_csv(self, *args, **kwargs):
        """V3の同名機能のスタブ：エクスポート SIM 履歴 csv"""
        pass

    def export_sim_history_xlsx(self, *args, **kwargs):
        """V3の同名機能のスタブ：エクスポート SIM 履歴 xlsx"""
        pass

    def _sim_close_market(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM close market"""
        pass

    def _ensure_sim_history(self, *args, **kwargs):
        """V3の同名機能のスタブ：初期化/確保 SIM 履歴"""
        pass

    def _record_sim_trade(self, *args, **kwargs):
        """V3の同名機能のスタブ：record SIM trade"""
        pass

    def _recalc_sim_stats_from_tree(self, *args, **kwargs):
        """V3の同名機能のスタブ：re計算 SIM 成績 from tree"""
        pass

    def _simpos_text(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIMポジション text"""
        pass

    def _append_sim_history(self, *args, **kwargs):
        """V3の同名機能のスタブ：追加 SIM 履歴"""
        pass

    def _sim_open(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM open"""
        pass

    def _sim_on_tick(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM on tick"""
        pass

    def _sim_close(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM close"""
        pass

    def _sim_enter(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM enter"""
        pass

    def _sim_flatten(self, *args, **kwargs):
        """V3の同名機能のスタブ：SIM flatten"""
        pass

    # LIVE  —  実弾（LIVE）側の履歴/更新など。   
    def _append_live_history(self, *args, **kwargs):
        """V3の同名機能のスタブ：追加 LIVE 履歴"""
        pass

    def save_live_csv(self, *args, **kwargs):
        """V3の同名機能のスタブ：保存 LIVE csv"""
        pass

    def save_live_xlsx(self, *args, **kwargs):
        """V3の同名機能のスタブ：保存 LIVE xlsx"""
        pass

    # 注文  —  新規/決済注文の生成、キュー管理、ツールからの反映など。  
    def _wire_send_entry_guard(self, *args, **kwargs):
        """V3の同名機能のスタブ：結線/接続 send entry ガード"""
        pass

    def sweep_orphan_close_orders(self, *args, **kwargs):
        """V3の同名機能のスタブ：sweep orphan close 注文s"""
        pass

    def _order_mode_params(self, *args, **kwargs):
        """V3の同名機能のスタブ：注文 mode params"""
        pass

    def _send_entry_order(self, *args, **kwargs):
        """V3の同名機能のスタブ：send entry 注文"""
        pass

    def _fill_orders(self, *args, **kwargs):
        """V3の同名機能のスタブ：詰め替え/反映 注文s"""
        pass

    # ポジション  —  建玉（ポジション）の取得/反映/評価損益計算。 
    def _fill_positions(self, *args, **kwargs):
        """V3の同名機能のスタブ：詰め替え/反映 建玉s"""
        pass

    # スクリーニング  —  銘柄スキャン、プリセット制御、選択反映などのスクリーニング関連。
    def _show_preset_menu(self, *args, **kwargs):
        """V3の同名機能のスタブ：show プリセット menu"""
        pass

    def _open_preset_tuner(self, *args, **kwargs):
        """V3の同名機能のスタブ：open プリセット tuner"""
        pass

    def start_scan(self, *args, **kwargs):
        """V3の同名機能のスタブ：開始 スクリーニング"""
        pass

    def stop_scan(self, *args, **kwargs):
        """V3の同名機能のスタブ：停止 スクリーニング"""
        pass

    def _fill_scan(self, *args, **kwargs):
        """V3の同名機能のスタブ：詰め替え/反映 スクリーニング"""
        pass

    # ML/特徴量  —  学習ログ、特徴量抽出、ゲート/アウトカム判定などの機械学習パイプライン。 
    def _on_ml_toggle(self, *args, **kwargs):
        """V3の同名機能のスタブ：on ml toggle"""
        pass

    def start_training_log(self, *args, **kwargs):
        """V3の同名機能のスタブ：開始 学習ing log"""
        pass

    def write_training_row(self, *args, **kwargs):
        """V3の同名機能のスタブ：write 学習ing row"""
        pass

    def stop_training_log(self, *args, **kwargs):
        """V3の同名機能のスタブ：停止 学習ing log"""
        pass

    def _log_training_row(self, *args, **kwargs):
        """V3の同名機能のスタブ：log 学習ing row"""
        pass

    # エクスポート/保存  —  CSV/XLSXへの保存、設定/状態の永続化。   
    def export_history_csv(self, *args, **kwargs):
        """V3の同名機能のスタブ：エクスポート 履歴 csv"""
        pass

    def export_history_xlsx(self, *args, **kwargs):
        """V3の同名機能のスタブ：エクスポート 履歴 xlsx"""
        pass

    def save_hist_csv(self, *args, **kwargs):
        """V3の同名機能のスタブ：保存 hist csv"""
        pass

    def save_hist_xlsx(self, *args, **kwargs):
        """V3の同名機能のスタブ：保存 hist xlsx"""
        pass

    # 統計/集計  —  勝率/平均tick/P&Lなどの成績集計、期間切替など。   
    def _ensure_peak_state_vars(self, *args, **kwargs):
        """V3の同名機能のスタブ：初期化/確保 ストップ高安 state vars"""
        pass

    def _reset_symbol_state(self, *args, **kwargs):
        """V3の同名機能のスタブ：reset 銘柄 state"""
        pass

    def _stats_heartbeat(self, *args, **kwargs):
        """V3の同名機能のスタブ：成績 heartbeat"""
        pass

    def toggle_chart_window(self, *args, **kwargs):
        """V3の同名機能のスタブ：toggle チャート window"""
        pass

    # リスク/制御  —  ストップ高安/特別気配/日次損失上限/クールダウンなどのリスクガード。
    def _is_real_trade_armed(self, *args, **kwargs):
        """V3の同名機能のスタブ：is real trade アームed"""
        pass

    def _guard_peak_and_limits(self, *args, **kwargs):
        """V3の同名機能のスタブ：ガード ストップ高安 and 値幅制限s"""
        pass

    def _arm_real_trade_prompt(self, *args, **kwargs):
        """V3の同名機能のスタブ：アーム real trade prompt"""
        pass

    def _disarm_real_trade(self, *args, **kwargs):
        """V3の同名機能のスタブ：disアーム real trade"""
        pass

    def _ensure_real_trade_armed(self, *args, **kwargs):
        """V3の同名機能のスタブ：初期化/確保 real trade アームed"""
        pass

    def arm_after_fill(self, *args, **kwargs):
        """V3の同名機能のスタブ：アーム after 詰め替え/反映"""
        pass

    def _peak_guard(self, *args, **kwargs):
        """V3の同名機能のスタブ：ストップ高安 ガード"""
        pass

    # ヘルパー/その他  —  上記以外の補助関数。名前から推測される用途をコメント。  
    def _pick(self, *args, **kwargs):
        """ヘルパー：条件に応じた要素選択ユーティリティ"""
        pass

    def _log(self, *args, **kwargs):
        """ログ出力（UIログにも反映）"""
        pass

    def _log_exc(self, *args, **kwargs):
        """例外の捕捉とログ出力"""
        pass

    def _to_float(self, *args, **kwargs):
        """V3の同名機能のスタブ：to float"""
        pass

    def _nowstr_full(self, *args, **kwargs):
        """V3の同名機能のスタブ：nowstr full"""
        pass

    def _init_context_menu(self, *args, **kwargs):
        """V3の同名機能のスタブ：init context menu"""
        pass

    def _trace(self, *args, **kwargs):
        """V3の同名機能のスタブ：trace"""
        pass

    def _emit_trace(self, *args, **kwargs):
        """V3の同名機能のスタブ：emit trace"""
        pass

    def _auto_decision_once(self, *args, **kwargs):
        """V3の同名機能のスタブ：auto decision once"""
        pass

    def _sync_auto_cached(self, *args, **kwargs):
        """V3の同名機能のスタブ：sync auto cached"""
        pass

    def _normalize_code(self, *args, **kwargs):
        """V3の同名機能のスタブ：normalize code"""
        pass

    def _codes_match(self, *args, **kwargs):
        """V3の同名機能のスタブ：codes match"""
        pass

    def _derive_book_metrics(self, *args, **kwargs):
        """V3の同名機能のスタブ：派生計算 book metrics"""
        pass

    def _on_debug_toggle(self, *args, **kwargs):
        """V3の同名機能のスタブ：on デバッグ toggle"""
        pass

    def _debug_auto(self, *args, **kwargs):
        """V3の同名機能のスタブ：デバッグ auto"""
        pass

    def _recalc_top_metrics_and_update(self, *args, **kwargs):
        """V3の同名機能のスタブ：re計算 top metrics and 更新"""
        pass

    def _set_summary_title(self, *args, **kwargs):
        """V3の同名機能のスタブ：set summary title"""
        pass

    def _refresh_summary_title(self, *args, **kwargs):
        """V3の同名機能のスタブ：refresh summary title"""
        pass

    def _set_summary_price(self, *args, **kwargs):
        """V3の同名機能のスタブ：set summary price"""
        pass

    def _refresh_summary_price(self, *args, **kwargs):
        """V3の同名機能のスタブ：refresh summary price"""
        pass

    def _push_symbol(self, *args, **kwargs):
        """V3の同名機能のスタブ：push 銘柄"""
        pass

    def _handle_push(self, *args, **kwargs):
        """V3の同名機能のスタブ：handle push"""
        pass

    def _set_best_quote(self, *args, **kwargs):
        """V3の同名機能のスタブ：set best quote"""
        pass

    def _apply_startup_options(self, *args, **kwargs):
        """V3の同名機能のスタブ：適用 開始up options"""
        pass

    def _boot_seq(self, *args, **kwargs):
        """V3の同名機能のスタブ：boot seq"""
        pass

    def _infer_tick_size(self, *args, **kwargs):
        """V3の同名機能のスタブ：infer tick size"""
        pass

    def _sma(self, *args, **kwargs):
        """V3の同名機能のスタブ：sma"""
        pass

    def _ema_series(self, *args, **kwargs):
        """V3の同名機能のスタブ：ema series"""
        pass

    def _macd(self, *args, **kwargs):
        """V3の同名機能のスタブ：macd"""
        pass

    def _rsi(self, *args, **kwargs):
        """V3の同名機能のスタブ：rsi"""
        pass

    def _append_tape(self, *args, **kwargs):
        """V3の同名機能のスタブ：追加 tape"""
        pass

    def _normalize_sym(self, *args, **kwargs):
        """V3の同名機能のスタブ：normalize sym"""
        pass

    def _get_current_symbol(self, *args, **kwargs):
        """V3の同名機能のスタブ：get current 銘柄"""
        pass

    def _get_symbol_name(self, *args, **kwargs):
        """V3の同名機能のスタブ：get 銘柄 名称"""
        pass

    def _append_history(self, *args, **kwargs):
        """V3の同名機能のスタブ：追加 履歴"""
        pass

    def _wire_history_scrollbar(self, *args, **kwargs):
        """V3の同名機能のスタブ：結線/接続 履歴 scrollbar"""
        pass

    def place_server_bracket(self, *args, **kwargs):
        """V3の同名機能のスタブ：place server bracket"""
        pass

    def self_check(self, *args, **kwargs):
        """V3の同名機能のスタブ：self check"""
        pass

    def toggle_auto(self, *args, **kwargs):
        """V3の同名機能のスタブ：toggle auto"""
        pass

    def _recent_momentum(self, *args, **kwargs):
        """V3の同名機能のスタブ：recent momentum"""
        pass

    def _microprice(self, *args, **kwargs):
        """V3の同名機能のスタブ：microprice"""
        pass

    def _filters_ok(self, *args, **kwargs):
        """V3の同名機能のスタブ：filters ok"""
        pass

    def reset_sim(self, *args, **kwargs):
        """V3の同名機能のスタブ：reset SIM"""
        pass

    def _round_tick(self, *args, **kwargs):
        """V3の同名機能のスタブ：round tick"""
        pass

    def refresh_hist_table(self, *args, **kwargs):
        """V3の同名機能のスタブ：refresh hist table"""
        pass

    def _clamp_price_for_side(self, *args, **kwargs):
        """V3の同名機能のスタブ：clamp price for side"""
        pass

    def _open_help(self, *args, **kwargs):
        """V3の同名機能のスタブ：open ヘルプ"""
        pass

    def _fmt_ticks(self, *args, **kwargs):
        """V3の同名機能のスタブ：整形 ticks"""
        pass

    def _help_text_ja(self, *args, **kwargs):
        """V3の同名機能のスタブ：ヘルプ text ja"""
        pass

    def _help_text_en(self, *args, **kwargs):
        """V3の同名機能のスタブ：ヘルプ text en"""
        pass

    def _resolve_symbol_name(self, *args, **kwargs):
        """V3の同名機能のスタブ：解決 銘柄 名称"""
        pass


# ======================================================
# エントリポイント
# ======================================================

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()


