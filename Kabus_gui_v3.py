# -*- coding: utf-8 -*-
"""
Kabus_gui_v2_safe.py
- Windows 11 / Python 3.13
- 1ファイル完結。requests / websocket-client / tkinter / matplotlib 依存のみ。

変更履歴（2025-08-27 JST）
- UIは現状維持（タブ/ボタン/配置/見た目）。ログ欄だけ横幅拡張・水平スクロール追加（wrap='none'）。
- Tk操作はメインスレッド限定：ui_call()/ui_after()を導入し、UI更新は必ず after(0) 経由。
- 初期化順序の遵守：①トークン → ②銘柄登録（他銘柄をunregister） → ③WS接続 → ④スナップショット（HTTPのみ）→ ⑤UI反映。
- /register後、RegistListの“他銘柄”は/unregisterで一括解除。ログに「UNREGISTER … -> …」を出力。
- WS銘柄不一致PUSHが20連続で発生したら自動で再登録を投げ直し。
- メインループの無制限ドレイン禁止：1ティック最大200件処理→after(100)で次回。
- チャート再描画のレート制限：最短200ms間隔で描画（描画要求が密な場合はスキップ）。
- MLモデル呼び出しの引数修正：load_latest_model(models_dir, symbol) を位置引数で実行（unexpected keyword 'symbol' を解消）。
- 例外は「要旨+末尾スタック1行」をログ出力。GUIは止めない。
- ログ改善： [HH:MM:SS] + タグ([WS][HTTP][ML][LOOP][SIM][AUTO]等) + 5秒同系抑制 + 大量JSONは先頭200文字のみ。
- 既存のAUTO/SIM/板/歩み値/サマリー/チャート/資金/建玉/注文/履歴/スクリーニングのUIを維持。

補足：元のKabus_gui_v2.pyをベースに内部実装を安全化しました。
"""

from __future__ import annotations
import os, sys, json, math, time, queue, threading, datetime as dt, traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ==== サードパーティ ====
import requests
import websocket  # websocket-client
import socket


# ==== Tk / Matplotlib ====
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText
import tkinter.font as tkfont

import matplotlib
matplotlib.use("Agg")    # Tk埋め込みは FigureCanvasTkAgg を使用
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates

# ==== ML（存在しない場合は無効化） ====
_ML_AVAILABLE = True
try:
    # 利用中のユーザー環境では `scalper` が存在する前提だが、保険としてtry/except
    import scalper
    from scalper.strategy.ml_gate import MLGate, MLGateConfig
    from scalper.strategy.ml_loader import load_latest_model
    from scalper.ml.features import compute_features, FEATURE_COLUMNS
    from scalper.core.types import OrderIntent
except Exception:
    _ML_AVAILABLE = False
    # ダミーを定義してGUIは起動可能にする
    class MLGateConfig:
        def __init__(self, enabled=False, min_prob=0.6, min_ev_ticks=0.1, cost_ticks=0.1):
            self.enabled = enabled
            self.min_prob = min_prob
            self.min_ev_ticks = min_ev_ticks
            self.cost_ticks = cost_ticks
    class _DummyDec: 
        def __init__(self, go=False, reason="ml-disabled"): self.go, self.reason = go, reason
    class MLGate:
        def __init__(self, cfg): self.cfg, self._external_proba = cfg, None
        def evaluate(self, intent, feats): return _DummyDec(False, "ml-disabled")
    def load_latest_model(*args, **kwargs): raise RuntimeError("ml-disabled")
    FEATURE_COLUMNS = []
    def compute_features(**kwargs): return {"ts": int(time.time()), "symbol": kwargs.get("symbol","—")}
    class OrderIntent:
        def __init__(self, tp_ticks, sl_ticks): self.tp_ticks, self.sl_ticks = tp_ticks, sl_ticks


# ==== 定数 ====
APP_TITLE = "kabuS – Board/Tape + Chart + Tabs + Filters + Screener (safe)"
EXCHANGE = 1
SECURITY_TYPE = 1
DEFAULT_TICK_SIZE = 0.5


IMBALANCE_TH = 0.45        # 0.60 → 0.45（板偏りの閾値を下げてシグナル増）
SPREAD_TICKS_MAX = 2       # 1 → 2（2tickスプレッドまで許容）
SIGNAL_COOLDOWN_S = 0.8    # 2.0 → 0.8（クールダウン短縮）
PUSH_FRESH_SEC = 0.8       # 1.0 → 0.8（“直近PUSHが新鮮”の判定も少し緩め）
MOM_WINDOW_S = 0.35        # ★追加：直近モメンタムを測る窓（秒）

'''
IMBALANCE_TH = 0.60
SPREAD_TICKS_MAX = 1
SIGNAL_COOLDOWN_S = 2.0
PUSH_FRESH_SEC = 1.0
'''

# --- WS health policy & HTTP fallback ---
WS_SILENT_RECONNECT_SEC = 30    # 無通信が続いたら再接続（既に導入済みならそのまま）
WS_HEALTH_OK_SEC = 5            # 直近PUSHがこれ以内なら HEALTHY
HTTP_FALLBACK_PERIOD_S = 3      # WS劣化中のHTTPポーリング周期（秒）
AUTO_BLOCK_WHEN_WS_DEGRADED = True  # 劣化中はAUTOの新規エントリを止める

# --- WS grace & recover policy ---
WS_FIRST_PUSH_GRACE_SEC = 8      # 接続直後、最初のPUSHを待つ猶予
WS_REJOIN_NO_PUSH_SEC    = 16     # 無通信がこの秒数続いたら /register を再投
#WS_SILENT_RECONNECT_SEC  = 30     # さらに続いたら WS を閉じて再接続（従来15→30に緩和）
WS_RECONNECT_BACKOFF_MAX = 30.0   # 再接続バックオフ上限（既にあればそのまま）

CHART_LOOKBACK_MIN = 180  # 分
ALLOWED_TICKS = [0.1, 0.5, 1.0, 5.0, 10.0, 50.0, 100.0, 1000.0]
PRESET_CODES = ["7203","8306","8411","8316","9432","9433","6758","7267","6501","6981","141A","285A","8136"]

# ==============================
# アプリ本体
# ==============================
class App(tk.Tk):

    # ---------- PEAK(特別気配)/上下限の取得＆発注前ガード ここから ----------
    def _pick(self, obj, keys, default=None):
        """入れ子dictにも耐える値取得: keysは 'A.B' のようなパスもOK"""
        def get_path(d, path):
            cur = d
            for part in path.split('.'):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    return None
            return cur
        for k in keys:
            if obj is None:
                continue
            v = None
            if isinstance(k, str) and '.' in k:
                v = get_path(obj, k)
            elif isinstance(obj, dict):
                v = obj.get(k, None)
            if v is not None:
                return v
        return default

    def _ensure_peak_state_vars(self):
        """未初期化なら初期化"""
        if not hasattr(self, 'upper_limit'):
            self.upper_limit = None
        if not hasattr(self, 'lower_limit'):
            self.lower_limit = None
        if not hasattr(self, 'special_quote'):
            self.special_quote = None

    def _update_limits_from_symbol(self, sym_json: dict):
        """/symbol 応答から日々の上下限を更新（名称差に耐える）"""
        self._ensure_peak_state_vars()
        up = self._pick(sym_json, [
            'DailyUpperLimit','PriceLimitUpper','UpperLimitPrice',
            'DailyPriceLimitUpper','LimitHigh','HighLimit'
        ])
        lo = self._pick(sym_json, [
            'DailyLowerLimit','PriceLimitLower','LowerLimitPrice',
            'DailyPriceLimitLower','LimitLow','LowLimit'
        ])
        try:
            self.upper_limit = float(up) if up not in (None,'') else None
        except Exception:
            self.upper_limit = None
        try:
            self.lower_limit = float(lo) if lo not in (None,'') else None
        except Exception:
            self.lower_limit = None
        try:
            self._log('DER', f'limits up={self.upper_limit} lo={self.lower_limit} spq={self.special_quote}')
        except Exception:
            pass

    def _update_special_from_board(self, board_json: dict):
        """/board(板) から特別気配を更新。公式フラグ優先、無ければ推定"""
        self._ensure_peak_state_vars()
        sp = self._pick(board_json, [
            'SpecialQuote','SpecialSell','SpecialBuy','SpecialFlag','SpecialQuotation'
        ])
        if sp is None:
            bid = self._pick(board_json, ['BidPrice','BestBid','Buy1.Price','Bid','Bids.0.Price'])
            ask = self._pick(board_json, ['AskPrice','BestAsk','Sell1.Price','Ask','Asks.0.Price'])
            try:
                over = float(self._pick(board_json, ['OverSellQty','OverSellQuantity'], 0) or 0)
            except Exception:
                over = 0.0
            try:
                under = float(self._pick(board_json, ['UnderBuyQty','UnderBuyQuantity'], 0) or 0)
            except Exception:
                under = 0.0
            sp = (bid is None or ask is None) or (over > 0 or under > 0)
        # SIM中は旧来のPEAKチェックに引っかからないようにする（実弾のみ有効）
        self.special_quote = bool(sp) if self._is_real_trade_armed() else False
        try:
            self._log('DER', f'limits up={self.upper_limit} lo={self.lower_limit} spq={self.special_quote}')
        except Exception:
            pass

    def _is_real_trade_armed(self) -> bool:
        """実弾ONかつARMEDかを安全に判定。既存の _ensure_real_trade_armed() があればそれを使う。"""
        try:
            if hasattr(self, '_ensure_real_trade_armed'):
                return bool(self._ensure_real_trade_armed())
        except Exception:
            pass
        try:
            rt = getattr(self, 'real_trade', None)
            if rt is None:
                return False
            armed = getattr(self, 'real_trade_armed', None)
            if armed is not None:
                return bool(armed)
            if hasattr(rt, 'get'):
                return bool(rt.get())
            return bool(rt)
        except Exception:
            return False

    def _guard_peak_and_limits(self) -> bool:
        """発注前ガード（True=続行可 / False=ブロック）SIMは常に通す、実弾のみブロック"""
        if not self._is_real_trade_armed():
            return True
        lp = getattr(self, 'last_price', None)
        if self.special_quote is True:
            try: self._log('ORD', 'block: SPECIAL QUOTE (PEAK)')
            except Exception: pass
            return False
        if lp is not None and self.upper_limit is not None and lp >= self.upper_limit:
            try: self._log('ORD', 'block: at UPPER LIMIT')
            except Exception: pass
            return False
        if lp is not None and self.lower_limit is not None and lp <= self.lower_limit:
            try: self._log('ORD', 'block: at LOWER LIMIT')
            except Exception: pass
            return False
        return True

    def _wire_send_entry_guard(self):
        """_send_entry_order を一度だけラップし、実弾時にだけブロックを有効化する"""
        if getattr(self, '__send_entry_guard_installed__', False):
            return
        if not hasattr(self, '_send_entry_order'):
            try: self._log('ERR', 'no _send_entry_order; call _guard_peak_and_limits() manually before real orders.')
            except Exception: pass
            return
        orig = self._send_entry_order
        def wrapped_send_entry_order(*args, **kwargs):
            if not self._guard_peak_and_limits():
                return None
            return orig(*args, **kwargs)
        self._send_entry_order = wrapped_send_entry_order
        self.__send_entry_guard_installed__ = True
        try: self._log('SYS', 'send-entry guard installed')
        except Exception: pass
    # ---------- PEAK(特別気配)/上下限の取得＆発注前ガード ここまで ----------


    # --------- 初期化 ---------
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        # self.geometry("1660x1000")  # 既存のUI前提サイズ
        # App.__init__ の self.geometry("1660x1000") を削除し、以下に置き換え
        #self.minsize(1200, 720)                 # 小さすぎ防止の最小サイズ
        #self.after(0, lambda: self.state("zoomed"))  # 起動時に最大化
        self.minsize(1200, 720)
        self.after(0, lambda: self.state("zoomed"))

        self.ws_state = "DISCONNECTED"   # "HEALTHY" / "DEGRADED" / "CONNECTED" / "DISCONNECTED"
        self._fallback_job = None
        self._auto_ws_pause_ts = 0.0

        self.sim_pos = None   # 例: {"side":"BUY","qty":100,"entry":1234.5,"ts":..., "symbol":"8136@1", "row_id":"tree item id"}

        self.tick_size   = 0.5
        self.best_bid    = None
        self.best_ask    = None
        self.best_bidq   = 0
        self.best_askq   = 0
        self.last_price  = None
        self.spread      = None
        self.imbalance   = None
        self.momentum    = 0.0
        self.ws_state    = "DISCONNECTED"
        self.imb_threshold = getattr(self, "imb_threshold", 0.35)
        self.cooldown_ms   = getattr(self, "cooldown_ms", 400)

        # 監視銘柄の初期値（例：7203@1）
        self.symbol = tk.StringVar(value="7203")

        self._auto_on_cached = False

        # 主要UIバインディング済みの状態変数
        self.auto_enabled = tk.BooleanVar(value=False)
        self.is_production = tk.BooleanVar(value=True)    # :18080
        self.real_trade   = tk.BooleanVar(value=False)    # 実弾スイッチ
        self.api_password = tk.StringVar()
        self.account_type = tk.StringVar(value="特定(4)")
        self.qty          = tk.IntVar(value=100)
        self.trade_mode   = tk.StringVar(value="信用(一般・デイトレ)")

        self.real_trade_armed = False  # ← 既定は“武装なし”=SIMのみ
        # === AUTO: Tk → 安全キャッシュ ===
        self._auto_on_cached = False
        def _sync_auto_cached(*_):
            try:
                self._auto_on_cached = bool(self.auto_enabled.get())
            except Exception:
                self._auto_on_cached = False
        self.auto_enabled.trace_add("write", _sync_auto_cached)
        _sync_auto_cached()  # 初期値反映

        # === ML 有効フラグも同様に（あれば） ===
        if not hasattr(self, "ml_enabled"):
            self.ml_enabled = tk.BooleanVar(value=False)
        self._ml_enabled_cached = bool(self.ml_enabled.get())
        self.ml_enabled.trace_add("write",
            lambda *_: setattr(self, "_ml_enabled_cached", bool(self.ml_enabled.get()))
        )

        # === debug フラグも同様に（あれば） ===
        if not hasattr(self, "debug_mode"):
            self.debug_mode = tk.BooleanVar(value=False)
        self._debug_cached = bool(self.debug_mode.get())
        self.debug_mode.trace_add("write",
            lambda *_: setattr(self, "_debug_cached", bool(self.debug_mode.get()))
        )

        # === 数量 qty も Tk を読まずに済むようキャッシュ ===
        self._qty_cached = int(self.qty.get() or 0)
        self.qty.trace_add("write",
            lambda *_: setattr(self, "_qty_cached", int(self.qty.get() or 0)))

        # Auto-wired: guard PEAK/limits for real trades only
        self._wire_send_entry_guard()


        # ホットキー：ARM/Disarm
        self.bind("<Control-Shift-R>", lambda e: self._arm_real_trade_prompt())
        self.bind("<F12>",             lambda e: self._disarm_real_trade())

        # 手動SIMエントリー（既に入れていればそのまま）
        self.bind("<Control-b>", lambda e: (self._sim_open("BUY"),  self._log("SIM", "manual BUY")))
        self.bind("<Control-s>", lambda e: (self._sim_open("SELL"), self._log("SIM", "manual SELL")))

        # ★ 手動決済／反転（今回追加）
        self.bind("<Control-e>",      lambda e: self._sim_close_market("MANUAL"))
        self.bind("<Control-Shift-e>",lambda e: self._sim_reverse())

        # デバッグ
        self.debug_mode = tk.BooleanVar(value=False)
        self._trace_buf = []
        self._last_trace_log_ts = 0.0
        self.debug_mode.trace_add("write", lambda *a: self._on_debug_toggle(source="var"))


        # 戦略
        self.tp_ticks     = tk.IntVar(value=3)
        self.sl_ticks     = tk.IntVar(value=2)
        self.use_trail    = tk.BooleanVar(value=True)
        self.trail_trigger= tk.IntVar(value=2)
        self.trail_gap    = tk.IntVar(value=1)

        # 補助フィルタ
        self.f_vwap  = tk.BooleanVar(value=True)
        self.f_sma25 = tk.BooleanVar(value=False)
        self.f_macd  = tk.BooleanVar(value=True)
        self.f_rsi   = tk.BooleanVar(value=False)
        self.f_swing = tk.BooleanVar(value=False)

        # 資金表示
        self.cash_stock_wallet = tk.StringVar(value="—")
        self.cash_bank         = tk.StringVar(value="—")
        self.margin_wallet     = tk.StringVar(value="—")
        self.margin_rate       = tk.StringVar(value="—")

        # WS/REST/状態
        self.token: Optional[str] = None
        self.ws = None
        self.ws_thread = None
        self.ws_connecting = False
        self.ws_should_reconnect = True
        self.ws_backoff_sec = 2
        self.msg_q: "queue.Queue[str]" = queue.Queue()
        self.push_count = 0
        self.last_push_ts = 0.0
        self._mismatch_count = 0

        # ティック・板・インジ状態
        self.tick_size = DEFAULT_TICK_SIZE
        self.best_bid=self.best_ask=self.bid_qty=self.ask_qty=None
        self.last_price=None; self.prev_close=None; self.last_vol=None; self.last_dir=None
        self.asks: List[Tuple[float,float]] = []
        self.bids: List[Tuple[float,float]] = []
        self.tick_hist = deque(maxlen=400)           # (ts, price)
        self.day_cum_vol=0.0; self.day_cum_turnover=0.0; self.vwap=None
        self.vwap_hist=deque(maxlen=48)
        self.bars: List[List[Any]] = []              # [time,O,L,H,C]
        self.sma25=self.sma25_prev=None
        self.macd=self.macd_sig=None
        self.rsi=None
        self.swing_higher_lows=self.swing_lower_highs=None

        # チャート
        self.chart_win=None; self.ax=None; self.canvas=None
        self._last_draw_ts = 0.0

        # ML・スクリーン
        self.ml_gate = MLGate(MLGateConfig(enabled=False, min_prob=0.60, min_ev_ticks=0.10, cost_ticks=0.10))
        self.ml_enabled = tk.BooleanVar(value=self.ml_gate.cfg.enabled)
        self.push_times = deque(maxlen=1200)  # 直近PUSH時刻（/min）
        self.scan_running = False; self.scan_thread=None; self.scan_states={}
        self.s_thr_tickrate = tk.DoubleVar(value=0.80)
        self.s_thr_updates  = tk.IntVar(value=40)
        self.s_thr_imbstd   = tk.DoubleVar(value=0.15)
        self.s_thr_revrate  = tk.DoubleVar(value=0.40)

        # SIM/LIVE
        self.auto_on = tk.BooleanVar(value=False)
        self.last_signal_ts=0.0
        self.pos=None
        self.sim_stats={'trades':0,'wins':0,'losses':0,'ticks_sum':0.0,'pnl_yen':0.0}
        self.sim_trades: List[List[Any]] = []
        self.live_rows: List[List[Any]] = []

        # 銘柄名キャッシュ
        self.symbol_name_cache: Dict[str,str] = {}

        # 学習ログ
        self.train_writer=None; self.train_csv_path=None; self.train_f=None

        # ログ抑制
        self._log_memo: Dict[str,float] = {}

        # ===== UI構築 =====
        self._build_ui()
        self._layout()

        # ===== MLモデル読み込み（位置引数版）=====
        if _ML_AVAILABLE:
            models_dir = Path(scalper.__file__).resolve().parent / "ml" / "models"
            try:
                proba_fn, feats, path = load_latest_model(str(models_dir), self.symbol.get().strip())
                self.ml_gate._external_proba = proba_fn
                self._log("ML", f"model loaded: {os.path.basename(path)}")
            except Exception as e:
                self._log("ML", f"no model yet ({e})")

        # メインループ起動
        self.after(100, self._loop)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("LOOP", "準備OK：①トークン → ②銘柄登録 → ③WS接続。まずはSIMで検証してください。")


        # 主要UIバインディングの直後あたり（__init__ 内）
        self.upper_limit = None     # 値幅上限（ストップ高）
        self.lower_limit = None     # 値幅下限（ストップ安）
        self.special_quote = False  # 特別気配フラグ（任意）


        # デバッグ切替（UI変更なし）
        self.auto_debug = False
        self.bind("<Control-d>", lambda e: (setattr(self, "auto_debug", not self.auto_debug),
                                    self._log("AUTO", f"debug={'ON' if self.auto_debug else 'OFF'}")))

        #強制決済（SIM）
        self.bind("<Control-Alt-e>", lambda e: self._sim_close_market("FORCE", force=True))

        # __init__ の末尾などに
        self._orphan_job = None
        def _orphan_tick():
            try:
                self.sweep_orphan_close_orders()
            except Exception as e:
                self._log_exc("AUTO", e)
            finally:
                self._orphan_job = self.after(60_000, _orphan_tick)  # 60秒ごと
        self._orphan_job = self.after(60_000, _orphan_tick)


    # --------- 汎用（UIスレッド呼び出し） ---------
    def ui_after(self, delay_ms:int, fn, *args, **kwargs):
        """Tkメインスレッドでの遅延実行"""
        try:
            self.after(delay_ms, lambda: fn(*args, **kwargs))
        except Exception as e:
            self._log("UI", f"after err: {e}")

    def ui_call(self, fn, *args, **kwargs):
        """Tkメインスレッドでの即時実行（after(0)）"""
        self.ui_after(0, fn, *args, **kwargs)

    # ------ プリセット ------
    def _define_presets(self):
        # 既定値（足りない場合に備え初期化）
        if not hasattr(self, "imb_threshold"): self.imb_threshold = 0.35
        if not hasattr(self, "cooldown_ms"):  self.cooldown_ms  = 400
        if not hasattr(self, "max_spread_ticks"): self.max_spread_ticks = 2
        if not hasattr(self, "size_cap_ratio"):   self.size_cap_ratio   = 0.50

        # プリセット（必要に応じて数値は調整可）
        self._presets = {
            "標準":  {"imb":0.35, "cd":400, "tp":3, "sl":2, "spread":2, "size_ratio":0.50},
            "高ボラ":{"imb":0.45, "cd":700, "tp":4, "sl":3, "spread":2, "size_ratio":0.30},
            "低ボラ":{"imb":0.28, "cd":400, "tp":2, "sl":2, "spread":1, "size_ratio":0.70},
        }

    def apply_preset(self, name: str):
        p = self._presets.get(name)
        if not p:
            self._log("CFG", f"未知のプリセット: {name}"); return
        self.imb_threshold     = float(p["imb"])
        self.cooldown_ms       = int(p["cd"])
        self.max_spread_ticks  = int(p["spread"])
        self.size_cap_ratio    = float(p["size_ratio"])
        # tk 変数に反映
        try:
            self.tp_ticks.set(int(p["tp"]))
            self.sl_ticks.set(int(p["sl"]))
        except Exception:
            pass
        self._log("CFG", f"プリセット適用: {name} (imb={self.imb_threshold}, cd={self.cooldown_ms}ms, "
                        f"tp={int(p['tp'])}, sl={int(p['sl'])}, spread≤{self.max_spread_ticks}t, "
                        f"size≤{int(self.size_cap_ratio*100)}%)")

    # --------- ログ ---------
    def _log(self, tag:str, msg:str, *, dedup_key:Optional[str]=None):
        """[HH:MM:SS] [TAG] message。5秒以内の同系ログは抑制。"""
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{tag}] {msg}"
        key = dedup_key or f"{tag}:{msg}"
        now = time.time()
        last = self._log_memo.get(key, 0.0)

        '''# ---- trace SIM err origin (temporary) ----
        import inspect, sys, threading
        try:
            if tag == "SIM" and isinstance(msg, str) and str(msg).strip().lower().startswith("err:"):
                fr = inspect.stack()[1]
                trace_line = f"[TRACE] log caller: {fr.filename}:{fr.lineno} in {fr.function}"
                # UIに出す
                def _trace_do():
                    try:
                        self.log_box.insert("end", trace_line + "\n")
                        self.log_box.see("end")
                    except Exception:
                        print(trace_line, file=sys.stderr)
                if threading.current_thread() is threading.main_thread():
                    _trace_do()
                else:
                    self.ui_call(_trace_do)
        except Exception as e:
            print(f"[TRACE-error] {e}", file=sys.stderr)
        '''


        if now - last < 5.0:
            return
        self._log_memo[key] = now

        def _do():
            try:
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
            except Exception:
                print(line, file=sys.stderr)

        # メインスレッドへ
        if threading.current_thread() is threading.main_thread():
            _do()
        else:
            self.ui_call(_do)

    def _log_exc(self, tag:str, e:Exception):
        tb = traceback.format_exc().strip().splitlines()
        last = tb[-1] if tb else repr(e)
        self._log(tag, f"{e}"); self._log(tag, last)

    # --------- HTTP/WS URL ---------
    def _base_url(self) -> str:
        return f"http://localhost:{18080 if self.is_production.get() else 18081}/kabusapi"
    def _ws_url(self) -> str:
        return f"ws://localhost:{18080 if self.is_production.get() else 18081}/kabusapi/websocket"

        # --------- 銘柄切り替え時の状態リセット ---------
    def _reset_symbol_state(self):
        """主銘柄を切り替えた直後の内部状態初期化（UIはメインスレッドから呼ばれる想定）"""
        # 価格/板/出来高まわり
        self.best_bid = self.best_ask = self.bid_qty = self.ask_qty = None
        self.last_price = None
        self.last_vol = None
        # 前日終値はスナップショットで上書きされるので一旦 None
        self.prev_close = None

        # 板と履歴
        self.asks = []
        self.bids = []
        self.tick_hist.clear()
        self.vwap = None
        self.day_cum_vol = 0.0
        self.day_cum_turnover = 0.0
        self.vwap_hist.clear()

        # チャート（5分足）
        self.bars.clear()
        self.sma25 = self.sma25_prev = None
        self.macd = self.macd_sig = None
        self.rsi = None
        self.swing_higher_lows = self.swing_lower_highs = None
        self._last_draw_ts = 0.0

        # PUSH関連
        self.push_count = 0
        self.last_push_ts = 0.0
        self._mismatch_count = 0

        # UI反映
        self._update_dom_tables()
        self._update_bars_and_indicators()
        self._update_summary()
        self._draw_chart_if_open(force=True)

    def _set_ws_state(self, state: str, reason: str | None = None):
        """WS状態管理（UIは既存ラベルに追加表示）"""
        if state == self.ws_state:
            return
        self.ws_state = state
        msg = f"state={state}" + (f" ({reason})" if reason else "")
        self._log("WS", msg)
        # pushes= の表示を拡張（UI変更はこのラベル文言だけ）
        try:
            self.ui_call(self.lbl_misc.config, text=f"pushes={self.push_count} | WS:{state}")
        except Exception:
            pass
        if state == "DEGRADED":
            self._start_http_fallback()
        elif state == "HEALTHY":
            self._stop_http_fallback()

    def _start_http_fallback(self):
        if self._fallback_job is not None:
            return
        self._log("WS", "HTTP fallback start")
        def _tick():
            try:
                # スナップショットはUIを触らない実装：HTTP→状態更新→必要箇所を ui_call で反映
                self._snapshot_symbol_once()
            except Exception as e:
                self._log_exc("HTTP", e)
            finally:
                self._fallback_job = self.after(int(HTTP_FALLBACK_PERIOD_S * 1000), _tick)
        self._fallback_job = self.after(200, _tick)

    def _stop_http_fallback(self):
        if self._fallback_job is not None:
            try:
                self.after_cancel(self._fallback_job)
            except Exception:
                pass
            self._fallback_job = None
            self._log("WS", "HTTP fallback stop")


    def _arm_real_trade_prompt(self):
    #実発注を“セッション限定”で有効化する最終確認（Ctrl+Shift+R）
        if not self.real_trade.get():
            self._log("ORD", "実発注ONがOFFのためARMできません。まずチェックをONにしてください。")
            return
        if self.real_trade_armed:
            self._log("ORD", "実発注は既に ARMED（F12で解除可）")
            return
        # 環境変数で明示許可されていれば確認省略
        if os.getenv("KABUS_ALLOW_REAL", "0") == "1":
            self.real_trade_armed = True
            self._log("ORD", "REAL TRADE ARMED (env:KABUS_ALLOW_REAL=1)")
            return
        # 最終確認
        ans = messagebox.askyesno(
            "実発注の最終確認",
            "本当に“実発注”を有効化しますか？\n"
            "・このセッションでは確認を省略します（F12で解除）\n"
            "・誤操作防止のため、通常はSIMのみで運用してください。"
        )
        if ans:
            self.real_trade_armed = True
            self._log("ORD", "REAL TRADE ARMED（F12で解除可）")
        else:
            self._log("ORD", "キャンセル：SIMのみ動作を継続")

    def _sim_close_market(self, reason="MANUAL", force=False):
        """SIMポジションを成行相当でクローズ。
        価格が取れない場合は HTTP スナップショットにフォールバック。
        force=True のときは最終手段として “建値” で強制クローズ。
        """
        p = getattr(self, "pos", None)
        if not p:
            self._log("SIM", "close: no position")
            return

        side = p.get("side")
        px = None

        # 1) 通常の決済価格（BUY→Bid / SELL→Ask → last）
        try:
            px = (self.best_bid if side == "BUY" else self.best_ask) or self.last_price
        except Exception:
            px = None

        # 2) 取れなければスナップショットで更新
        if px is None:
            try:
                self._snapshot_symbol_once()  # UIは触らない実装
                px = (self.best_bid if side == "BUY" else self.best_ask) or self.last_price
            except Exception:
                px = None

        # 3) 強制クローズ（建値）オプション
        if px is None and force:
            px = p.get("entry")

        if px is None:
            self._log("SIM", "close skipped: price unavailable（引け後・無更新の可能性）")
            # 画面はポジションが残る（未決済）ので表示は維持
            return

        try:
            self._sim_close(float(px), reason=reason)
        except Exception as e:
            self._log_exc("SIM", e)
        # 表示更新（非同期でUIスレッドへ）
        self.ui_call(self._update_simpos)


        def _sim_reverse(self):
            """現在のSIMポジションを即反転（決済→反対側で即エントリー）"""
            if self.pos is None:
                self._log("SIM", "reverse: no position")
                return
            cur_side = self.pos["side"]
            self._sim_close_market("REVERSE")
            opp = "SELL" if cur_side == "BUY" else "BUY"
            self._sim_open(opp)
            self._log("SIM", f"reverse -> {opp}")



    def _disarm_real_trade(self):
        """即時 DISARM（F12）"""
        self.real_trade_armed = False
        try:
            # ついでに見た目もOFFに戻す（任意。外したい場合は次行コメントアウト）
            self.real_trade.set(False)
        except Exception:
            pass
        self._log("ORD", "DISARMED：実発注は無効（SIMのみ）")

    def _ensure_real_trade_armed(self) -> bool:
        """実注前の最終ゲート。ARMされていなければ False を返す。"""
        if not self.real_trade.get():
            return False
        if os.getenv("KABUS_ALLOW_REAL", "0") == "1":
            self.real_trade_armed = True
            return True
        return bool(self.real_trade_armed)


    def _init_context_menu(self):
        import tkinter as tk

        # 既存があれば作り直し
        if hasattr(self, "_preset_menu") and self._preset_menu:
            try: self._preset_menu.destroy()
            except Exception: pass

        self._preset_menu = tk.Menu(self, tearoff=False)

        # プリセット
        self._preset_menu.add_command(label="標準 (Ctrl+Shift+1)", command=lambda: self.apply_preset("標準"))
        self._preset_menu.add_command(label="高ボラ (Ctrl+Shift+2)", command=lambda: self.apply_preset("高ボラ"))
        self._preset_menu.add_command(label="低ボラ (Ctrl+Shift+3)", command=lambda: self.apply_preset("低ボラ"))
        self._preset_menu.add_separator()

        # デバッグ（チェック状態は self.debug_mode に直結）
        if not hasattr(self, "debug_mode"):
            self.debug_mode = tk.BooleanVar(value=False)
        self._preset_menu.add_checkbutton(
            label="デバッグ（意思決定ログ）",
            onvalue=True, offvalue=False,
            variable=self.debug_mode,
            command=lambda: self._log("CFG", f"debug={'ON' if self.debug_mode.get() else 'OFF'}")
        )

        # パラメータ調整ウィンドウ
        self._preset_menu.add_command(label="調整… (Ctrl+Shift+T)", command=self._open_preset_tuner)

        # メニュー表示ハンドラ
        def _popup(event):
            try:
                self._preset_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._preset_menu.grab_release()

        # 右クリックを“アプリ全域”で拾う（CanvasやTreeviewの上でも出るように add="+"）
        targets = [
            self,
            getattr(self, "container", None),
            getattr(self, "canvas_outer", None),
            getattr(self, "root", None),
            getattr(self, "left", None),
            getattr(self, "summary", None),
            getattr(self, "main_nb", None),
        ]
        for w in targets:
            if w:
                w.bind("<Button-3>", _popup, add="+")     # 右クリック
                w.bind("<Shift-F10>", _popup, add="+")    # コンテキストキー代替

        # ホットキー（見えなくても操作できる）
        self.bind_all("<Control-Shift-1>", lambda e: self.apply_preset("標準"))
        self.bind_all("<Control-Shift-2>", lambda e: self.apply_preset("高ボラ"))
        self.bind_all("<Control-Shift-3>", lambda e: self.apply_preset("低ボラ"))
        self.bind_all("<Control-Shift-T>", lambda e: self._open_preset_tuner())


        # --------- UI構築 ---------
    def _build_ui(self):
        # === ① スクロール可能な外枠（縦のみ） ===
        self.container = ttk.Frame(self)
        self.container.grid(row=0, column=0, sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.canvas_outer = tk.Canvas(self.container, borderwidth=0, highlightthickness=0)
        self.vbar = ttk.Scrollbar(self.container, orient="vertical", command=self.canvas_outer.yview)
        self.canvas_outer.configure(yscrollcommand=self.vbar.set)
        self.canvas_outer.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(0, weight=1)

        # ここが従来の root（中身はこの中に作る）
        self.root = ttk.Frame(self.canvas_outer, padding=8)
        self._root_window_id = self.canvas_outer.create_window((0, 0), window=self.root, anchor="nw")

        # 中身サイズに応じてスクロール領域と幅を更新
        def _on_root_configure(event=None):
            try:
                self.canvas_outer.configure(scrollregion=self.canvas_outer.bbox("all"))
                self.canvas_outer.itemconfigure(self._root_window_id, width=self.canvas_outer.winfo_width())
            except Exception:
                pass
        self.root.bind("<Configure>", _on_root_configure)
        self.canvas_outer.bind("<Configure>", lambda e: self.canvas_outer.itemconfigure(self._root_window_id, width=e.width))

        # マウスホイールで縦スクロール（Windows）
        def _bind_wheel(_):
            self.canvas_outer.bind_all("<MouseWheel>", lambda e: self.canvas_outer.yview_scroll(-1 * (e.delta // 120), "units"))
        def _unbind_wheel(_):
            self.canvas_outer.unbind_all("<MouseWheel>")
        self.canvas_outer.bind("<Enter>", _bind_wheel)
        self.canvas_outer.bind("<Leave>", _unbind_wheel)

        # === ② ここから従来UI ===

        # 左ペイン
        self.left = ttk.LabelFrame(self.root, text="操作 / 設定", padding=8)
        ttk.Checkbutton(self.left, text="本番(:18080)", variable=self.is_production).grid(row=0,column=0,sticky="w")
        ttk.Checkbutton(self.left, text="実発注ON（最初はOFF）", variable=self.real_trade).grid(row=1,column=0,sticky="w")

        ttk.Label(self.left, text="APIパスワード").grid(row=2,column=0,sticky="w",pady=(8,0))
        ttk.Entry(self.left, textvariable=self.api_password, show="•", width=26).grid(row=3,column=0,sticky="w")
        ttk.Button(self.left, text="① トークン取得",
                command=lambda: threading.Thread(target=self._get_token, daemon=True).start()
                ).grid(row=4,column=0,sticky="we",pady=(8,2))

        ttk.Separator(self.left).grid(row=5,column=0,sticky="we",pady=8)
        ttk.Label(self.left, text="メイン銘柄コード").grid(row=6,column=0,sticky="w")
        ttk.Entry(self.left, textvariable=self.symbol, width=12).grid(row=7,column=0,sticky="w")

        ttk.Button(self.left, text="② 銘柄登録(/register PUT)",
                command=lambda: threading.Thread(target=self._register_symbol_safe, daemon=True).start()
                ).grid(row=8,column=0,sticky="we",pady=(6,2))
        ttk.Button(self.left, text="③ WebSocket接続",
                command=lambda: threading.Thread(target=self._connect_ws, daemon=True).start()
                ).grid(row=9,column=0,sticky="we")

        ttk.Separator(self.left).grid(row=10,column=0,sticky="we",pady=8)
        ttk.Label(self.left, text="取引モード").grid(row=11,column=0,sticky="w")
        ttk.Combobox(self.left, width=18, state="readonly", textvariable=self.trade_mode,
                    values=["現物","信用(制度)","信用(一般・長期)","信用(一般・デイトレ)"]).grid(row=12,column=0,sticky="w")
        ttk.Label(self.left, text="口座区分").grid(row=13,column=0,sticky="w")
        ttk.Combobox(self.left, textvariable=self.account_type, values=["一般(2)","特定(4)"],
                    width=18, state="readonly").grid(row=14,column=0,sticky="w")
        ttk.Label(self.left, text="数量（株）").grid(row=15,column=0,sticky="w",pady=(6,0))
        ttk.Spinbox(self.left, from_=100, to=100000, increment=100, textvariable=self.qty, width=12).grid(row=16,column=0,sticky="w")

        ttk.Separator(self.left).grid(row=17,column=0,sticky="we",pady=8)

        strat = ttk.LabelFrame(self.left, text="戦略（OCO/Trail）", padding=6); strat.grid(row=18,column=0,sticky="we")
        r=0
        ttk.Label(strat, text="利確(+tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(strat,from_=1,to=20,textvariable=self.tp_ticks,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(strat, text="損切(-tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(strat,from_=1,to=20,textvariable=self.sl_ticks,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Checkbutton(strat, text="トレーリング", variable=self.use_trail).grid(row=r,column=0,sticky="w"); r+=1
        ttk.Label(strat, text="発動(+tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(strat,from_=1,to=20,textvariable=self.trail_trigger,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(strat, text="追随距離(tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(strat,from_=1,to=20,textvariable=self.trail_gap,width=6).grid(row=r,column=1,sticky="w"); r+=1

        filt=ttk.LabelFrame(self.left, text="補助フィルタ（メイン監視）", padding=6); filt.grid(row=19,column=0,sticky="we",pady=(8,0))
        ttk.Checkbutton(filt,text="VWAP（順張り）",variable=self.f_vwap).grid(row=0,column=0,sticky="w")
        ttk.Checkbutton(filt,text="SMA25/5m（順張り）",variable=self.f_sma25).grid(row=1,column=0,sticky="w")
        ttk.Checkbutton(filt,text="MACD(12,26,9)",variable=self.f_macd).grid(row=2,column=0,sticky="w")
        ttk.Checkbutton(filt,text="RSI(14)",variable=self.f_rsi).grid(row=3,column=0,sticky="w")
        ttk.Checkbutton(filt,text="Swing（高値切下げ/安値切上げ）",variable=self.f_swing).grid(row=4,column=0,sticky="w")

        ttk.Separator(self.left).grid(row=20,column=0,sticky="we",pady=8)
        ttk.Button(self.left, text="AUTO ON/OFF", command=self.toggle_auto).grid(row=21,column=0,sticky="we")
        ttk.Button(self.left, text="チャート別ウィンドウ", command=self.toggle_chart_window).grid(row=22,column=0,sticky="we",pady=(6,0))
        ttk.Button(self.left, text="板スナップショットGET",
           command=lambda: threading.Thread(target=self._snapshot_combo, daemon=True).start()
          ).grid(row=23, column=0, sticky="we", pady=(6,0))
        ttk.Button(self.left, text="SIMリセット", command=self.reset_sim).grid(row=24,column=0,sticky="we",pady=(10,0))
        ttk.Checkbutton(self.left,text="MLゲート有効化（GO/NOGO）",variable=self.ml_enabled, command=self._on_ml_toggle).grid(row=25, column=0, sticky="w", pady=(8,0))
        # ★追加（ここから）
        ttk.Separator(self.left).grid(row=26, column=0, sticky="we", pady=8)
        ttk.Button(self.left, text="使い方 / HELP", command=self._open_help).grid(row=27, column=0, sticky="we")
        #ttk.Checkbutton(self.left, text="デバッグ（意思決定ログ）", variable=self.debug_mode).grid(row=28, column=0, sticky="w", pady=(6,0))
        ttk.Button(self.left, text="自己診断（場外テスト）", command=lambda: threading.Thread(target=self.self_check, daemon=True).start()).grid(row=28, column=0, sticky="we", pady=(6,0))
        _, rows_used = self.left.grid_size()
        # self.left の“いちばん下”に確実に置く（固定 row=28 をやめる）
        ttk.Checkbutton(
            self.left,
            text="デバッグ（意思決定ログ）",
            variable=self.debug_mode,
            command=lambda: self._on_debug_toggle(source="chk")
        ).grid(row=rows_used, column=0, sticky="w", pady=(6,0))

                # ★追加（ここまで）

        # 右上サマリー（2段レイアウト）※ root直下への配置は _layout() で grid する（packしない）
        self.summary = ttk.LabelFrame(self.root, text="サマリー（現在値 / 前日比 / SIM成績 / インジ）", padding=8)

        # ── 上段：銘柄名＋現在値（可変幅・長いと「…」で省略） ──
        self.summary_top = ttk.Frame(self.summary)
        self.summary_top.pack(fill="x")

        self.lbl_price  = tk.Label(self.summary_top, text="—", font=("Segoe UI", 18, "bold"), anchor="w")
        self.lbl_price.pack(side="left", fill="x", expand=1)
        self.lbl_change = tk.Label(self.summary_top, text=" — ", font=("Segoe UI", 16, "bold"))
        self.lbl_change.pack(side="left", padx=12)

        # タイトルの省略を幅に合わせて更新
        self.summary_top.bind("<Configure>", lambda e: self._refresh_summary_price())

        # ── 下段：左＝pushes/WS/Vol/Val｜中＝インジケータ｜右＝SIM・成績 ──
        self.summary_bot = ttk.Frame(self.summary)
        self.summary_bot.pack(fill="x", pady=(2, 0))

        # 3カラム（中だけ伸縮）
        self.summary_bot.grid_columnconfigure(0, weight=0)
        self.summary_bot.grid_columnconfigure(1, weight=1)
        self.summary_bot.grid_columnconfigure(2, weight=0)

        # 左：pushes/WS/Vol/Val
        self.lbl_misc = ttk.Label(self.summary_bot, text="pushes=0")
        self.lbl_misc.grid(row=0, column=0, sticky="w")

        # 中：インジ（VWAP/SMA/MACD/RSI）
        self.lbl_inds = ttk.Label(self.summary_bot, text="VWAP:—  SMA25:—  MACD:—/—  RSI:—", anchor="w")
        self.lbl_inds.grid(row=0, column=1, sticky="we", padx=10)

        # 右：SIM建玉と成績（縦配置）
        self.lbl_simpos = ttk.Label(self.summary_bot, text="SIM: —")
        self.lbl_simpos.grid(row=0, column=2, sticky="e")
        self.lbl_stats  = ttk.Label(self.summary_bot, text="Trades: 0 | Win: 0 (0.0%) | P&L: ¥0 | Avg: 0.00t")
        self.lbl_stats.grid(row=1, column=2, sticky="e")

        # Notebook（配置は _layout で）
        self.main_nb = ttk.Notebook(self.root)

        # 板・歩み値
        self.tab_board = ttk.Frame(self.main_nb)
        dom_frame = ttk.LabelFrame(self.tab_board, text="板（10段）", padding=6)
        dom = ttk.Frame(dom_frame); dom.pack(fill="x")
        ttk.Label(dom, text="売り(Ask)").grid(row=0,column=0,sticky="w")
        ttk.Label(dom, text="買い(Bid)").grid(row=0,column=2,sticky="w")

        self.tree_ask = ttk.Treeview(dom, columns=("price","qty"), show="headings", height=12)
        self.tree_ask.heading("price", text="価格"); self.tree_ask.heading("qty", text="数量")
        self.tree_ask.column("price", width=110, anchor="e"); self.tree_ask.column("qty", width=110, anchor="e")
        self.tree_ask.grid(row=1,column=0,sticky="nsew", padx=(0,8))

        self.tree_bid = ttk.Treeview(dom, columns=("price","qty"), show="headings", height=12)
        self.tree_bid.heading("price", text="価格"); self.tree_bid.heading("qty", text="数量")
        self.tree_bid.column("price", width=110, anchor="e"); self.tree_bid.column("qty", width=110, anchor="e")
        self.tree_bid.grid(row=1,column=2,sticky="nsew")

        best = ttk.Frame(dom_frame); best.pack(fill="x", pady=(6,0))
        '''ttk.Label(best, text="Best Ask").pack(side="left"); self.lbl_best_ask = ttk.Label(best, text="—", foreground="red"); self.lbl_best_ask.pack(side="left", padx=6)
        ttk.Label(best, text="AskQty").pack(side="left"); self.lbl_best_askq = ttk.Label(best, text="—"); self.lbl_best_askq.pack(side="left", padx=(2,12))
        ttk.Label(best, text="Best Bid").pack(side="left"); self.lbl_best_bid = ttk.Label(best, text="—", foreground="blue"); self.lbl_best_bid.pack(side="left", padx=6)
        ttk.Label(best, text="BidQty").pack(side="left"); self.lbl_best_bidq = ttk.Label(best, text="—"); self.lbl_best_bidq.pack(side="left", padx=(2,12))
        ttk.Label(best, text="Spread").pack(side="left"); self.lbl_spread = ttk.Label(best, text="—"); self.lbl_spread.pack(side="left", padx=6)
        ttk.Label(best, text="Imbalance").pack(side="left"); self.lbl_imb = ttk.Label(best, text="—"); self.lbl_imb.pack(side="left", padx=6)
        '''
        # Best Ask
        ttk.Label(best, text="Best Ask").pack(side="left")
        self.lbl_best_ask = ttk.Label(best, text="—", foreground="red")
        self.lbl_best_ask.pack(side="left", padx=6)

        ttk.Label(best, text="AskQty").pack(side="left")
        self.lbl_best_askq = ttk.Label(best, text="—")
        self.lbl_best_askq.pack(side="left", padx=(2,12))

        # Best Bid
        ttk.Label(best, text="Best Bid").pack(side="left")
        self.lbl_best_bid = ttk.Label(best, text="—", foreground="blue")
        self.lbl_best_bid.pack(side="left", padx=6)

        ttk.Label(best, text="BidQty").pack(side="left")
        self.lbl_best_bidq = ttk.Label(best, text="—")
        self.lbl_best_bidq.pack(side="left", padx=(2,12))

        # Spread（値ラベル）
        ttk.Label(best, text="Spread").pack(side="left")
        self.lbl_spread = ttk.Label(best, text="—")
        self.lbl_spread.pack(side="left", padx=6)

        # ★ここに “古い固定ラベル text='inv'” は入れない★

        # INV（反転フラグ）
        ttk.Label(best, text="INV:").pack(side="left")
        self.lbl_inv = ttk.Label(best, text="—")
        self.lbl_inv.pack(side="left", padx=6)

        # Imbalance
        ttk.Label(best, text="Imbalance").pack(side="left")
        self.lbl_imb = ttk.Label(best, text="—")
        self.lbl_imb.pack(side="left", padx=6)


        dom_frame.pack(fill="x")

        tape_frame = ttk.LabelFrame(self.tab_board, text="歩み値（最新が上／擬似）", padding=6)
        self.tape = ScrolledText(tape_frame, height=10, font=("Consolas", 10))
        self.tape.pack(fill="x", expand=False)
        tape_frame.pack(fill="x", expand=False, pady=(6,0))
        self.tape.tag_config("up", foreground="red")
        self.tape.tag_config("down", foreground="blue")
        self.tape.tag_config("flat", foreground="black")

        # 資金タブ
        self.tab_wallet = ttk.Frame(self.main_nb)
        btnw = ttk.Frame(self.tab_wallet); btnw.pack(anchor="w", pady=(6,2))
        ttk.Button(btnw, text="資金更新", command=lambda: threading.Thread(target=self.update_wallets, daemon=True).start()).pack(side="left")
        gridw = ttk.Frame(self.tab_wallet); gridw.pack(anchor="w", padx=6, pady=6)
        ttk.Label(gridw,text="現物余力(株式):").grid(row=0,column=0,sticky="w"); ttk.Label(gridw,textvariable=self.cash_stock_wallet).grid(row=0,column=1,sticky="w",padx=10)
        ttk.Label(gridw,text="預り金/現金:").grid(row=1,column=0,sticky="w"); ttk.Label(gridw,textvariable=self.cash_bank).grid(row=1,column=1,sticky="w",padx=10)
        ttk.Label(gridw,text="信用新規建可能額:").grid(row=2,column=0,sticky="w"); ttk.Label(gridw,textvariable=self.margin_wallet).grid(row=2,column=1,sticky="w",padx=10)
        ttk.Label(gridw,text="委託保証金率:").grid(row=3,column=0,sticky="w"); ttk.Label(gridw,textvariable=self.margin_rate).grid(row=3,column=1,sticky="w",padx=10)

        # 建玉
        self.tab_pos = ttk.Frame(self.main_nb)
        btnp = ttk.Frame(self.tab_pos); btnp.pack(anchor="w", pady=(6,2))
        ttk.Button(btnp, text="建玉更新", command=lambda: threading.Thread(target=self.update_positions, daemon=True).start()).pack(side="left")
        self.tree_pos = ttk.Treeview(self.tab_pos, columns=("sym","name","side","qty","price","pl"), show="headings", height=16)
        for c,t,w in (("sym","銘柄コード",100),("name","銘柄名",180),("side","売買",60),("qty","数量",80),("price","建値",90),("pl","評価損益",100)):
            self.tree_pos.heading(c, text=t); self.tree_pos.column(c, width=w, anchor="e" if c in ("qty","price","pl") else "w")
        self.tree_pos.pack(fill="both", expand=True, padx=6, pady=6)

        # 注文
        self.tab_ord = ttk.Frame(self.main_nb)
        btno = ttk.Frame(self.tab_ord); btno.pack(anchor="w", pady=(6,2))
        ttk.Button(btno, text="注文更新", command=lambda: threading.Thread(target=self.update_orders, daemon=True).start()).pack(side="left")
        self.tree_ord = ttk.Treeview(self.tab_ord, columns=("id","sym","name","side","qty","price","status"), show="headings", height=16)
        for c,t,w in (("id","注文ID",180),("sym","銘柄",90),("name","銘柄名",160),("side","売買",60),("qty","数量",80),("price","価格",90),("status","状態",140)):
            self.tree_ord.heading(c, text=t); self.tree_ord.column(c, width=w, anchor="w" if c in ("id","status","name") else "e")
        self.tree_ord.pack(fill="both", expand=True, padx=6, pady=6)

        # SIM履歴
        self.tab_hist = ttk.Frame(self.main_nb)
        bth = ttk.Frame(self.tab_hist); bth.pack(fill="x", pady=(6,2))
        ttk.Button(bth, text="CSV保存",  command=self.save_hist_csv).pack(side="left", padx=(6,0))
        ttk.Button(bth, text="XLSX保存", command=self.save_hist_xlsx).pack(side="left", padx=6)
        self.tree_hist = ttk.Treeview(self.tab_hist, columns=("time","sym","side","qty","entry","exit","ticks","pnl","reason"), show="headings", height=16)
        for c,t,w in (("time","時刻",160),("sym","銘柄",90),("side","売買",60),("qty","数量",80),("entry","建値",90),("exit","決済",90),("ticks","tick",70),("pnl","損益(円)",100),("reason","理由",120)):
            self.tree_hist.heading(c, text=t); self.tree_hist.column(c, width=w, anchor="e" if c in ("qty","entry","exit","ticks","pnl") else "w")
        self.tree_hist.pack(fill="both", expand=True, padx=6, pady=6)

        # LIVE履歴
        self.tab_live = ttk.Frame(self.main_nb)
        btl = ttk.Frame(self.tab_live); btl.pack(fill="x", pady=(6,2))
        ttk.Button(btl, text="LIVE更新（/orders取得）", command=lambda: threading.Thread(target=self.update_live_history, daemon=True).start()).pack(side="left", padx=(6,0))
        ttk.Button(btl, text="CSV保存",  command=self.save_live_csv).pack(side="left", padx=6)
        ttk.Button(btl, text="XLSX保存", command=self.save_live_xlsx).pack(side="left", padx=6)
        self.tree_live = ttk.Treeview(self.tab_live, columns=("time","id","sym","name","side","qty","price","status"), show="headings", height=16)
        for c,t,w in (("time","時刻",160),("id","注文ID",180),("sym","コード",80),("name","銘柄名",160),("side","売買",60),("qty","数量",80),("price","価格",90),("status","状態",160)):
            self.tree_live.heading(c, text=t); self.tree_live.column(c, width=w, anchor="e" if c in ("qty","price") else "w")
        self.tree_live.pack(fill="both", expand=True, padx=6, pady=6)

        # スクリーニング
        self.tab_scan = ttk.Frame(self.main_nb)
        lefts = ttk.Frame(self.tab_scan); lefts.pack(side="left", fill="y", padx=(6,6), pady=6)
        rights = ttk.Frame(self.tab_scan); rights.pack(side="left", fill="both", expand=True, padx=(0,6), pady=6)
        ttk.Label(lefts, text="プリセット銘柄（複数選択可）").pack(anchor="w")
        self.list_preset = tk.Listbox(lefts, selectmode="extended", height=14, width=22)
        for c in PRESET_CODES: self.list_preset.insert("end", f"{c} (取得中)")
        self.list_preset.pack(fill="x")
        ttk.Button(lefts, text="銘柄名を取得/更新", command=lambda: threading.Thread(target=self.update_preset_names, daemon=True).start()).pack(fill="x", pady=(4,10))

        box = ttk.LabelFrame(lefts, text="スクリーニング閾値", padding=6); box.pack(fill="x")
        ttk.Label(box,text="Spread 1tick率 ≥").grid(row=0,column=0,sticky="w"); ttk.Spinbox(box,from_=0.50,to=1.00,increment=0.05,textvariable=self.s_thr_tickrate,width=6).grid(row=0,column=1,sticky="w")
        ttk.Label(box,text="更新/分 ≥").grid(row=1,column=0,sticky="w"); ttk.Spinbox(box,from_=10,to=200,increment=5,textvariable=self.s_thr_updates,width=6).grid(row=1,column=1,sticky="w")
        ttk.Label(box,text="|imb| σ(5分) ≥").grid(row=2,column=0,sticky="w"); ttk.Spinbox(box,from_=0.05,to=0.60,increment=0.01,textvariable=self.s_thr_imbstd,width=6).grid(row=2,column=1,sticky="w")
        ttk.Label(box,text="逆行率 ≤").grid(row=3,column=0,sticky="w"); ttk.Spinbox(box,from_=0.0,to=1.0,increment=0.05,textvariable=self.s_thr_revrate,width=6).grid(row=3,column=1,sticky="w")
        ttk.Button(lefts, text="スクリーニング開始", command=self.start_scan).pack(fill="x", pady=(10,2))
        ttk.Button(lefts, text="停止", command=self.stop_scan).pack(fill="x")
        ttk.Button(lefts, text="選択を主銘柄にセット", command=self.set_main_from_scan_selection).pack(fill="x", pady=(10,0))

        self.tree_scan = ttk.Treeview(rights, columns=("code","name","tickr","upd","imbstd","rev","tick","state"), show="headings", height=18)
        for c,t,w in (("code","コード",90),("name","銘柄名",180),("tickr","1tick率",80),("upd","更新/分",80),("imbstd","imbσ(5分)",90),("rev","逆行率",80),("tick","推定tick",80),("state","状態",200)):
            self.tree_scan.heading(c, text=t); self.tree_scan.column(c, width=w, anchor="e" if c in ("tickr","upd","imbstd","rev","tick") else "w")
        self.tree_scan.pack(fill="both", expand=True)

        # Notebookタブ追加（配置は _layout で）
        self.main_nb.add(self.tab_board, text="板・歩み値")
        self.main_nb.add(self.tab_wallet, text="資金")
        self.main_nb.add(self.tab_pos, text="建玉")
        self.main_nb.add(self.tab_ord, text="注文")
        self.main_nb.add(self.tab_hist, text="SIM履歴")
        self.main_nb.add(self.tab_live, text="LIVE履歴")
        self.main_nb.add(self.tab_scan, text="スクリーニング")

        # ログ（横幅拡張 + 水平スクロール追加）※ 配置は _layout で
        self.logf = ttk.LabelFrame(self.root, text="ログ（送受信・イベント・エラー）", padding=6)
        self.trainbar = ttk.Frame(self.logf); self.trainbar.pack(fill="x", padx=6, pady=(0,6))
        ttk.Button(self.trainbar, text="学習ログ開始", command=self.start_training_log).pack(side="left")
        ttk.Button(self.trainbar, text="学習ログ停止", command=self.stop_training_log).pack(side="left", padx=(6,0))

        self.log_box = ScrolledText(self.logf, height=10, font=("Consolas", 10), wrap="none")
        self.log_box.pack(fill="both", expand=True)
        self.hbar = tk.Scrollbar(self.logf, orient="horizontal", command=self.log_box.xview)
        self.log_box.configure(xscrollcommand=self.hbar.set)
        self.hbar.pack(fill="x")
        # どこかの初期化時に（_build_ui の最後など）
        self.bind_all("<Control-d>", lambda e: self.debug_mode.set(not self.debug_mode.get()))
        try:
            self._init_context_menu()
            self._log("CFG", "右クリックメニュー準備OK（Ctrl+Shift+T で調整ウィンドウ）")
        except Exception as e:
            self._log("CFG", f"右クリックメニュー初期化失敗: {e}")
            
    def _layout(self):
        # root 直下は grid 統一（pack混在禁止）
        self.root.grid_columnconfigure(0, weight=0, minsize=340)  # 左ペイン
        self.root.grid_columnconfigure(1, weight=1)               # 右側（サマリー/Notebook/ログ）
        self.root.grid_rowconfigure(0, weight=0)                  # サマリー
        self.root.grid_rowconfigure(1, weight=2)                  # Notebook
        self.root.grid_rowconfigure(2, weight=1)                  # ログ

        self.left.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0,8))
        self.summary.grid(row=0, column=1, sticky="nsew")
        self.main_nb.grid(row=1, column=1, sticky="nsew", pady=(8,0))
        self.logf.grid(row=2, column=1, sticky="nsew", pady=(8,0))

    # ------ プリセットメニュー -----

    def _build_preset_menu(self):
        import tkinter as tk
        self._preset_menu = tk.Menu(self, tearoff=False)
        self._preset_menu.add_command(label="標準 (Ctrl+Shift+1)", command=lambda: self.apply_preset("標準"))
        self._preset_menu.add_command(label="高ボラ (Ctrl+Shift+2)", command=lambda: self.apply_preset("高ボラ"))
        self._preset_menu.add_command(label="低ボラ (Ctrl+Shift+3)", command=lambda: self.apply_preset("低ボラ"))
        self._preset_menu.add_separator()
        self._preset_menu.add_command(label="調整…", command=self._open_preset_tuner)

    def _show_preset_menu(self, event):
        try:
            self._preset_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._preset_menu.grab_release()

    def _open_preset_tuner(self):
        import tkinter as tk
        from tkinter import ttk
        if hasattr(self, "_tuner") and self._tuner and tk.Toplevel.winfo_exists(self._tuner):
            self._tuner.lift(); return

        t = self._tuner = tk.Toplevel(self)
        t.title("プリセット調整"); t.attributes("-topmost", True)
        t.resizable(False, False)

        v_imb  = tk.DoubleVar(value=float(getattr(self, "imb_threshold", 0.35)))
        v_cd   = tk.IntVar(value=int(getattr(self, "cooldown_ms", 400)))
        v_tp   = tk.IntVar(value=int(self.tp_ticks.get()))
        v_sl   = tk.IntVar(value=int(self.sl_ticks.get()))
        v_spd  = tk.IntVar(value=int(getattr(self, "max_spread_ticks", 2)))
        v_sz   = tk.DoubleVar(value=float(getattr(self, "size_cap_ratio", 0.5)))

        frm = ttk.Frame(t, padding=10); frm.pack(fill="both", expand=True)
        r=0
        ttk.Label(frm,text="Imbalance 閾値").grid(row=r,column=0,sticky="w"); ttk.Spinbox(frm,from_=0.10,to=0.80,increment=0.01,textvariable=v_imb,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(frm,text="クールダウン(ms)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(frm,from_=100,to=1500,increment=50,textvariable=v_cd,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(frm,text="TP(+tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(frm,from_=1,to=10,textvariable=v_tp,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(frm,text="SL(-tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(frm,from_=1,to=10,textvariable=v_sl,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(frm,text="許容スプレッド(max tick)").grid(row=r,column=0,sticky="w"); ttk.Spinbox(frm,from_=0,to=5,textvariable=v_spd,width=6).grid(row=r,column=1,sticky="w"); r+=1
        ttk.Label(frm,text="Best枚数に対する使用率").grid(row=r,column=0,sticky="w"); ttk.Spinbox(frm,from_=0.1,to=1.0,increment=0.05,textvariable=v_sz,width=6).grid(row=r,column=1,sticky="w"); r+=1

        btns = ttk.Frame(frm); btns.grid(row=r,column=0,columnspan=2,sticky="we",pady=(8,0))
        def _apply_and_close():
            self.imb_threshold    = float(v_imb.get())
            self.cooldown_ms      = int(v_cd.get())
            self.max_spread_ticks = int(v_spd.get())
            self.size_cap_ratio   = float(v_sz.get())
            try:
                self.tp_ticks.set(int(v_tp.get())); self.sl_ticks.set(int(v_sl.get()))
            except Exception: pass
            self._log("CFG", f"調整OK: imb={self.imb_threshold}, cd={self.cooldown_ms}ms, "
                            f"tp={self.tp_ticks.get()}, sl={self.sl_ticks.get()}, "
                            f"spread≤{self.max_spread_ticks}t, size≤{int(self.size_cap_ratio*100)}%")
            t.destroy()
        ttk.Button(btns, text="OK", command=_apply_and_close).pack(side="left")
        ttk.Button(btns, text="キャンセル", command=t.destroy).pack(side="left", padx=6)



    # --------- ML toggle ---------
    def _on_ml_toggle(self):
        self.ml_gate.cfg.enabled = bool(self.ml_enabled.get())
        self._log("ML", f"Enabled={self.ml_gate.cfg.enabled}")

    # ==============================
    # REST
    # ==============================
    def _get_token(self):
        try:
            url = self._base_url()+"/token"
            payload={"APIPassword": self.api_password.get().strip()}
            self._log("HTTP", f"POST {url} payload={payload}")
            r = requests.post(url, headers={"Content-Type":"application/json"}, data=json.dumps(payload), timeout=10)
            self._log("HTTP", f"status={r.status_code} resp={r.text[:200]}...")
            r.raise_for_status()
            self.token = r.json()["Token"]
            self._log("HTTP", f"Token OK: {self.token[:8]}...")
            # App._get_token の try 成功後に1行追加
            threading.Thread(target=self.update_wallets, daemon=True).start()  # ← 追加
            # _get_token() 成功直後
            self._log("HTTP", f"Token OK: {self.token[:8]}...")

        except Exception as e:
            self._log_exc("HTTP", e)

    def _register_symbol_safe(self):
        if not self.token:
            self.ui_call(messagebox.showwarning, "Token","先にトークンを取得してください。")
            return
        try:
            url=self._base_url()+"/register"
            payload={"Symbols":[{"Symbol": self.symbol.get().strip(), "Exchange": EXCHANGE}]}
            self._log("HTTP", f"PUT {url} payload={payload}")
            r=requests.put(url, headers={"X-API-KEY": self.token,"Content-Type":"application/json"}, data=json.dumps(payload), timeout=10)
            self._log("HTTP", f"status={r.status_code} resp={r.text[:200]}...")
            r.raise_for_status()
            self._log("WS", "メイン銘柄を登録しました。PUSHはWS接続後に流れます。")
            # 先に状態リセット（UIはafter）
            self.ui_call(self._reset_symbol_state)
            self._last_register_ts = time.time()   # ← /register 成功タイムスタンプ
            self._re_registering = False           # 再登録フラグ解除
            threading.Thread(target=self._snapshot_combo, daemon=True).start()

            # RegistListから他銘柄をunregister
            try:
                j = r.json()
                reg = j.get("RegistList") or []
                cur = self.symbol.get().strip()
                others = [d for d in reg if str(d.get("Symbol","")).strip() != cur]
                if others:
                    u = self._base_url()+"/unregister"
                    r2 = requests.put(u, headers={"X-API-KEY": self.token, "Content-Type":"application/json"},
                                      data=json.dumps({"Symbols": others}), timeout=10)
                    self._log("HTTP", f"UNREGISTER {len(others)} -> status={r2.status_code} resp={r2.text[:200]}...")
            except Exception as e:
                self._log("HTTP", f"[UNREGISTER] err: {e}")

            # スナップショットをHTTPで取得（UIはafterで更新）
            threading.Thread(target=self._snapshot_symbol_once, daemon=True).start()
        except Exception as e:
            self._log_exc("HTTP", e)


    # ===== デバッグヘルパー関数 =====

    def _trace(self, tag, msg):
        if getattr(self, "debug_mode", tk.BooleanVar(value=False)).get():
            if not hasattr(self, "_trace_buf"): self._trace_buf = []
            self._trace_buf.append(f"{tag}:{msg}")

    def _emit_trace(self, verdict):
        if not getattr(self, "debug_mode", tk.BooleanVar(value=False)).get():
            if hasattr(self, "_trace_buf"): self._trace_buf.clear()
            return
        import time
        if not hasattr(self, "_last_trace_log_ts"): self._last_trace_log_ts = 0.0
        line = " | ".join(getattr(self, "_trace_buf", [])[:12])
        now = time.time()
        if now - self._last_trace_log_ts >= 5.0 or verdict.startswith("ENTER"):
            self._log("DEBUG", f"ENTRY DECISION → {verdict} | {line}")
            self._last_trace_log_ts = now
        self._trace_buf.clear()

    # ===== 意思決定 =====
    def _auto_decision_once(self):
        """現在状態から1回だけエントリー判定。UIは触らない。"""
        import time, tkinter as tk

        # ---- 現在値の取得（安全に） ----
        ws_state  = str(getattr(self, "ws_state", "DISCONNECTED"))
        imb_thr   = float(getattr(self, "imb_threshold", 0.45))
        momentum  = float(getattr(self, "momentum", 0.0))
        imbalance = getattr(self, "imbalance", None)
        s_raw     = getattr(self, "spread", None)
        inv       = bool(getattr(self, "_inv", False))
        auto_on = bool(getattr(self, "_auto_on_cached", False))
        ml_go     = bool(getattr(self, "_ml_go", True))

        # INV時は見た目と合わせてスプレッドは絶対値で判定
        s_eff = abs(s_raw) if (s_raw is not None and inv) else s_raw

        # クールダウン
        if not hasattr(self, "cooldown_ms"): self.cooldown_ms = 300
        if not hasattr(self, "_last_enter_ts"): self._last_enter_ts = 0.0
        now = time.time()
        self._cooldown_ok = ((now - self._last_enter_ts) * 1000.0 >= self.cooldown_ms)

        # 可視化トレース
        try:
            self._trace("WS", f"state={ws_state}")
            self._trace("IMB", f"{imbalance if imbalance is not None else '—'} thr={imb_thr}")
            self._trace("MOM", f"{momentum:+.3f}")
            self._trace("SPREAD", f"raw={s_raw if s_raw is not None else '—'} eff={s_eff if s_eff is not None else '—'} inv={inv}")
            self._trace("COOLDOWN", f"ok={self._cooldown_ok}")
            self._trace("ML", f"{'GO' if ml_go else 'NOGO'}")
            self._trace("RISK", f"armed={self._ensure_real_trade_armed()} auto={bool(getattr(self,'_auto_on_cached',False))}")

        except Exception:
            pass

        # ---- 見送り条件（skip時は学習ログ: label=0） ----
        def _skip(why: str, tag: str):
            try: self._trace("WHY", why)
            except Exception: pass
            try: self._log_training_row(side_hint="", label=0, skip_reason=why)
            except Exception: pass
            try: self._emit_trace(tag)
            except Exception: pass

        if not auto_on:
            return _skip("auto_off", "SKIP auto")

        ws_ok = ws_state in ("CONNECTED", "HEALTHY", "OPEN")
        if not ws_ok:
            return _skip("ws_not_connected", "SKIP ws")

        if not self._cooldown_ok:
            return _skip("cooldown", "SKIP cd")

        if s_eff is None or s_eff <= 0:
            return _skip("spread<=0", "SKIP spread")

        # 最大スプレッド制限（tick換算）
        ts = float(getattr(self, "tick_size", 0.5) or 0.5)
        max_sp_ticks = int(getattr(self, "max_spread_ticks", 2))
        try:
            if ts > 0 and max_sp_ticks > 0:
                if (s_eff / ts) > max_sp_ticks:
                    return _skip("wide_spread", "SKIP spread-max")
        except Exception:
            pass

        # 薄板ガード：必要数量がBestの一定割合を超える場合は見送り
        need = int(getattr(self, "_qty_cached", 0))
        bk = float(getattr(self, "best_bidq", 0.0) or getattr(self, "bid_qty", 0.0) or 0.0)
        ak = float(getattr(self, "best_askq", 0.0) or getattr(self, "ask_qty", 0.0) or 0.0)
        cap_ratio = float(getattr(self, "size_cap_ratio", 0.5))  # 50%が既定
        if bk > 0 and ak > 0 and need > 0:
            cap = max(1, int(min(bk, ak) * cap_ratio))
            if need > cap:
                return _skip(f"thin_book need>{cap}(≤{int(cap_ratio*100)}%)", "SKIP thin-book")

        if imbalance is None:
            return _skip("imb=None", "SKIP imb-none")

        # MLゲート（有効時のみ）
        ml_enabled = bool(getattr(self, "_ml_enabled_cached", False))
        if ml_enabled and not ml_go:
            return _skip("ml=nogo", "SKIP ml")

        # ---- 発火ロジック ----
        if float(imbalance) >= imb_thr:
            side = "BUY"
        elif float(imbalance) <= -imb_thr:
            side = "SELL"
        else:
            return _skip("imb_deadband", "SKIP imb-band")

        # ---- エントリー（成功ラベル=1） ----
        try:
            self._log_training_row(side_hint=side, label=1, skip_reason="")
        except Exception:
            pass
        try:
            self._emit_trace(f"ENTER {side}")
        except Exception:
            pass
        try:
            self._send_entry_order(side)
            self._last_enter_ts = now
        except Exception as e:
            self._log("AUTO", f"send_entry_order error: {e}")


    def _http_get(self, tag: str, path: str, timeout=10):
        """共通GET。URL/Status/Body先頭キーをタグ付きでログして、JSON(dict)を返す。"""
        base = self._base_url()
        url = f"{base}{path}"
        headers = {"X-API-KEY": self.token} if getattr(self, "token", None) else {}
        self._log("HTTP", f"[{tag}] GET {url}")
        r = requests.get(url, headers=headers, timeout=timeout)
        head = (r.text or "")[:200]
        try:
            j = r.json() if r.text else {}
            first_keys = ", ".join(list(j.keys())[:5])
        except Exception:
            j = {}
            first_keys = "(not json)"
        self._log("HTTP", f"[{tag}] status={r.status_code} keys=[{first_keys}] body={head}...")
        r.raise_for_status()
        return j



    # ★追加：Tk変数が変更されたらキャッシュを更新（UIスレッドで動く）
    def _sync_auto_cached(*_):
        try:
            self._auto_on_cached = bool(self.auto_enabled.get())
        except Exception:
            pass

    # --- 銘柄コードの正規化と一致判定（WS mismatch対策） ---
    def _normalize_code(self, s):
        if s is None: return ""
        return str(s).strip().split("@")[0]

    def _codes_match(self, a, b) -> bool:
        return self._normalize_code(a) == self._normalize_code(b)


    def _snapshot_combo(self):
        try:
            self._log("HTTP", "snapshot combo: /symbol → /board")
            self._snapshot_symbol_once()
            self._snapshot_board()
        except Exception as e:
            self._log_exc("HTTP", e)

    def _snapshot_board(self):
        if not getattr(self, "token", None):
            return self._log("HTTP", "Token無し（_snapshot_board中止）")

        code = (self.symbol.get() or "").strip()
        code_ex = code if "@" in code else f"{code}@{EXCHANGE}"

        try:
            j = self._http_get("BOARD", f"/board/{code_ex}")

            # 誤応答（/register）検知
            if isinstance(j, dict) and "RegistList" in j and not any(k in j for k in ("CurrentPrice","BidPrice","AskPrice","Buy1","Sell1","Bids","Asks")):
                self._log("HTTP", "[BOARD] 警告: /register の応答を受信"); return

            # 価格
            # …（前略：j = self._http_get(...) まであなたの既存コード）…

            # 現在値
            last = self._pick(j, "CurrentPrice", "LastPrice", "NowPrice")
            if last is not None:
                try: self.last_price = float(last)
                except Exception: pass

            if getattr(self, "prev_close", None) is None:
                prev = self._pick(j, "PreviousClose", "PrevClose", "PreviousClosePrice", "BasePrice")
                if prev is not None:
                    try: self.prev_close = float(prev)
                    except Exception: pass

            # Best（候補を広めに）
            bid  = self._pick(j, "BidPrice","BestBid","BestBidPrice","Buy1.Price","Bids.0.Price")
            ask  = self._pick(j, "AskPrice","BestAsk","BestAskPrice","Sell1.Price","Asks.0.Price")
            bidq = self._pick(j, "BidQty","BestBidQty","Buy1.Quantity","Bids.0.Quantity","Bids.0.Qty")
            askq = self._pick(j, "AskQty","BestAskQty","Sell1.Quantity","Asks.0.Quantity","Asks.0.Qty")

            # === ここを追加：値幅上限/下限を取り込む ===
            up = self._pick(j, "UpperLimit", "UpperPriceLimit", "Upper", "PriceLimitUpper")
            lo = self._pick(j, "LowerLimit", "LowerPriceLimit", "Lower", "PriceLimitLower")
            if up is not None:
                try: self.upper_limit = float(up)
                except: pass
            if lo is not None:
                try: self.lower_limit = float(lo)
                except: pass

            # （任意）特別気配
            spq = self._pick(j, "SpecialQuote", "SpecialBidQuote", "SpecialAskQuote")
            self.special_quote = bool(spq)

            # ★ここに入れる
            self._log("DER", f"limits up={self.upper_limit} lo={self.lower_limit} spq={self.special_quote}",
                    dedup_key="limits-snap")


            # === ★ここで派生→UI（この2行が “raw=j” 付き） ===
            self._derive_book_metrics(bid, ask, bidq, askq, last, raw=j)
            self.ui_call(self._update_price_bar)
            self.ui_call(self._update_metrics_ui)


        except Exception as e:
            self._log_exc("HTTP", e)




    def _derive_book_metrics(self, bid=None, ask=None, bidq=None, askq=None, last=None, raw=None):
        """Best/数量から spread, inv, imbalance を堅牢に計算して保持する。"""
        try:
            # 直近値でフォールバック
            b  = float(bid)  if bid  is not None else (float(self.best_bid)  if getattr(self, "best_bid",  None) is not None else None)
            a  = float(ask)  if ask  is not None else (float(self.best_ask)  if getattr(self, "best_ask",  None) is not None else None)
            bq = float(bidq) if bidq is not None else (float(self.best_bidq) if getattr(self, "best_bidq", None) is not None else None)
            aq = float(askq) if askq is not None else (float(self.best_askq) if getattr(self, "best_askq", None) is not None else None)

            inv = (a is not None and b is not None and a < b)
            self._inv = bool(inv)

            if a is not None and b is not None:
                raw_spread = a - b              # “符号付き”の生スプレッド
                self.spread = raw_spread        # 互換のため保持（inv時は負になる）
                self.spread_eff = abs(raw_spread)  # 判定・表示用の絶対値
            else:
                self.spread = None
                self.spread_eff = None

            # Imbalance（数量が無ければ直前値を温存）
            if bq is not None and aq is not None and (bq + aq) > 0:
                self.imbalance = (bq - aq) / (bq + aq)
                self._imb_ts = time.time()
            else:
                # 欠損時は前回値をそのまま使う（フリッカ防止）
                if not hasattr(self, "imbalance"):
                    self.imbalance = None

            # ログ（デバッグ）
            try:
                self._log("DER", f"calc spread={self.spread} inv={self._inv} bq={bq} aq={aq} imb={self.imbalance}")
            except Exception:
                pass
            try:
                now = time.time()
                ttl_ok = (getattr(self, "_imb_ts", 0.0) and now - self._imb_ts < 1.0)
                imb = self.imbalance if (self.imbalance is not None or ttl_ok) else None
                # …以降は上の実装と同じ
            except Exception:
                pass
        except Exception as e:
            self._log("DER", f"calc error: {e}")



    def _update_metrics_ui(self):
        try:
            inv = bool(getattr(self, "_inv", False))
            sp_eff = getattr(self, "spread_eff", None)
            imb = getattr(self, "imbalance", None)

            # Spread（数値を出す。INV は色だけ赤く、数値は絶対値）
            if hasattr(self, "lbl_spread"):
                if sp_eff is None:
                    self.lbl_spread.config(text="—", foreground="black")
                else:
                    self.lbl_spread.config(text=f"{sp_eff:.1f}", foreground=("red" if inv else "black"))

            # INV マーク（別ラベル）
            if hasattr(self, "lbl_inv_mark"):
                self.lbl_inv_mark.config(text="✓" if inv else "—", foreground=("red" if inv else "black"))

            # もし spread ラベルに “inv” を入れている古いコードがあれば削除してください
            # if hasattr(self, "lbl_inv"): self.lbl_inv.config(text="inv" if inv else "—") ← これは不要

            # Imbalance
            if hasattr(self, "lbl_imb"):
                if imb is None:
                    # 直前値を維持したいならここで TTL を使っても良い（例：1秒以内なら保持）
                    self.lbl_imb.config(text="—")
                else:
                    self.lbl_imb.config(text=f"{imb:+.2f}")
        except Exception:
            pass





    def _on_debug_toggle(self, source="ui"):
        v = bool(self.debug_mode.get())
        # 表記ブレ修正：degug→debug、タグはCFGで統一
        self._log("CFG", f"debug={'ON' if v else 'OFF'}")


    def _debug_auto(self, reason, **kv):
        """AUTOゲートの通過/見送り理由を要点だけログ（1秒に1回まで）"""
        now = time.time()
        last = getattr(self, "_auto_dbg_last", 0.0)
        if now - last < 1.0:
            return
        self._auto_dbg_last = now
        parts = [f"{k}={v:.2f}" if isinstance(v, (int,float)) else f"{k}={v}" for k,v in kv.items()]
        self._log("AUTO", f"dbg: {reason} | " + " ".join(parts))

# 派生値をまとめて再計算して UI を更新
    def _recalc_top_metrics_and_update(self):
        try:
            bid = getattr(self, "best_bid", None)
            ask = getattr(self, "best_ask", None)
            bq  = float(getattr(self, "best_bidq", 0) or 0)
            aq  = float(getattr(self, "best_askq", 0) or 0)

            # spread / inv
            spread = (ask - bid) if (ask is not None and bid is not None) else None
            self.spread = spread
            inv = (ask is not None and bid is not None and ask < bid)

            # imbalance = (BidQty - AskQty) / (BidQty + AskQty)
            imb = None
            denom = (bq + aq)
            if denom > 0:
                imb = (bq - aq) / denom
            self.imbalance = imb

            # ---- UI（必ず after で）----
            def _ui():
                if hasattr(self, "lbl_spread"):
                    self.lbl_spread.config(text=f"{spread:.1f}" if spread is not None else "—")
                # inv を個別ラベルで出したい場合（無ければスキップ）
                if hasattr(self, "lbl_inv"):
                    self.lbl_inv.config(
                        text=("✓" if inv else "—"),
                        foreground=("red" if inv else "black")
                    )
                if hasattr(self, "lbl_imb"):
                    self.lbl_imb.config(text=f"{imb:+.2f}" if imb is not None else "—")
            self.ui_call(_ui)
        except Exception as e:
            self._log("DER", f"recalc error: {e}")


    def _simpos_text(self) -> str:
        """現在のSIM建玉を1行テキスト化（UPLとtickも添える）"""
        p = getattr(self, "pos", None)
        if not p:
            return "SIM: —"
        try:
            side  = (p.get("side") or "?").upper()
            qty   = int(p.get("qty") or 0)
            entry = p.get("entry")
            s = f"SIM: {side} {qty}"
            if entry is not None:
                s += f" @ {float(entry):.1f}"
                last = self.last_price
                if last is not None:
                    ts = self.tick_size or 1.0
                    ticks = (last - entry)/ts if side == "BUY" else (entry - last)/ts
                    pnl   = (last - entry)*qty if side == "BUY" else (entry - last)*qty
                    s += f" | UPL ¥{int(pnl):,} ({ticks:+.1f}t)"
            return s
        except Exception:
            return "SIM: —"

    def _update_simpos(self):
        """サマリー欄のSIM表示を更新（UIスレッドで呼ばれる想定）"""
        try:
            if hasattr(self, "lbl_simpos"):
                self.lbl_simpos.config(text=self._simpos_text())
        except Exception:
            pass

    

    def _set_summary_title(self, full_text: str):
        self._title_full = full_text
        self._refresh_summary_title()

    def _refresh_summary_title(self):
        try:
            full = getattr(self, "_title_full", "") or ""
            w = self.lbl_title.winfo_width()
            if w <= 1:
                self.after(50, self._refresh_summary_title); return
            fnt = tkfont.Font(font=self.lbl_title.cget("font"))
            pad = 8
            if fnt.measure(full) <= max(10, w - pad):
                self.lbl_title.config(text=full); return
            ell = "…"; left, right = 1, len(full)
            while left < right:
                mid = (left + right)//2
                s = full[:mid] + ell
                if fnt.measure(s) <= (w - pad): left = mid + 1
                else: right = mid
            self.lbl_title.config(text=full[:max(1, right-1)] + ell)
        except Exception:
            self.lbl_title.config(text=getattr(self, "_title_full", ""))


    # ===== サマリー表示 =====

    def _set_summary_price(self, full_text: str):
        """上段（銘柄名＋価格＋前日比）のフルテキストを保存 → 幅に合わせて省略表示"""
        self._price_full = full_text or ""
        self._refresh_summary_price()

    def _refresh_summary_price(self):
        """lbl_price の幅に収まるよう '…' 省略（1行固定）"""
        try:
            full = getattr(self, "_price_full", "") or ""
            w = self.lbl_price.winfo_width()
            if w <= 1:
                self.after(50, self._refresh_summary_price)
                return
            fnt = tkfont.Font(font=self.lbl_price.cget("font"))
            pad = 8
            if fnt.measure(full) <= max(10, w - pad):
                self.lbl_price.config(text=full)
                return
            ell = "…"
            left, right = 1, len(full)
            while left < right:
                mid = (left + right) // 2
                s = full[:mid] + ell
                if fnt.measure(s) <= (w - pad): left = mid + 1
                else: right = mid
            self.lbl_price.config(text=full[:max(1, right - 1)] + ell)
        except Exception:
            self.lbl_price.config(text=getattr(self, "_price_full", ""))

    # キー候補から最初に見つかった値を返す（既に _pick があればそちらを使ってOK）
    def _pick(self, d: dict, *paths, default=None):
        """'Bids.0.Price' のようなパスを安全に辿る"""
        for p in paths:
            cur = d
            try:
                for part in str(p).split("."):
                    if part == "": continue
                    if isinstance(cur, list) and part.isdigit():
                        i = int(part); cur = cur[i] if 0 <= i < len(cur) else None
                    elif isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = None
                    if cur is None: break
                if cur is not None: return cur
            except Exception:
                pass
        return default


    def _update_price_bar(self):
        """上段：銘柄名＋現在値のみ表示。前日比は右側の lbl_change にだけ表示する。"""
        try:
            sym  = (self.symbol.get() if hasattr(self, "symbol") else "") or ""
            name = (getattr(self, "symbol_name", "") or "").strip()
            last = getattr(self, "last_price", None)
            prev = getattr(self, "prev_close", None)

            # 前日比テキストと色
            chg_txt = " — "
            color   = "#616161"  # neutral gray
            if isinstance(last, (int, float)) and isinstance(prev, (int, float)) and prev:
                chg = last - prev
                pct = (chg / prev) * 100.0
                chg_txt = f"{chg:+.1f} (+{pct:.2f}%)" if chg > 0 else f"{chg:+.1f} ({pct:+.2f}%)"
                if chg > 0:   color = "#d32f2f"  # red
                elif chg < 0: color = "#2e7d32"  # green

            # ★ここがポイント：タイトルは「コード＋銘柄名＋現在値」だけにする
            if isinstance(last, (int, float)):
                title = f"{sym} {name}  {last:.1f}".strip()
            else:
                title = f"{sym} {name}".strip()

            self._set_summary_price(title)                 # ← 省略表示はこの中で実施
            self.lbl_change.config(text=f" {chg_txt} ",    # ← 前日比はここだけに出す
                                foreground=color)
        except Exception as e:
            try:
                self._log("UI", f"_update_price_bar error: {e}")
            except Exception:
                pass


    def _snapshot_symbol_once(self):
        if not getattr(self, "token", None):
            return self._log("HTTP", "Token無し（_snapshot_symbol_once中止）")
        code = (self.symbol.get() or "").strip()
        code_ex = code if "@" in code else f"{code}@{EXCHANGE}"

        try:
            j = self._http_get("SYMBOL", f"/symbol/{code_ex}")
            name = j.get("SymbolName") or j.get("DisplayName") or j.get("CompanyName") or j.get("Name")
            if name: self.symbol_name = name
            prev = (j.get("PreviousClose") or j.get("PrevClose") or
                    j.get("PreviousClosePrice") or j.get("BasePrice"))
            if prev is not None:
                try: self.prev_close = float(prev)
                except Exception: pass
            self.ui_call(self._update_price_bar)
        except Exception as e:
            self._log_exc("HTTP", e)


    # ==============================
    # WebSocket
    # ==============================
    def _connect_ws(self):
        """WebSocket 接続（接続直後に last_push_ts を現在時刻で初期化。無通信→段階的に回復）"""
        if not self.token:
            return self._log("WS", "Token無し")
        if getattr(self, "_ws_connecting", False):
            return self._log("WS", "接続中です")

        self._ws_connecting = True
        url = self._ws_url()
        headers = [f"X-API-KEY: {self.token}"]
        self.ws_open = False

        # 再登録管理フラグ
        if not hasattr(self, "_re_registering"):
            self._re_registering = False
        if not hasattr(self, "_last_register_ts"):
            self._last_register_ts = 0.0

        def on_open(ws):
            # 接続直後：まず「今」を last_push_ts にセットして idle 誤判定を防ぐ
            now = time.time()
            self.ws_open = True
            self.last_push_ts = now
            self._ws_connect_ts = now
            self._log("WS", f"OPEN {url} (auth ok)")
            try:
                if hasattr(self, "_set_ws_state"):
                    self._set_ws_state("CONNECTED", "open")
            except Exception:
                pass

            # 直近で/register を投げていないなら念のため再投（安全側）
            try:
                if now - getattr(self, "_last_register_ts", 0.0) > 60 and hasattr(self, "_register_symbol_safe"):
                    self._log("WS", "auto re-/register on open (stale>60s)")
                    threading.Thread(target=self._register_symbol_safe, daemon=True).start()
            except Exception:
                pass

            # ウォッチドッグ開始
            threading.Thread(target=self._ws_watchdog_loop, args=(ws,), daemon=True).start()

        def on_message(ws, message):
            try:
                self._handle_push(message)
            finally:
                # PUSHを受けたら「健全」に遷移
                self.last_push_ts = time.time()
                try:
                    if hasattr(self, "_set_ws_state") and self.ws_state != "HEALTHY":
                        self._set_ws_state("HEALTHY", "push")
                except Exception:
                    pass

        def on_error(ws, err):
            self._log("WS", f"ERROR: {err}")
            self.ws_open = False

        def on_close(ws, code, msg):
            self._log("WS", f"CLOSE code={code} msg={msg}")
            self.ws_open = False
            try:
                if hasattr(self, "_set_ws_state"):
                    self._set_ws_state("DISCONNECTED", "close")
            except Exception:
                pass

        # TCP KeepAlive（使える値だけ設定）
        def _sockopt():
            import socket as _s
            opts = [(_s.SOL_SOCKET, _s.SO_KEEPALIVE, 1)]
            for attr, val in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
                if hasattr(_s, attr):
                    opts.append((_s.IPPROTO_TCP, getattr(_s, attr), val))
            return tuple(opts)

        def runner():
            import random, time as _t
            backoff = 1.0
            while getattr(self, "ws_should_run", True):
                try:
                    self._log("WS", f"CONNECT {url}")
                    ws = websocket.WebSocketApp(
                        url, header=headers,
                        on_open=on_open, on_message=on_message,
                        on_error=on_error, on_close=on_close
                    )
                    self.ws = ws
                    # Ping/Pong は無効。無通信監視で段階的に回復。
                    ws.run_forever(ping_interval=0, sockopt=_sockopt())
                except Exception as e:
                    self._log_exc("WS", e)

                if not getattr(self, "ws_should_run", True):
                    break
                sleep = min(backoff, globals().get("WS_RECONNECT_BACKOFF_MAX", 30.0)) + random.uniform(0, 0.5)
                try:
                    if hasattr(self, "_set_ws_state"):
                        self._set_ws_state("DISCONNECTED", f"reconnect in {sleep:.1f}s")
                except Exception:
                    pass
                _t.sleep(sleep)
                backoff = min(backoff * 1.8, globals().get("WS_RECONNECT_BACKOFF_MAX", 30.0))
            self._ws_connecting = False

        threading.Thread(target=runner, daemon=True).start()


    # --------- メインループ（200件/ティック制限） ---------
    def _loop(self):
        try:
            processed = 0
            MAX_PER_TICK = 200
            while processed < MAX_PER_TICK:
                try:
                    msg = self.msg_q.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                try:
                    self._handle_push(msg)
                except Exception as e:
                    self._log("LOOP", f"push err: {e}")
                    tb = traceback.format_exc().strip().splitlines()
                    if tb: self._log("LOOP", tb[-1])
        except Exception as e:
            self._log_exc("LOOP", e)
        finally:
            # 軽いUI更新はここで
            try:
                self.lbl_misc.config(text=f"pushes={self.push_count}")
            except Exception:
                pass
            try:
                self._sim_on_tick()
            except Exception as e:
                 # SIM中は例外で止めず、KeyError('peak') は警告に格下げして継続
                name = type(e).__name__
                text = str(e).strip().strip("'\"").lower()
                if name == "KeyError" and text == "peak":
                    self._log("SIM", "warn: missing field 'peak' (ignored)")
                else:
                    # 実弾ARM時だけ重めに扱う（SIMはwarnで継続）
                    if getattr(self, "_is_real_trade_armed", None) and self._is_real_trade_armed():
                        self._log("ORD", f"block-ish: SIM exception: {name}: {e}")
                    else:
                        self._log("SIM", f"warn: {name}: {e}")
            if self.auto_on.get():
                try:
                    self._auto_loop()
                except Exception as e:
                    self._log("AUTO", f"err: {e}")
            self.after(100, self._loop)

    # --------- PUSH処理 ---------
    def _push_symbol(self, data:Dict[str,Any]) -> str:
        try:
            return str(data.get("Symbol") or data.get("IssueCode") or data.get("SymbolCode") or "").strip()
        except Exception:
            return ""

    def _handle_push(self, msg: str):
        """WS受信1件を処理。@付き/無しの銘柄差異を吸収し、Best→派生→UIを更新。"""
        import json, time, threading

        # ネスト対応ピッカー（クラスに _pick が無くても動くローカル版）
        def _pick_local(d: dict, *paths, default=None):
            if hasattr(self, "_pick"):
                try: return self._pick(d, *paths, default=default)
                except TypeError:  # 古いシグネチャ用
                    pass
            for p in paths:
                cur = d
                try:
                    for part in str(p).split("."):
                        if part == "": continue
                        if isinstance(cur, list) and part.isdigit():
                            i = int(part); cur = cur[i] if 0 <= i < len(cur) else None
                        elif isinstance(cur, dict):
                            cur = cur.get(part)
                        else:
                            cur = None
                        if cur is None: break
                    if cur is not None: return cur
                except Exception:
                    pass
            return default

        def _normalize_code(s):
            if s is None: return ""
            return str(s).strip().split("@")[0]

        def _codes_match(a, b) -> bool:
            try:
                return self._codes_match(a, b)      # 既存があれば利用
            except Exception:
                return _normalize_code(a) == _normalize_code(b)

        # --- JSON 解析 ---
        try:
            data = json.loads(msg)
        except Exception:
            self._log("WS", f"RAW: {msg[:200]}")
            return

        if not hasattr(self, "_mismatch_count"):
            self._mismatch_count = 0

        # --- 銘柄一致チェック（@差は無視） ---
        cur_sym = (self.symbol.get() or "").strip()
        try:
            sym_msg = self._push_symbol(data)
        except Exception:
            sym_msg = None
        if not sym_msg:
            sym_msg = _pick_local(data, "Symbol", "Code", "SymbolCode", default="")

        if sym_msg and not _codes_match(sym_msg, cur_sym):
            self._mismatch_count += 1
            self._log("WS", f"ignore push for {sym_msg} (current={cur_sym})", dedup_key="ws-ignore")
            if self._mismatch_count >= 20:
                self._log("WS", f"mismatch persists -> re-register {cur_sym}")
                threading.Thread(target=self._register_symbol_safe, daemon=True).start()
                self._mismatch_count = 0
            return
        else:
            self._mismatch_count = 0

        self.ws_state = "HEALTHY"

        # --- 値の取得（ネスト対応） ---
        bid  = _pick_local(data, "BidPrice", "BestBid", "BestBidPrice", "Buy1.Price", "Bids.0.Price")
        ask  = _pick_local(data, "AskPrice", "BestAsk", "BestAskPrice", "Sell1.Price", "Asks.0.Price")
        bidq = _pick_local(data, "BidQty", "BestBidQty", "Buy1.Quantity", "Bids.0.Quantity", "Bids.0.Qty")
        askq = _pick_local(data, "AskQty", "BestAskQty", "Sell1.Quantity", "Asks.0.Quantity", "Asks.0.Qty")
        last = _pick_local(data, "CurrentPrice", "LastPrice", "NowPrice")
        vol  = _pick_local(data, "TradingVolume", "Volume")
        prev = _pick_local(data, "PreviousClose", "RefPrice", "ReferencePrice", "PreviousClosePrice")

        # === ここを追加：pushからも上限/下限を更新 ===
        up = _pick_local(data, "UpperLimit", "UpperPriceLimit", "Upper", "PriceLimitUpper")
        lo = _pick_local(data, "LowerLimit", "LowerPriceLimit", "Lower", "PriceLimitLower")
        spq = _pick_local(data, "SpecialQuote","SpecialBidQuote","SpecialAskQuote")
        if up is not None:
            try: self.upper_limit = float(up)
            except: pass
        if lo is not None:
            try: self.lower_limit = float(lo)
            except: pass
        spq = _pick_local(data, "SpecialQuote", "SpecialBidQuote", "SpecialAskQuote")
        self.special_quote = bool(spq)

        self._log("DER", f"limits up={self.upper_limit} lo={self.lower_limit} spq={self.special_quote}",
          dedup_key="limits-push")

        if prev is not None:
            try: self.prev_close = float(prev)
            except Exception: pass

        updated = False
        if bid  is not None: 
            try: self.best_bid  = float(bid);  updated = True
            except Exception: pass
        if ask  is not None: 
            try: self.best_ask  = float(ask);  updated = True
            except Exception: pass
        if bidq is not None:
            try: v = float(bidq); self.bid_qty = v; self.best_bidq = v; updated = True
            except Exception: pass
        if askq is not None:
            try: v = float(askq); self.ask_qty = v; self.best_askq = v; updated = True
            except Exception: pass

        if last is not None:
            try:
                last_f = float(last)
                if getattr(self, "last_price", None) is not None and vol is not None and getattr(self, "last_vol", None) is not None:
                    dv = float(vol) - float(self.last_vol)
                    if dv < 0:
                        self.day_cum_vol = 0.0; self.day_cum_turnover = 0.0; dv = 0.0
                    if dv > 0:
                        self.day_cum_vol += dv
                        self.day_cum_turnover += last_f * dv
                        self.vwap = self.day_cum_turnover / max(1.0, self.day_cum_vol)
                    direction = "UP" if last_f > self.last_price else ("DOWN" if last_f < self.last_price else "FLAT")
                    try: self._append_tape(last_f, int(dv), direction)
                    except Exception: pass
                    self.last_dir = direction
                self.last_price = last_f
                updated = True
            except Exception:
                pass

        if vol is not None:
            try: self.last_vol = float(vol)
            except Exception: pass

        # 板10段（配列 or Sell/Buy形式）
        asks, bids = [], []
        alist = _pick_local(data, "Asks", default=None)
        blist = _pick_local(data, "Bids", default=None)
        if isinstance(alist, list) and isinstance(blist, list):
            for a in alist[:10]:
                try:
                    p = a.get("Price"); q = a.get("Qty") or a.get("Quantity")
                    if p is not None and q is not None: asks.append((float(p), float(q)))
                except Exception: pass
            for b in blist[:10]:
                try:
                    p = b.get("Price"); q = b.get("Qty") or b.get("Quantity")
                    if p is not None and q is not None: bids.append((float(p), float(q)))
                except Exception: pass
        else:
            for i in range(1, 11):
                s = data.get(f"Sell{i}"); b = data.get(f"Buy{i}")
                if s and isinstance(s, dict):
                    p = s.get("Price"); q = s.get("Qty") or s.get("Quantity")
                    if p is not None and q is not None: asks.append((float(p), float(q)))
                if b and isinstance(b, dict):
                    p = b.get("Price"); q = b.get("Qty") or b.get("Quantity")
                    if p is not None and q is not None: bids.append((float(p), float(q)))
        if asks or bids:
            try:
                self.asks = sorted(asks, key=lambda x: x[0], reverse=True)[:10]
                self.bids = sorted(bids, key=lambda x: x[0], reverse=True)[:10]
                updated = True
            except Exception:
                pass

        # === ★ここで派生→UI（この2行が “raw=data” 付き） ===
        try:
            self._derive_book_metrics(bid, ask, bidq, askq, last, raw=data)
        except Exception as e:
            self._log("DER", f"derive err: {e}")

        self._derive_book_metrics(bid, ask, bidq, askq, last)  # ←先に派生値更新
        # ★追加：派生値をUIに反映（WS受信でも必ずやる）
        self.ui_call(self._update_metrics_ui)

        if updated:
            try:
                self.last_push_ts = time.time()
                if not hasattr(self, "push_times"): self.push_times = []
                self.push_times.append(self.last_push_ts)
                if len(self.push_times) > 5000:
                    self.push_times = self.push_times[-2000:]
            except Exception:
                pass
            try: self._infer_tick_size()
            except Exception as e: self._log("TICK", f"infer err: {e}")

            # UI更新は必ず after 経由
            self.ui_call(self._update_price_bar)
            self.ui_call(self._update_summary)
            self.ui_call(self._update_dom_tables)
            self.ui_call(self._update_bars_and_indicators)

            # チャートはレート制限（>=200ms）
            def _draw_chart_cool():
                try:
                    now = time.time()
                    if not hasattr(self, "_last_chart_ts"): self._last_chart_ts = 0.0
                    if (now - self._last_chart_ts) >= 0.20:
                        self._last_chart_ts = now
                        self._draw_chart_if_open()
                except Exception:
                    pass
            self.ui_call(_draw_chart_cool)

        # 自動判定（UIに触らないので直呼びOK）
        try:
            self._auto_decision_once()
        except Exception as e:
            self._log_exc("AUTO", e)

    # --- Best気配をセット → 派生値(spread/inv/imb)を再計算してUI更新 ---
    def _set_best_quote(self, bid=None, ask=None, bidq=None, askq=None):
        try:
            if bid  is not None: self.best_bid  = float(bid)
            if ask  is not None: self.best_ask  = float(ask)
            if bidq is not None: self.best_bidq = float(bidq or 0)
            if askq is not None: self.best_askq = float(askq or 0)

            # 価格・数量のラベル（存在するときのみ）
            def _ui_quote():
                if hasattr(self, "lbl_best_bid"):
                    self.lbl_best_bid.config(text=f"{self.best_bid:.1f}" if self.best_bid is not None else "—")
                if hasattr(self, "lbl_best_ask"):
                    self.lbl_best_ask.config(text=f"{self.best_ask:.1f}" if self.best_ask is not None else "—")
                if hasattr(self, "lbl_best_bidq"):
                    self.lbl_best_bidq.config(text=str(int(self.best_bidq)) if self.best_bidq else "—")
                if hasattr(self, "lbl_best_askq"):
                    self.lbl_best_askq.config(text=str(int(self.best_askq)) if self.best_askq else "—")
            self.ui_call(_ui_quote)

            # 派生値の再計算＋UI更新
            self._recalc_top_metrics_and_update()
        except Exception as e:
            self._log("DER", f"_set_best_quote error: {e}")

    def _recalc_top_metrics_and_update(self):
        try:
            bid = getattr(self, "best_bid", None)
            ask = getattr(self, "best_ask", None)
            # 数量は best_* が無ければ旧フィールドをフォールバック
            bq  = float(getattr(self, "best_bidq", getattr(self, "bid_qty", 0)) or 0)
            aq  = float(getattr(self, "best_askq", getattr(self, "ask_qty", 0)) or 0)

            # spread / inv
            spread_raw = (ask - bid) if (ask is not None and bid is not None) else None
            inv = bool(ask is not None and bid is not None and ask < bid)

            # ← ここが抜けていました
            self.spread = spread_raw                 # 生の値（inv 時は負）
            self._inv = inv                          # 反転フラグ
            self.spread_eff = (abs(spread_raw)       # 表示・判定用（常に絶対値）
                            if spread_raw is not None else None)

            # imbalance
            denom = (bq + aq)
            self.imbalance = ((bq - aq) / denom) if denom > 0 else None

            # --- UI 更新 ---
            def _ui():
                sp_eff = getattr(self, "spread_eff", None)
                inv_f  = bool(getattr(self, "_inv", False))
                imb    = getattr(self, "imbalance", None)

                if hasattr(self, "lbl_spread"):
                    self.lbl_spread.config(
                        text=("—" if sp_eff is None else f"{sp_eff:.1f}"),
                        foreground=("red" if inv_f else "black")
                    )
                if hasattr(self, "lbl_inv"):
                    self.lbl_inv.config(
                        text=("✓" if inv_f else "—"),
                        foreground=("red" if inv_f else "black")
                    )
                if hasattr(self, "lbl_imb"):
                    self.lbl_imb.config(text=(f"{imb:+.2f}" if imb is not None else "—"))

            self.ui_call(_ui)

        except Exception as e:
            self._log("DER", f"recalc error: {e}")


    def _pick(self, d: dict, *paths, default=None):
        """'Buy1.Price', 'Bids.0.Quantity' のようなパスを安全に辿る"""
        for p in paths:
            cur = d
            try:
                for part in str(p).split("."):
                    if part == "": continue
                    if isinstance(cur, list) and part.isdigit():
                        idx = int(part)
                        cur = cur[idx] if 0 <= idx < len(cur) else None
                    elif isinstance(cur, dict):
                        cur = cur.get(part)
                    else:
                        cur = None
                    if cur is None:
                        break
                if cur is not None:
                    return cur
            except Exception:
                pass
        return default



    # ----- 起動 -----

    def _apply_startup_options(self, args):
        """起動オプションを UI 変数 & 内部変数に反映（UIは触らず値だけ入れる）"""
        import tkinter as tk

        # プリセット（日本語/英語どちらでも受ける）
        name = getattr(args, "preset", None)
        name_map = {"std":"標準","volatile":"高ボラ","calm":"低ボラ"}
        if name: 
            if hasattr(self, "_define_presets"): self._define_presets()
            self.apply_preset(name_map.get(name, name)) if hasattr(self, "apply_preset") else None

        # メイン銘柄
        if getattr(args, "symbol", None):
            self.symbol.set(args.symbol.strip())

        # 主要数値
        if getattr(args, "qty", None): self.qty.set(int(args.qty))
        if getattr(args, "tp",  None): self.tp_ticks.set(int(args.tp))
        if getattr(args, "sl",  None): self.sl_ticks.set(int(args.sl))

        # 閾値など（存在すれば上書き）
        if getattr(args, "imb", None) is not None: self.imb_threshold = float(args.imb)
        if getattr(args, "cooldown", None) is not None: self.cooldown_ms = int(args.cooldown)
        if getattr(args, "spread", None) is not None: self.max_spread_ticks = int(args.spread)
        if getattr(args, "size_ratio", None) is not None: self.size_cap_ratio = float(args.size_ratio)

        # 本番/検証
        if args.production: self.is_production.set(True)
        if args.sandbox:    self.is_production.set(False)

        # 実発注ON（ただし ARM しない限り送信はしない）
        if args.real: self.real_trade.set(True)

        # MLゲート
        if getattr(args, "ml", None):
            on = (args.ml.lower() == "on")
            if hasattr(self, "ml_enabled"): self.ml_enabled.set(on)

        # デバッグON
        if getattr(args, "debug", False) and hasattr(self, "debug_mode"):
            self.debug_mode.set(True)

        # APIパスワード（環境変数推奨）
        if getattr(args, "api_pass", None):
            try:
                self.api_password.set(args.api_pass)  # Entry の値になる
                self._log("CFG", "APIパスワード: 起動時オプション/環境変数から設定")
            except Exception:
                pass

    def _boot_seq(self):
        """①トークン → ②銘柄登録 → ③WS接続 → ④スナップショット（順次）。UIは止めない。"""
        import time, threading
        self._log("LOOP", "自動起動: ①トークン→②銘柄登録→③WS接続→④スナップショット")

        # ① トークン
        try:
            self._get_token()
        except Exception as e:
            self._log("HTTP", f"Token失敗: {e}"); return

        # トークン待ち（最大5秒）
        t0 = time.time()
        while not getattr(self, "token", None) and time.time() - t0 < 5:
            time.sleep(0.1)
        if not getattr(self, "token", None):
            self._log("HTTP", "Token未取得のため停止"); return

        # ② 銘柄登録（安全版）
        try:
            self._register_symbol_safe()
        except Exception as e:
            self._log("HTTP", f"register失敗: {e}")

        # ③ WS接続
        try:
            self._connect_ws()
        except Exception as e:
            self._log("WS", f"connect失敗: {e}")

        # ④ スナップショット（板/サマリーなど）
        try:
            # 環境差に強い順で叩く（存在する関数だけ）
            if hasattr(self, "_snapshot_symbol_once"):
                self._snapshot_symbol_once()
            elif hasattr(self, "_snapshot_board"):
                self._snapshot_board()
            elif hasattr(self, "_snapshot_symbol"):
                self._snapshot_symbol()
        except Exception as e:
            self._log("HTTP", f"snapshot失敗: {e}")


    # --------- ティックサイズ推定 ---------
    def _infer_tick_size(self):
        diffs = []
        for i in range(1, len(self.tick_hist)):
            d = abs(self.tick_hist[i][1] - self.tick_hist[i-1][1])
            if d > 0: diffs.append(d)
        for side in (self.asks, self.bids):
            for i in range(1, min(len(side), 5)):
                d = abs(side[i-1][0] - side[i][0])
                if d > 0: diffs.append(d)
        if not diffs: return
        m = min(diffs)
        best = min(ALLOWED_TICKS, key=lambda t: abs(t - m))
        if abs(best - self.tick_size) / max(self.tick_size, 1e-9) > 0.4:
            self.tick_size = best
            self._log("TICK", f"推定更新 -> {self.tick_size}")

    # --------- インジ/表示 ---------
    def _update_bars_and_indicators(self):
        if self.last_price is not None:
            now=dt.datetime.now()
            cur=now.replace(second=0, microsecond=0, minute=(now.minute//5)*5)
            if not self.bars or self.bars[-1][0]!=cur:
                if self.vwap is not None: self.vwap_hist.append(self.vwap)
                self.bars.append([cur,self.last_price,self.last_price,self.last_price,self.last_price])
                cutoff=now-dt.timedelta(minutes=CHART_LOOKBACK_MIN)
                self.bars=[b for b in self.bars if b[0]>=cutoff]
            else:
                b=self.bars[-1]
                b[2]=min(b[2],self.last_price); b[3]=max(b[3],self.last_price); b[4]=self.last_price

        closes=[b[4] for b in self.bars] if self.bars else []
        self.sma25_prev=self.sma25
        self.sma25=self._sma(closes, 25) if closes else None
        self.macd, self.macd_sig = (self._macd(closes) if len(closes)>=26 else (None,None))
        self.rsi = self._rsi(closes, 14) if len(closes)>=15 else None

        if len(self.bars)>=3:
            L=[b[2] for b in self.bars][-3:]; H=[b[3] for b in self.bars][-3:]
            self.swing_higher_lows = (L[0] < L[1] < L[2])
            self.swing_lower_highs = (H[0] > H[1] > H[2])
        else:
            self.swing_higher_lows=None; self.swing_lower_highs=None

    def _sma(self, arr, n):
        if len(arr) < n: return None
        return sum(arr[-n:])/n

    def _ema_series(self, arr, n):
        k=2/(n+1.0); ema=[arr[0]]
        for x in arr[1:]: ema.append(ema[-1] + k*(x-ema[-1]))
        return ema

    def _macd(self, closes):
        if len(closes)<26: return (None,None)
        ema12=self._ema_series(closes,12); ema26=self._ema_series(closes,26)
        macd_line=[ema12[i]-ema26[i] for i in range(len(ema26))]
        sig=self._ema_series(macd_line,9)
        return macd_line[-1], sig[-1]

    def _rsi(self, closes, n=14):
        if len(closes) < n+1: return None
        gains=[]; losses=[]
        for i in range(1,len(closes)):
            ch=closes[i]-closes[i-1]
            gains.append(max(0.0,ch)); losses.append(max(0.0,-ch))
        avg_gain=sum(gains[:n])/n; avg_loss=sum(losses[:n])/n
        for i in range(n,len(gains)):
            avg_gain=(avg_gain*(n-1)+gains[i])/n
            avg_loss=(avg_loss*(n-1)+losses[i])/n
        if avg_loss==0: return 100.0
        rs=avg_gain/avg_loss
        return 100.0 - (100.0/(1.0+rs))

    def _update_summary(self):
        code=self.symbol.get().strip(); name=self._resolve_symbol_name(code) or ""
        if self.last_price is None:
            self.lbl_price.config(text=f"{code} {name} —", fg="black")
            self.lbl_change.config(text=" — ", fg="black")
        else:
            txt=f"{code} {name} {self.last_price:,.1f}"
            if self.prev_close:
                diff=self.last_price-self.prev_close; pct=diff/self.prev_close*100.0
                col="green" if diff>=0 else "red"
                self.lbl_price.config(text=txt, fg="black")
                self.lbl_change.config(text=f"{diff:+.1f} ({pct:+.2f}%)", fg=col)
            else:
                self.lbl_price.config(text=txt, fg="black"); self.lbl_change.config(text=" — ", fg="black")

        t=self.sim_stats['trades']; w=self.sim_stats['wins']
        wr=(w/t*100.0) if t>0 else 0.0
        avg=(self.sim_stats['ticks_sum']/t) if t>0 else 0.0
        pnl=int(self.sim_stats['pnl_yen'])
        self.lbl_stats.config(text=f"Trades: {t} | Win: {w} ({wr:.1f}%) | P&L: ¥{pnl:,} | Avg: {avg:.2f}t")

        sma = "—" if self.sma25 is None else f"{self.sma25:.1f}"
        macd = "—/—" if self.macd is None or self.macd_sig is None else f"{self.macd:+.2f}/{self.macd_sig:+.2f}"
        rsi = "—" if self.rsi is None else f"{self.rsi:.1f}"
        vwap = "—" if self.vwap is None else f"{self.vwap:.1f}"
        self.lbl_inds.config(text=f"VWAP:{vwap} SMA25:{sma} MACD:{macd} RSI:{rsi}")
        self._update_simpos()
        self._update_price_bar()
        self._update_simpos_summary()


    def _append_tape(self, price, size, direction):
        ts=time.strftime("%H:%M:%S")
        mark = "▲" if direction=="UP" else ("▼" if direction=="DOWN" else "・")
        text = f"{ts} {price:,.1f} x{size} {mark}\n"
        tag  = "up" if direction=="UP" else ("down" if direction=="DOWN" else "flat")
        # 最新を上
        try:
            self.tape.insert("1.0", text, tag)
            lines=int(float(self.tape.index('end-1c').split('.')[0]))
            if lines>600: self.tape.delete("550.0","end")
        except Exception:
            pass

    def _update_dom_tables(self):
        # Best 表示
        self.lbl_best_bid.config(text="—" if self.best_bid is None else f"{float(self.best_bid):.1f}")
        self.lbl_best_ask.config(text="—" if self.best_ask is None else f"{float(self.best_ask):.1f}")
        self.lbl_best_bidq.config(text="—" if self.bid_qty is None else f"{int(self.bid_qty)}")
        self.lbl_best_askq.config(text="—" if self.ask_qty is None else f"{int(self.ask_qty)}")

        # Spread / Imbalance
        try:
            if self.best_bid is not None and self.best_ask is not None:
                bid = float(self.best_bid); ask = float(self.best_ask)
                eps = 1e-9
                if ask + eps < bid:
                    # まれな反転（板崩れ/更新順差）を明示
                    self.lbl_spread.config(text="inv")
                    self.lbl_imb.config(text="—")
                else:
                    spread = max(0.0, ask - bid)
                    self.lbl_spread.config(text=f"{spread:.1f}")

                    # Qty が 0 でも計算できるよう None だけ弾く
                    bq = 0.0 if self.bid_qty is None else float(self.bid_qty)
                    aq = 0.0 if self.ask_qty is None else float(self.ask_qty)
                    total = bq + aq
                    if total > 0:
                        imb = (bq - aq) / total
                        self.lbl_imb.config(text=f"{imb:+.2f}")
                    else:
                        self.lbl_imb.config(text="—")
            else:
                self.lbl_spread.config(text="—")
                self.lbl_imb.config(text="—")
        except Exception as e:
            # 何かがおかしくても UI を止めない
            self._log("DOM", f"calc err: {e}")
            self.lbl_spread.config(text="—")
            self.lbl_imb.config(text="—")

        # 板テーブル
        try:
            for t in (self.tree_ask, self.tree_bid):
                for iid in t.get_children():
                    t.delete(iid)
            for p, q in (self.asks or []):
                self.tree_ask.insert("", "end", values=(f"{float(p):.1f}", f"{int(q)}"))
            for p, q in (self.bids or []):
                self.tree_bid.insert("", "end", values=(f"{float(p):.1f}", f"{int(q)}"))
        except Exception:
            pass

    # --- [5] place server-side bracket (TP + SL) ---------------------------------
    def place_server_bracket(self, symbol, exchange, hold_id, qty, entry_side,
                            take_profit_price, stop_trigger, stop_after_price=0,
                            expire_day=0, is_margin=True, account_type=2):
        """
        symbol: '7203' / exchange: 1 (東証) など
        hold_id: 返済対象の建玉ID（/positions で取得）
        qty: 返済数量
        entry_side: '2'買い新規 or '1'売り新規（返済側は自動で反転）
        take_profit_price: 利確指値
        stop_trigger: 逆指値のトリガ価格
        stop_after_price: ヒット後の指値（成行なら 0）
        expire_day: 0=当日、YYYYMMDD でも可
        is_margin: 信用なら True（返済指定が使えます）
        account_type: 2=一般, 4=特定 等（ご利用の口座に合わせて）
        """
        import requests

        if not self.token:
            return self._log("HTTP", "Token無し")

        base = self._base_url()
        headers = {"X-API-KEY": self.token}
        close_side = '1' if entry_side == '2' else '2'  # 返済側の売買区分
        cash_margin = 3 if is_margin else 1            # 3=信用, 1=現物

        def _post(path, payload):
            try:
                r = requests.post(base + path, json=payload, headers=headers, timeout=10)
                ok = r.ok
                self._log("HTTP", f"POST {base+path} {r.status_code} {str(r.text)[:200]}...")
                return ok, (r.json() if ok and r.text else None)
            except Exception as e:
                self._log_exc("HTTP", e)
                return False, None

        # 共通パラメータ（返済）
        common = dict(
            Password=self.api_password.get().strip() if hasattr(self, "api_password") else "",
            Symbol=symbol, Exchange=exchange, SecurityType=1,  # 1=株式
            Side=close_side, CashMargin=cash_margin,
            AccountType=account_type, Qty=int(qty), ExpireDay=expire_day,
            ClosePositions=[{"HoldID": hold_id, "Qty": int(qty)}]
        )

        # 1) 利確（指値）
        tp = dict(common)
        tp.update(dict(FrontOrderType=20, Price=float(take_profit_price)))  # 20=指値
        ok1, resp1 = _post("/sendorder", tp)

        # 2) 損切り（逆指値→成行 or 指値）
        under_over = 1 if entry_side == '2' else 2  # 買い建ては「以下」、売り建ては「以上」
        sl = dict(common)
        sl.update(dict(
            FrontOrderType=30,  # 30=逆指値
            Price=0,            # 逆指値ではここは通常0（成行時）
            ReverseLimitOrder={
                # 株式の逆指値（仕様はOpenAPI定義準拠）
                "TriggerPrice": float(stop_trigger),
                "UnderOver": under_over,          # 1=以下, 2=以上
                "AfterHitOrderType": 1 if stop_after_price == 0 else 2,  # 1=成行,2=指値
                "AfterHitPrice": float(stop_after_price),
            }
        ))
        ok2, resp2 = _post("/sendorder", sl)

        # ログ
        if ok1 and ok2:
            self._log("AUTO", f"BRACKET armed: TP={take_profit_price}, SL@{stop_trigger}{' (MKT)' if stop_after_price==0 else f'→{stop_after_price}'} HoldID={hold_id}")
        else:
            self._log("AUTO", "BRACKET arm failed (片方または両方)")

    # --- [6] watch fill then arm bracket -----------------------------------------
    def arm_after_fill(self, entry_order_id, symbol, exchange, entry_side,
                    qty, take_profit_price, stop_trigger, stop_after_price=0,
                    poll_ms=500):
        """
        新規注文(entry_order_id)の約定を /orders で監視し、建玉ID(HoldID) を /positions から取得。
        取得できたら、その建玉に対して「サーバ常駐の TP(指値) + SL(逆指値)」を装着します。
        - TP/SL 価格が None の場合は、建玉の建値(base_px) と tick 数から自動計算
        - SL は ReverseLimitOrder（サーバ側トリガ）なので、接続断でも損切りは実行されます
        """
        import requests, time

        # 前提チェック
        if not self.token:
            return self._log("HTTP", "Token無し（arm_after_fill中止）")

        base = self._base_url()
        headers = {"X-API-KEY": self.token}

        # 小ヘルパ
        def _get(path):
            try:
                r = requests.get(base + path, headers=headers, timeout=10)
                self._log("HTTP", f"GET {base+path} -> {r.status_code} {str(r.text)[:200]}...")
                return r.ok, (r.json() if r.ok and r.text else None)
            except Exception as e:
                self._log_exc("HTTP", e)
                return False, None

        # 1) /orders で該当注文の完了を待つ（最大 ~20秒）
        tries = 0
        order_done = False
        while tries < 40:  # 40 * 0.5s = 20秒目安
            ok, od = _get(f"/orders?id={entry_order_id}")
            if ok and od:
                # od が dict のとき（単一返し）/ list のとき（複数返し）の両方に対応
                cand = None
                if isinstance(od, dict):
                    cand = od
                elif isinstance(od, list):
                    for x in od:
                        oid = x.get("ID") or x.get("OrderId") or x.get("Oid")
                        if oid and str(oid) == str(entry_order_id):
                            cand = x; break
                if cand:
                    st = cand.get("State")
                    try:
                        st = int(st)
                    except Exception:
                        st = None
                    # 5=終了（公式の運用知見）。4=処理済の扱いもあるため広めに受理
                    if st in (4, 5):
                        order_done = True
                        break
            tries += 1
            time.sleep(max(0.05, poll_ms/1000.0))

        # 2) /positions から当該銘柄/サイドの建玉を取得
        ok, poss = _get("/positions")
        if not ok or not isinstance(poss, list) or not poss:
            return self._log("HTTP", "positions取得失敗（BRACKET装着不可）")

        side_s = str(entry_side)  # '2' = 買い建, '1' = 売り建
        # 条件に合う建玉（銘柄一致・取引所一致・サイド一致・残量>0）
        candidates = []
        for p in poss:
            try:
                if str(p.get("Symbol")) != str(symbol): 
                    continue
                if int(p.get("Exchange", 0)) != int(exchange):
                    continue
                if str(p.get("Side")) != side_s:
                    continue
                leaves = float(p.get("LeavesQty", p.get("HoldQty", 0)))
                if leaves <= 0:
                    continue
                candidates.append(p)
            except Exception:
                continue

        if not candidates:
            # 注文がすぐに positions に反映されないケースに対する簡易リトライ
            for _ in range(4):  # 追加で ~2秒待つ
                time.sleep(0.5)
                ok, poss = _get("/positions")
                if ok and isinstance(poss, list):
                    candidates = [p for p in poss
                                if str(p.get("Symbol")) == str(symbol)
                                and int(p.get("Exchange", 0)) == int(exchange)
                                and str(p.get("Side")) == side_s
                                and float(p.get("LeavesQty", p.get("HoldQty", 0))) > 0]
                    if candidates:
                        break

        if not candidates:
            return self._log("HTTP", "建玉が見つからないためBRACKET装着を中止（後続の孤児掃除に委任）")

        # 最新っぽい建玉を選ぶ（約定時刻などで降順に）
        def _key(p):
            # ExecutionDay が無い環境もあるので、文字列比較でフォールバック
            return p.get("ExecutionDay", "") or p.get("UpdateTime", "") or ""
        pos = sorted(candidates, key=_key, reverse=True)[0]

        hold_id = pos.get("HoldID") or pos.get("ID") or ""
        if not hold_id:
            return self._log("HTTP", "HoldID不明のためBRACKET装着不可")

        # 3) TP/SL が None の場合は、建値から自動で算出（tick 数）
        base_px = pos.get("Price") or pos.get("HoldPrice") or self.last_price
        ts = self.tick_size or 1.0
        tp_t = int(self.tp_ticks.get())
        sl_t = int(self.sl_ticks.get())

        if take_profit_price is None and base_px is not None:
            if side_s == '2':   # 買い建
                take_profit_price = float(base_px + ts * tp_t)
            else:               # 売り建
                take_profit_price = float(base_px - ts * tp_t)

        if stop_trigger is None and base_px is not None:
            if side_s == '2':   # 買い建
                stop_trigger = float(base_px - ts * sl_t)
            else:               # 売り建
                stop_trigger = float(base_px + ts * sl_t)

        if take_profit_price is None or stop_trigger is None:
            return self._log("AUTO", "TP/SL 価格が決定できずBRACKET装着を中止")

        # 4) サーバ常駐の TP(指値) + SL(逆指値) を装着
        try:
            acct = 4 if str(self.account_type.get()).endswith("(4)") else 2
        except Exception:
            acct = 2
        try:
            self.place_server_bracket(
                symbol=symbol, exchange=exchange, hold_id=hold_id, qty=int(qty),
                entry_side=side_s, take_profit_price=float(take_profit_price),
                stop_trigger=float(stop_trigger), stop_after_price=float(stop_after_price),
                expire_day=0, is_margin=True, account_type=acct
            )
        except Exception as e:
            self._log_exc("AUTO", e)


    # --- [7] orphan killer (no position but close order still alive) -------------
    def sweep_orphan_close_orders(self):
        """
        建玉がゼロなのに残っている返済注文（利確/逆指値）を定期的に取消。
        GUI固まらないよう短時間で早期return。1～2分に1回程度呼び出し想定。
        """
        import requests

        if not self.token:
            return
        base = self._base_url()
        headers = {"X-API-KEY": self.token}

        def _get(path):
            try:
                r = requests.get(base + path, headers=headers, timeout=10)
                return r.ok, (r.json() if r.ok and r.text else None)
            except Exception as e:
                self._log_exc("HTTP", e); return False, None

        def _put(path, payload):
            try:
                r = requests.put(base + path, json=payload, headers=headers, timeout=10)
                self._log("HTTP", f"PUT {base+path} {r.status_code} {str(r.text)[:200]}...")
                return r.ok
            except Exception as e:
                self._log_exc("HTTP", e); return False

        ok, poss = _get("/positions")
        if not ok:
            return
        holds = set()
        for p in (poss if isinstance(poss, list) else []):
            hid = p.get("HoldID") or p.get("ID")
            if hid: holds.add(hid)

        ok, orders = _get("/orders")
        if not ok or not isinstance(orders, list):
            return

        # 取消対象 = 返済系かつState<4（未完了）かつ ClosePositions のHoldIDが現在の建玉に存在しない
        for od in orders:
            try:
                if int(od.get("State", 9)) >= 4:   # 4=処理済,5=終了
                    continue
                cps = od.get("ClosePositions") or []
                is_close_order = bool(cps)
                if not is_close_order:
                    continue
                # いずれのHoldIDも現存しない→孤児
                if all((cp.get("HoldID") not in holds) for cp in cps if isinstance(cp, dict)):
                    # 取消
                    payload = {"OrderId": od.get("ID"), "Password": self.api_password if hasattr(self, "api_password") else ""}
                    _put("/cancelorder", payload)
                    self._log("AUTO", f"孤児返済注文を取消: {od.get('Symbol')} {od.get('ID')}")
            except Exception as e:
                self._log_exc("AUTO", e)



    def _ws_watchdog_loop(self, ws):
        """無通信を監視：接続直後は猶予→/register 再投→それでも無通信なら静かに再接続"""
        ok_sec    = globals().get("WS_HEALTH_OK_SEC", 5)
        grace_sec = globals().get("WS_FIRST_PUSH_GRACE_SEC", 8)
        rejoin_s  = globals().get("WS_REJOIN_NO_PUSH_SEC", 16)
        reclo_s   = globals().get("WS_SILENT_RECONNECT_SEC", 30)

        last_state = None
        rejoined = False

        while self.ws is ws:
            time.sleep(1.0)
            now = time.time()

            # idle の基準は「最後の PUSH か、なければ接続時刻」
            lp0 = getattr(self, "last_push_ts", 0.0) or getattr(self, "_ws_connect_ts", now)
            idle = max(0.0, now - lp0)

            # 1) 接続直後の猶予
            if idle <= grace_sec and self.ws_open:
                if last_state != "CONNECTED":
                    try:
                        if hasattr(self, "_set_ws_state"):
                            self._set_ws_state("CONNECTED", f"grace {grace_sec}s (idle={idle:.1f}s)")
                    except Exception:
                        pass
                    last_state = "CONNECTED"
                continue

            # 2) 健全
            if self.ws_open and idle <= ok_sec:
                if last_state != "HEALTHY":
                    try:
                        if hasattr(self, "_set_ws_state"):
                            self._set_ws_state("HEALTHY", f"idle={idle:.1f}s")
                    except Exception:
                        pass
                    last_state = "HEALTHY"
                continue

            # 3) 無通信が続く → まず /register を再投（1回だけ）
            if self.ws_open and idle >= rejoin_s and not rejoined:
                try:
                    if not getattr(self, "_re_registering", False) and hasattr(self, "_register_symbol_safe"):
                        self._re_registering = True
                        self._log("WS", f"no push {idle:.1f}s → re-/register")
                        threading.Thread(target=self._register_symbol_safe, daemon=True).start()
                        # 再登録キック後も少し様子を見る
                        rejoined = True
                        continue
                except Exception:
                    self._re_registering = False

            # 4) それでもダメなら DEGRADED → 切断して再接続
            if self.ws_open and idle >= reclo_s:
                if last_state != "DEGRADED":
                    try:
                        if hasattr(self, "_set_ws_state"):
                            self._set_ws_state("DEGRADED", f"idle={idle:.1f}s")
                    except Exception:
                        pass
                    last_state = "DEGRADED"
                self._log("WS", f"no push {idle:.1f}s → reconnect")
                try:
                    ws.close()  # run_forever を抜けて runner がバックオフ再接続
                except Exception:
                    pass
                break


    # --------- チャート ---------
    def toggle_chart_window(self):
        if self.chart_win and tk.Toplevel.winfo_exists(self.chart_win):
            try: self.chart_win.destroy()
            except: pass
            self.chart_win=None; self.ax=None; self.canvas=None; return

        self.chart_win=tk.Toplevel(self)
        self.chart_win.title("5分足（別ウィンドウ）")
        self.chart_win.geometry("900x600")
        fig=Figure(figsize=(7.5,5.0), dpi=100); self.ax=fig.add_subplot(111); self.ax.grid(True)
        self.canvas=FigureCanvasTkAgg(fig, master=self.chart_win); self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._draw_chart_if_open(force=True)

    def _draw_chart_if_open(self, force:bool=False):
        if not (self.chart_win and self.ax and self.canvas): return
        now = time.time()
        if not force and (now - self._last_draw_ts) < 0.2:
            return
        self._last_draw_ts = now

        self.ax.clear(); self.ax.grid(True)
        if self.bars:
            xs=[b[0] for b in self.bars]; O=[b[1] for b in self.bars]; L=[b[2] for b in self.bars]
            H=[b[3] for b in self.bars]; C=[b[4] for b in self.bars]
            for i,x in enumerate(xs):
                t=mdates.date2num(x)
                col="black" if C[i]>=O[i] else "gray"
                self.ax.plot([t,t],[L[i],H[i]],lw=1,color=col)
                self.ax.plot([t-0.0018,t+0.0018],[O[i],O[i]],lw=4,color=col)
                self.ax.plot([t-0.0018,t+0.0018],[C[i],C[i]],lw=4,color=col)
            self.ax.xaxis_date()
            self.ax.set_title(f"{self.symbol.get()} 5m bars={len(xs)} pushes={self.push_count}")
        else:
            self.ax.set_title(f"{self.symbol.get()} 5m (no data)")
        try:
            self.canvas.draw_idle()
        except Exception as e:
            self._log("CHART", f"draw err: {e}")


    # ===== 場外テスト =====
    def self_check(self):
        try:
            self._log("TEST", "=== 自己診断開始（場外）===")
            # 1) Token
            self._get_token()
            if not self.token: self._log("TEST", "Token取得失敗"); return

            # 2) /register → 他銘柄をunregister
            self._register_symbol_safe()
            self._log("TEST", "register/unregister OK")

            # 3) WS接続（5秒だけ開く）
            self._connect_ws()
            t0 = time.time()
            while time.time()-t0 < 5:
                time.sleep(0.2)
            self._log("TEST", "WS接続OK（場外のためPUSH無しでも可）")

            # 4) /board スナップショット
            self._snapshot_board()
            # 5) UI反映（価格バーなど）
            self.ui_call(self._update_price_bar)

            ok = (self.last_price is not None) and (getattr(self, "symbol_name", None) is not None)
            self._log("TEST", f"サマリー確認 last={self.last_price} name={getattr(self,'symbol_name',None)} → {'OK' if ok else 'NG'}")

            self._log("TEST", "=== 自己診断完了 ===")
        except Exception as e:
            self._log_exc("TEST", e)


    # ==============================
    # AUTO / SIM
    # ==============================
    def toggle_auto(self, on: bool | None = None):
        """AUTOをON/OFF。on=None ならトグル。キャッシュも更新してログ出し。"""
        try:
            cur = bool(self.auto_enabled.get())
        except Exception:
            cur = bool(getattr(self, "_auto_on_cached", False))
        new = (not cur) if on is None else bool(on)
        try:
            # Tk変数 → trace で _auto_on_cached が自動更新される
            self.auto_enabled.set(new)
        except Exception:
            # 念のため保険
            self._auto_on_cached = new
        self._log("AUTO", "ON" if new else "OFF")

    def _recent_momentum(self, sec=0.7):
        if not self.tick_hist: return 0.0
        now = time.time()
        base = None
        for ts0, px0 in reversed(self.tick_hist):
            if ts0 <= now - sec:
                base = (ts0, px0); break
        return 0.0 if base is None else (self.tick_hist[-1][1] - base[1])

    def _microprice(self):
        if None in (self.best_bid, self.best_ask, self.bid_qty, self.ask_qty): return None
        den = self.bid_qty + self.ask_qty
        if den <= 0: return None
        return (self.best_ask * self.bid_qty + self.best_bid * self.ask_qty) / den

    def _filters_ok(self, side):
        lp = self.last_price
        # VWAP
        if self.f_vwap.get():
            if self.vwap is None or lp is None: return False
            if side == "BUY" and not (lp >= self.vwap): return False
            if side == "SELL" and not (lp <= self.vwap): return False
        # SMA25（5分）
        if self.f_sma25.get():
            if self.sma25 is None or self.sma25_prev is None or lp is None: return False
            slope = self.sma25 - self.sma25_prev
            if side == "BUY" and not (lp >= self.sma25 and slope >= 0): return False
            if side == "SELL" and not (lp <= self.sma25 and slope <= 0): return False
        # MACD
        if self.f_macd.get():
            if self.macd is None or self.macd_sig is None: return False
            if side == "BUY" and not (self.macd > self.macd_sig): return False
            if side == "SELL" and not (self.macd < self.macd_sig): return False
        # RSI
        if self.f_rsi.get():
            if self.rsi is None: return False
            if side == "BUY" and not (self.rsi >= 50): return False
            if side == "SELL" and not (self.rsi <= 50): return False
        # Swing
        if self.f_swing.get():
            if side == "BUY" and not (self.swing_higher_lows is True): return False
            if side == "SELL" and not (self.swing_lower_highs is True): return False
        return True

    def _auto_loop(self):
        """自動シグナルのループ（WSが健全時のみ稼働）"""
        # 1) WSが健全でなければ新規エントリを抑止（SIM/実弾とも）
        if getattr(self, "ws_state", "DISCONNECTED") != "HEALTHY":
            now = time.time()
            if now - getattr(self, "_auto_ws_pause_ts", 0.0) > 5.0:
                self._auto_ws_pause_ts = now
                self._log("AUTO", f"paused: WS {getattr(self, 'ws_state', 'UNKNOWN')}")
            return

        # ===== 以降：従来のエントリ判定ロジック =====
        PUSH_FRESH_SEC = globals().get("PUSH_FRESH_SEC", 1.0)
        IMBALANCE_TH = globals().get("IMBALANCE_TH", 0.60)
        SPREAD_TICKS_MAX = globals().get("SPREAD_TICKS_MAX", 1)
        SIGNAL_COOLDOWN_S = globals().get("SIGNAL_COOLDOWN_S", 2.0)
        MOM_WINDOW_S = globals().get("MOM_WINDOW_S", 0.35)

        # データ鮮度と前提
        if time.time() - getattr(self, "last_push_ts", 0.0) > PUSH_FRESH_SEC: return
        if None in (self.best_bid, self.best_ask, self.bid_qty, self.ask_qty, self.last_price): return
        if getattr(self, "pos", None) is not None: return

        # スプレッド制限
        spread = self.best_ask - self.best_bid
        if spread < 0: return
        if self.tick_size and (spread / self.tick_size) > SPREAD_TICKS_MAX: return

        # 板の偏り
        bq = float(self.bid_qty or 0.0); aq = float(self.ask_qty or 0.0)
        total = bq + aq
        if total <= 0: return
        imb = (bq - aq) / total

        now = time.time()
        if now - getattr(self, "last_signal_ts", 0.0) < SIGNAL_COOLDOWN_S:
            return

        # モメンタム
        mom = self._recent_momentum(MOM_WINDOW_S) if hasattr(self, "_recent_momentum") else 0.0
        side = None

        # 標準トリガ（偏り）
        if imb >= IMBALANCE_TH:
            side = "BUY"
        elif imb <= -IMBALANCE_TH:
            side = "SELL"
        else:
            # 補助トリガ：偏りやや弱い＋モメンタム
            if self.tick_size:
                if abs(imb) >= max(0.0, IMBALANCE_TH - 0.15):
                    if mom >= 0.4 * self.tick_size:
                        side = "BUY"
                    elif mom <= -0.4 * self.tick_size:
                        side = "SELL"
                # Spread=1tick で強モメンタムなら試し玉
                if not side and (spread / self.tick_size) <= 1.0:
                    if mom >= 0.8 * self.tick_size:
                        side = "BUY"
                    elif mom <= -0.8 * self.tick_size:
                        side = "SELL"

        if not side:
            if getattr(self, "auto_debug", False):
                try:
                    self._debug_auto("no-side", imb=imb, mom_t=(mom/self.tick_size if self.tick_size else 0), sp_t=(spread/self.tick_size if self.tick_size else 0))
                except Exception:
                    pass
            return

        # 逆行チェック（緩め）
        if self.tick_size:
            if (side == "SELL" and mom > +0.3 * self.tick_size) or (side == "BUY" and mom < -0.3 * self.tick_size):
                if getattr(self, "auto_debug", False):
                    self._debug_auto("reverse-mom", side=side, mom_t=(mom/self.tick_size if self.tick_size else 0))
                return

        # マイクロプライス：明確逆行だけ弾く
        mp = self._microprice() if hasattr(self, "_microprice") else None
        if mp is not None and self.tick_size:
            delta_tick = (self.last_price - mp) / self.tick_size
            if (side == "SELL" and delta_tick > +0.7) or (side == "BUY" and delta_tick < -0.7):
                if getattr(self, "auto_debug", False):
                    self._debug_auto("mp-guard", side=side, d_t=delta_tick)
                return

        # 補助フィルタ（ONのものだけ適用）
        if hasattr(self, "_filters_ok") and not self._filters_ok(side):
            if getattr(self, "auto_debug", False):
                self._debug_auto("filters-ng", side=side)
            return

        # MLゲート（有効なら）
        if hasattr(self, "ml_gate") and self.ml_gate is not None and getattr(self.ml_gate, "cfg", None) and self.ml_gate.cfg.enabled:
            ppm = sum(1 for t in getattr(self, "push_times", []) if t >= now - 60)
            feats = {"imbalance": imb, "spread": spread, "tick_size": self.tick_size,
                    "macd": getattr(self, "macd", None), "macd_sig": getattr(self, "macd_sig", None),
                    "rsi14": getattr(self, "rsi", None), "pushes_per_min": ppm,
                    "vwap": getattr(self, "vwap", None), "last": self.last_price}
            intent = OrderIntent(tp_ticks=self.tp_ticks.get(), sl_ticks=self.sl_ticks.get())
            dec = self.ml_gate.evaluate(intent, feats)
            if not dec.go:
                self._log("ML", f"NO-GO {dec.reason}")
                return
            else:
                self._log("ML", f"GO {dec.reason}")

        # シグナル確定 → SIM
        self._log("AUTO", f"{side} signal (imb={imb:+.2f}, mom={mom/self.tick_size if self.tick_size else 0:+.2f}t, sp={spread/self.tick_size if self.tick_size else 0:.1f}t)")
        self._sim_open(side)

        # 実発注（ARM安全弁あり）
        if self.real_trade.get() and self.is_production.get() and self.token:
            if hasattr(self, "_ensure_real_trade_armed") and not self._ensure_real_trade_armed():
                self._log("ORD", "実発注は未ARM（SIMのみ）。Ctrl+Shift+Rで有効化、F12で解除。")
            else:
                self._send_entry_order(side)
        elif self.real_trade.get() and not self.is_production.get():
            self._log("AUTO", "実発注ONですが検証(:18081)のため注文は送信しません。")

        self.last_signal_ts = now
        try:
            self._log_training_row()
        except Exception:
            pass



    # ====== SIM（簡易版：内部ロジックは現状維持） ======
    def reset_sim(self):
        self.pos = None
        self.sim_stats = {'trades': 0, 'wins': 0, 'losses': 0, 'ticks_sum': 0.0, 'pnl_yen': 0.0}
        self.sim_trades.clear()
        self.refresh_hist_table()
        self._update_summary()
        self._log("SIM", "リセット")

    def _round_tick(self, px): return round(px / self.tick_size) * self.tick_size

    def _sim_open(self, side: str):
        """SIM建玉オープン（引け後でも価格が取れれば成立）。実注文は出さない。"""
        try:
            if getattr(self, "pos", None) is not None:
                self._log("SIM", "already in position")
                return

            # 価格の取得（BUY→Ask / SELL→Bid → last → HTTPスナップショット）
            px = (self.best_ask if side == "BUY" else self.best_bid) or self.last_price
            if px is None:
                try:
                    # UIは触らないスナップショット。完了後に self.last_price などが更新される想定。
                    self._snapshot_symbol_once()
                    px = self.last_price or (self.best_ask if side == "BUY" else self.best_bid)
                except Exception:
                    px = None

            if px is None:
                self._log("SIM", "open skipped: price unavailable（引け後・無更新の可能性）")
                return

            qty = int(self.qty.get())
            self.pos = {"side": side, "qty": qty, "entry": float(px), "ts": time.time()}
            self._log("SIM", f"OPEN {side} {qty} @ {float(px):.1f} (manual/SIM)")
            self.ui_call(self._update_simpos)
            # ここで必要ならUIのサマリー更新などを after で呼ぶ
            try:
                self.ui_call(self._update_summary)
            except Exception:
                pass

        except Exception as e:
            self._log_exc("SIM", e)


    def _sim_on_tick(self):
        if self.pos is None or self.last_price is None: return
        p = self.pos
        side = p["side"]; entry = p["entry"]; qty = p["qty"]
        last = float(self.last_price)
        # 利確/損切/トレーリング（簡易）
        ticks = (last - entry) / self.tick_size * (1 if side=="BUY" else -1)
        # 利確
        if ticks >= self.tp_ticks.get():
            self._sim_close(last, reason="TP")
            return
        # 損切
        if ticks <= -self.sl_ticks.get():
            self._sim_close(last, reason="SL")
            return
        # トレーリング


        # トレーリング
        if self.use_trail.get():
            # 初期化（pos['peak'] が無い/Noneでも動くように）
            base_entry = float(p.get("entry", last))
            peak = p.get("peak", None)
            try:
                peak = float(peak) if peak is not None else base_entry
            except Exception:
                peak = base_entry

            # 直近価格で peak を更新
            if side == "BUY":
                peak = max(peak, last)
            else:
                peak = min(peak, last)
            p["peak"] = peak  # 更新した値を保存

            # 判定用パラメータ（数値化しておく）
            trigger = float(self.trail_trigger.get()) * float(self.tick_size)
            gap     = float(self.trail_gap.get())     * float(self.tick_size)

            # クローズ条件
            if side == "BUY"  and (peak - base_entry) >= trigger and (peak - last)  >= gap:
                self._sim_close(last, reason="TRAIL"); return
            if side == "SELL" and (base_entry - peak) >= trigger and (last - peak) >= gap:
                self._sim_close(last, reason="TRAIL"); return


    def _sim_close(self, exit_price: float, reason="MANUAL"):
        """SIMポジションをクローズし、履歴に残す。必ず self.pos=None にする。"""
        try:
            p = getattr(self, "pos", None)
            if not p:
                self._log("SIM", "close: no position")
                return

            side  = (p.get("side") or "").upper()
            qty   = int(p.get("qty") or 0)
            entry = float(p.get("entry"))

            ts    = self.tick_size or 1.0
            ticks = ((exit_price - entry)/ts) if side == "BUY" else ((entry - exit_price)/ts)
            pnl   = (exit_price - entry)*qty  if side == "BUY" else (entry - exit_price)*qty

            # 履歴記録（実装があればそれを使う）
            try:
                if hasattr(self, "_append_sim_history"):
                    self._append_sim_history(side=side, qty=qty, entry=entry,
                                            exit=exit_price, ticks=ticks, pnl=pnl, reason=reason)
                elif hasattr(self, "tree_hist"):
                    now = time.strftime("%Y-%m-%d %H:%M:%S")
                    vals = (now, self.symbol.get(), side, qty,
                            f"{entry:.1f}", f"{exit_price:.1f}",
                            f"{ticks:+.1f}", f"{int(pnl):,}", reason)
                    # UIはメインスレッドで
                    self.ui_call(self.tree_hist.insert, "", 0, values=vals)
            except Exception as e:
                self._log_exc("SIM", e)

            # ポジションを確実にクリア
            self.pos = None

            # ログとUI更新
            self._log("SIM", f"CLOSE {side} {qty} @ {exit_price:.1f} ({reason}) pnl=¥{int(pnl):,} ({ticks:+.1f}t)")
            try:
                self.ui_call(self._update_summary)
                self.ui_call(self._update_simpos)   # ← サマリーの「SIM: —」更新
            except Exception:
                pass

        except Exception as e:
            self._log_exc("SIM", e)
            # エラーでも表示だけは矛盾しないようにする（好みに応じてコメントアウト可）
            try:
                self.pos = None
                self.ui_call(self._update_simpos)
            except Exception:
                pass


    def refresh_hist_table(self):
        try:
            for iid in self.tree_hist.get_children(): self.tree_hist.delete(iid)
            for row in self.sim_trades:
                self.tree_hist.insert("", "end", values=row)
        except Exception:
            pass

    # --------- 実発注（成行相当） ---------
    def _order_mode_params(self):
        m = self.trade_mode.get()
        if m == "現物": return {"CashMargin": 1, "MarginTradeType": None}
        if m == "信用(制度)": return {"CashMargin": 2, "MarginTradeType": 1}
        if m == "信用(一般・長期)": return {"CashMargin": 2, "MarginTradeType": 2}
        return {"CashMargin": 2, "MarginTradeType": 3}

    def _clamp_price_for_side(self, side: str, price: float):
        """limit を超えたら限界値にクランプ。等しいのは許可。戻り値: (px, reason or '')"""
        if price is None:
            return None, "no_price"
        px = float(price)
        up, lo = self.upper_limit, self.lower_limit
        if side == "BUY" and up is not None and px > up:
            return up, "clamped_upper"
        if side == "SELL" and lo is not None and px < lo:
            return lo, "clamped_lower"
        return px, ""

    def _peak_guard(self, side: str, price: float) -> str:
        """limit を“超えた”ときだけ PEAK。等しいなら通す。"""
        if price is None:
            return "no_price"
        up, lo = self.upper_limit, self.lower_limit
        eps = 1e-9
        if side == "BUY"  and up is not None and float(price) > float(up) + eps:
            return "PEAK"
        if side == "SELL" and lo is not None and float(price) < float(lo) - eps:
            return "PEAK"
        return ""



    def _send_entry_order(self, side: str):
        """
        実弾条件を満たさない場合は即SIMルートへ。
        実弾条件（:18080 + real_trade ON + armed）が揃えば /sendorder を叩く。
        """
            # ★ まず安全なスナップショットを作る（これを以後の“唯一の価格変数”にする）
        px = price

        try:
            # 必要ならここで px を決める
            if px is None:
                # 例：成行っぽい振る舞いなら最良気配や最後値にフォールバック
                if side == "BUY":
                    px = self.best_ask if getattr(self, "best_ask", None) is not None else self.last_price
                else:
                    px = self.best_bid if getattr(self, "best_bid", None) is not None else self.last_price

            # ここまでで px が決まらないなら明示的にエラー
            if px is None:
                raise ValueError("price is None (no quote available)")

            # ……以降のロジックは **price ではなく常に px** を使う……
            # body = {..., "price": float(px), ...}
            # 実注文 / SIM 注文の送信処理など

        except Exception as e:
            tag = "AUTO" if is_auto else "ORD"
            # ★ 例外ログは px だけを参照（price は触らない）
            self._log(tag, f"send_entry_order error: {type(e).__name__}: {e}; px={px}")
            return
            # === 追加ここから ===
        # ベース価格（price未指定なら Best を使用）
        if price is None:
            base = (self.best_ask if side == "BUY" else self.best_bid) or self.last_price
        else:
            base = price

        # tick 丸め
        ts = float(getattr(self, "tick_size", 0.5) or 0.5)
        if base is None:
            self._log("ORD", "no base price"); return
        px = round(float(base) / ts) * ts

        # はみ出しクランプ（超えていたら上限/下限に寄せる）
        px, clamp = self._clamp_price_for_side(side, px)
        if clamp:
            self._log("ORD", f"price {clamp} -> {px}")

        # 最終ガード：等しいのは許可、超えてたら PEAK
        why = self._peak_guard(side, px)
        if why == "PEAK":
            self._log("ORD", f"PEAK: price out of daily limit (side={side} px={px} up={self.upper_limit} lo={self.lower_limit})")
            return  # ブロック
        try:
            # 実弾の武装と各スイッチ
            armed = bool(self._ensure_real_trade_armed())
            is_prod = bool(self.is_production.get()) if hasattr(self, "is_production") else False
            real_on = bool(self.real_trade.get()) if hasattr(self, "real_trade") else False

            # 実弾不可ならSIMへ
            if (not armed) or (not is_prod) or (not real_on):
                reason = "未ARM" if not armed else ("検証(:18081)" if not is_prod else "実発注OFF")
                self._log("ORD", f"{reason} → SIMルートへ")
                return self._sim_enter(side)

            # ------- ここから実弾 (/sendorder) -------
            url = self._base_url() + "/sendorder"
            acct = 4 if str(self.account_type.get()).endswith("(4)") else 2
            price = self.best_ask if side == "BUY" else self.best_bid
            if price is None: price = self.last_price
            if price is None:
                self._log("ORD", "価格未取得のためキャンセル（SIM/実弾ともに不可）")
                return

            mp = self._order_mode_params()
            payload = {
                "Symbol": (self.symbol.get() or "").strip(),
                "Exchange": EXCHANGE,
                "SecurityType": SECURITY_TYPE,
                "Side": 2 if side == "BUY" else 1,
                "CashMargin": mp["CashMargin"],
                "AccountType": acct,
                "Qty": int(getattr(self, "_qty_cached", self.qty.get() if hasattr(self,"qty") else 0)),
                "FrontOrderType": 20,
                "Price": float(price),
                "ExpireDay": 0,
                "DelivType": 0
            }
            if mp["MarginTradeType"] is not None:
                payload["MarginTradeType"] = mp["MarginTradeType"]

            import requests, threading
            self._log("HTTP", f"POST {url} payload={str(payload)[:200]}...")
            r = requests.post(url, headers={"X-API-KEY": self.token}, json=payload, timeout=10)
            self._log("HTTP", f"status={r.status_code} resp={r.text[:200]}...")
            r.raise_for_status()
            try:
                resp = r.json(); oid = resp.get("OrderId") or resp.get("ID")
            except Exception:
                oid = None
            self._append_live_history(order_id=oid, side=side, price=float(price),
                                    qty=int(payload["Qty"]), status="SENT")
        except Exception as e:
            self._log("ORD", f"ERROR(sendorder): {e}")
            self._append_live_history(order_id=None, side=side,
                                    price=float(self.last_price or 0.0),
                                    qty=int(getattr(self,"_qty_cached",0)),
                                    status=f"ERROR:{e}")


    def _sim_enter(self, side: str, reason: str = "AUTO"):
        """最良気配（なければlast）で疑似約定 → サマリー更新 → SIM履歴行（ENTRY）作成"""
        import time
        px = (self.best_ask if side == "BUY" else self.best_bid)
        if px is None:
            px = self.last_price
        qty = int(getattr(self, "_qty_cached", self.qty.get() if hasattr(self,"qty") else 0))
        if px is None or qty <= 0:
            self._log("SIM", "NO-ENTRY（価格/数量不足）")
            return

        sym = (self.symbol.get() or "").strip()
        self.sim_pos = {"side": side, "qty": qty, "entry": float(px),
                        "ts": time.time(), "symbol": sym, "row_id": None}

        # 履歴（ENTRY行を先に差し込んで、row_idを保持。EXIT時に更新）
        row_id = None
        try:
            if hasattr(self, "tree_hist") and self.tree_hist:
                tstr = time.strftime("%H:%M:%S")
                row_id = self.tree_hist.insert(
                    "", 0,
                    values=(tstr, sym, side, qty, f"{px:.1f}", "", "", "", f"ENTER:{reason}")
                )
        except Exception:
            pass
        self.sim_pos["row_id"] = row_id

        self._log("SIM", f"ENTRY {side} {qty} @ {px}")
        self.ui_call(self._update_simpos_summary)

    def _sim_flatten(self, reason: str = "MANUAL"):
        """SIM建玉の成行決済（Bestが無ければlast）。履歴更新して建玉クリア。"""
        import time
        if not self.sim_pos:
            self._log("SIM", "FLAT: 建玉なし"); return

        side = self.sim_pos["side"]
        qty  = int(self.sim_pos["qty"])
        ent  = float(self.sim_pos["entry"])
        sym  = self.sim_pos.get("symbol","")
        # 反対側の気配で決済
        px = (self.best_bid if side == "BUY" else self.best_ask)
        if px is None: px = self.last_price
        if px is None:
            self._log("SIM", "FLAT: 価格未取得"); return
        ex  = float(px)

        ts = float(getattr(self, "tick_size", 0.5) or 0.5)
        ticks = 0
        try:
            raw = (ex - ent) if side == "BUY" else (ent - ex)
            ticks = int(round(raw / ts))
        except Exception:
            pass
        pnl = (ex - ent) * qty if side == "BUY" else (ent - ex) * qty

        # 既存のENTRY行があれば更新、なければ新規でEXIT行を追加
        try:
            if self.sim_pos.get("row_id") and hasattr(self, "tree_hist"):
                tstr = time.strftime("%H:%M:%S")
                self.tree_hist.item(
                    self.sim_pos["row_id"],
                    values=(tstr, sym, side, qty, f"{ent:.1f}", f"{ex:.1f}", ticks, int(round(pnl)), reason)
                )
            elif hasattr(self, "tree_hist"):
                tstr = time.strftime("%H:%M:%S")
                self.tree_hist.insert(
                    "", 0,
                    values=(tstr, sym, side, qty, f"{ent:.1f}", f"{ex:.1f}", ticks, int(round(pnl)), reason)
                )
        except Exception:
            pass

        self._log("SIM", f"EXIT  {side} {qty} @ {ex}  ticks={ticks}  pnl={int(round(pnl))}")
        self.sim_pos = None
        self.ui_call(self._update_simpos_summary)

    def _update_simpos_summary(self):
        """右上サマリーの 'SIM: …' を更新"""
        try:
            if not hasattr(self, "lbl_simpos") or self.lbl_simpos is None:
                return
            if not self.sim_pos:
                self.lbl_simpos.config(text="SIM: —"); return
            side = self.sim_pos["side"]; qty = self.sim_pos["qty"]; ent = self.sim_pos["entry"]
            self.lbl_simpos.config(text=f"SIM: {side} {qty} @ {ent:.1f}")
        except Exception:
            pass


    # --------- 学習ログ ---------
    def start_training_log(self):
        """
        学習ログを開始。
        - 既存の保存場所（scalper/sim_logs/features/YYYYMMDD/<symbol>.csv）を維持
        - 既存ファイルが v1 ヘッダ（ts,symbol,side_hint,...）なら <symbol>.v2.csv に切替
        - 新規/空ファイルのときだけヘッダを1回書く
        """
        if not _ML_AVAILABLE:
            self._log("TRAIN", "ml-disabled"); return

        import csv, os
        from pathlib import Path
        import datetime as dt

        base = Path(scalper.__file__).resolve().parent / "sim_logs" / "features"
        day = dt.datetime.now().strftime("%Y%m%d")
        out_dir = base / day
        out_dir.mkdir(parents=True, exist_ok=True)

        sym = self.symbol.get().strip()
        path = out_dir / f"{sym}.csv"

        # 既存が v1 ヘッダ（ts,symbol,side_hint,...) なら v2 へ移行
        use_v2_path = False
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as rf:
                    first_line = rf.readline().strip()
                if first_line.startswith("ts,symbol,side_hint"):
                    use_v2_path = True
            except Exception:
                pass
        if use_v2_path:
            path = out_dir / f"{sym}.v2.csv"

        is_new_file = (not path.exists())

        # 追記オープン
        self.train_f = open(path, "a", newline="", encoding="utf-8")
        self.train_writer = csv.writer(self.train_f)
        self.train_csv_path = str(path)

        # 空ファイルのときだけヘッダを1回書く
        if is_new_file or self.train_f.tell() == 0:
            header = ["ts","symbol","side","label","skip_reason","tp_ticks","sl_ticks"] \
                    + list(FEATURE_COLUMNS) + ["pushes_per_min"]
            self.train_writer.writerow(header)
            try: self.train_f.flush()
            except Exception: pass

        self._log("TRAIN", f"開始: {path}")


    def stop_training_log(self):
        if getattr(self, "train_f", None):
            try: self.train_f.flush()
            except Exception: pass
            try: self.train_f.close()
            except Exception: pass
            self.train_f = None
            self.train_writer = None
            self._log("TRAIN", "停止")

    def _log_training_row(self, side_hint: str = "", label: int = 0, skip_reason: str = None):
        """
        学習ログ1行をCSVに書く。
        side_hint : "BUY"/"SELL" or ""（SKIP時は空）
        label     : 1=ENTER, 0=SKIP
        skip_reason : 見送り理由（ENTER時は "" 推奨）。None の場合は _trace_buf から WHY: を自動抽出（emit_trace 前に呼ぶこと）。
        """
        if not getattr(self, "train_writer", None):
            return

        import time
        now = time.time()

        # 直近1分のpush回数（特徴量の一つ）
        ppm = sum(1 for t in getattr(self, "push_times", []) if t >= now - 60)

        # 特徴量を作成（既存の関数をそのまま利用）
        feats = compute_features(
            symbol=self.symbol.get().strip(),
            last_price=self.last_price,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            bid_qty=getattr(self, "bid_qty", None),   # 既存プロパティ名に合わせる
            ask_qty=getattr(self, "ask_qty", None),
            asks=getattr(self, "asks", None),
            bids=getattr(self, "bids", None),
            vwap=getattr(self, "vwap", None),
            sma25=getattr(self, "sma25", None),
            macd=getattr(self, "macd", None),
            macd_sig=getattr(self, "macd_sig", None),
            rsi=getattr(self, "rsi", None),
            tick_hist=list(getattr(self, "tick_hist", [])),
            tick_size=getattr(self, "tick_size", None),
        )
        feats["pushes_per_min"] = float(ppm)

        # skip_reason が指定されていなければ、TRACEバッファから WHY: を1個だけ抽出（emit_trace 前に呼んでね）
        if skip_reason is None:
            skip_reason = ""
            try:
                if getattr(self, "debug_mode", None) and self.debug_mode.get():
                    for t in getattr(self, "_trace_buf", []):
                        if isinstance(t, str) and t.startswith("WHY:"):
                            skip_reason = t[4:]
                            break
            except Exception:
                pass

        # 行を組み立て（ヘッダは start_training_log のパッチを参照）
        tp = int(self.tp_ticks.get())
        sl = int(self.sl_ticks.get())
        row = [
            feats.get("ts"),                 # 0 ts
            feats.get("symbol"),             # 1 symbol
            side_hint or "",                 # 2 side
            int(label),                      # 3 label (1=ENTER, 0=SKIP)
            skip_reason or "",               # 4 skip_reason
            tp,                              # 5 tp_ticks
            sl,                              # 6 sl_ticks
        ] + [feats.get(c) for c in FEATURE_COLUMNS] + [
            feats.get("pushes_per_min")      # 末尾に追加
        ]

        self.train_writer.writerow(row)
        try:
            # Windowsでもこまめに落ちないようflush
            self.train_f.flush()
        except Exception:
            pass

    # ==============================
    # 資金/建玉/注文/LIVE履歴（簡易REST）
    # ==============================
    def update_wallets(self):
        """資金情報を取得してUIへ反映（大小文字やキー揺れ・複数エンドポイントに対応）"""
        if not self.token:
            return self._log("HTTP", "Token無し")

        base = self._base_url()
        headers = {"X-API-KEY": self.token}
        urls = [f"{base}/wallet/cash", f"{base}/wallet/margin", f"{base}/wallet/stock"]

        merged = {}
        merged_ci = {}  # case-insensitive view

        # --- 取得＆マージ ---
        for url in urls:
            try:
                r = requests.get(url, headers=headers, timeout=10)
                self._log("HTTP", f"GET {url} -> {r.status_code} {r.text[:200]}...")
                if not r.ok or not r.text:
                    continue
                j = r.json()
                if isinstance(j, dict):
                    # オリジナル/小文字の両方で保持
                    for k, v in j.items():
                        merged[k] = v
                        merged_ci[k.lower()] = v
            except Exception as e:
                self._log("HTTP", f"wallet get err: {e}")

        def pick_ci(keys, default=0.0):
            """キー候補から最初に見つかった数値を返す（大小文字無視）"""
            for k in keys:
                # そのまま
                if k in merged and merged[k] is not None:
                    try: return float(merged[k])
                    except: pass
                # 小文字
                lk = k.lower()
                if lk in merged_ci and merged_ci[lk] is not None:
                    try: return float(merged_ci[lk])
                    except: pass
            return default

        def sum_ci(keys):
            s = 0.0; hit = False
            for k in keys:
                lk = k.lower()
                val = None
                if k in merged and merged[k] is not None:
                    val = merged[k]
                elif lk in merged_ci and merged_ci[lk] is not None:
                    val = merged_ci[lk]
                if val is not None:
                    try: s += float(val); hit = True
                    except: pass
            return (s if hit else 0.0), hit

        # --- 金額の拾い出し ---
        # 現物余力(株式)
        stock_wallet = pick_ci([
            "StockAccountWallet", "StockAccountBalance", "StockBalance",
            "CashStock", "CashStockBalance"
        ], 0.0)
        # AuKC / AuJbn が来る環境では合算を優先
        aux_sum, aux_hit = sum_ci(["AuKCStockAccountWallet", "AuJbnStockAccountWallet"])
        if aux_hit:
            stock_wallet = aux_sum
            self._log("HTTP", f"wallet: using AuKC+AuJbn sum = {stock_wallet}")

        # 預り金/現金（該当キーが無ければ 0 のまま）
        cash_bank = pick_ci([
            "CashDeposits", "Cash", "Deposit", "AvailableAmount", "BankBalance",
            "Collateral.Cash"  # ネスト風のキーも一応見る
        ], 0.0)

        # 信用新規建可能額
        margin_avail = pick_ci([
            "MarginAvailable", "MarginAccountWallet", "MarginAccountBalance", "MarginAvail",
            "BuyingPower", "MarginBuyingPower"
        ], 0.0)

        # 委託保証金率（%）
        margin_rate = pick_ci([
            "ConsignmentDepositRate", "CashOfConsignmentDepositRate",
            "DepositKeepRate", "DepositkeepRate", "MarginRequirement", "RequiredMarginRate", "MarginRate"
        ], 0.0)

        # --- UI反映 ---
        self.ui_call(self.cash_stock_wallet.set, f"{int(stock_wallet):,}")
        self.ui_call(self.cash_bank.set,         f"{int(cash_bank):,}")
        self.ui_call(self.margin_wallet.set,     f"{int(margin_avail):,}")
        self.ui_call(self.margin_rate.set,       f"{margin_rate*100:.1f}%")



    def update_positions(self):
        if not self.token: return self._log("HTTP", "Token無し")
        url = self._base_url()+"/positions"
        try:
            r = requests.get(url, headers={"X-API-KEY": self.token}, timeout=10)
            if not r.ok: self._log("HTTP", f"/positions {r.status_code}"); return
            d = r.json() if isinstance(r.json(), list) else (r.json().get("Positions") or [])
            self.ui_call(self._fill_positions, d)
        except Exception as e:
            self._log_exc("HTTP", e)

    def _fill_positions(self, rows:List[Dict[str,Any]]):
        try:
            for iid in self.tree_pos.get_children(): self.tree_pos.delete(iid)
            for p in rows:
                sym=str(p.get("Symbol",""))
                name=self._resolve_symbol_name(sym) or str(p.get("SymbolName",""))
                side="買" if int(p.get("Side",2))==2 else "売"
                qty=int(float(p.get("Qty",0)))
                price=float(p.get("Price",0.0))
                pl=int(float(p.get("ProfitLoss",0.0)))
                self.tree_pos.insert("", "end", values=(sym,name,side,qty,price,pl))
        except Exception:
            pass

    def update_orders(self):
        if not self.token: return self._log("HTTP", "Token無し")
        url = self._base_url()+"/orders"
        try:
            r = requests.get(url, headers={"X-API-KEY": self.token}, timeout=10)
            if not r.ok: self._log("HTTP", f"/orders {r.status_code}"); return
            d = r.json() if isinstance(r.json(), list) else (r.json().get("Orders") or [])
            self.ui_call(self._fill_orders, d)
        except Exception as e:
            self._log_exc("HTTP", e)

    def _fill_orders(self, rows:List[Dict[str,Any]]):
        try:
            for iid in self.tree_ord.get_children(): self.tree_ord.delete(iid)
            for o in rows:
                sym=str(o.get("Symbol",""))
                name=self._resolve_symbol_name(sym) or str(o.get("SymbolName",""))
                side="買" if int(o.get("Side",2))==2 else "売"
                qty=int(float(o.get("Qty",0)))
                price=float(o.get("Price",0.0))
                st=str(o.get("State", o.get("Status","")))
                oid=str(o.get("ID", o.get("OrderId","")))
                self.tree_ord.insert("", "end", values=(oid,sym,name,side,qty,price,st))
        except Exception:
            pass

    def update_live_history(self):
        # /ordersを流用して一覧表示（発注時にも _append_live_history で追記）
        self.update_orders()

    def _append_live_history(self, order_id:Optional[str], side:str, price:float, qty:int, status:str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.tree_live.insert("", "end", values=(ts, order_id or "—", self.symbol.get().strip(),
                                                     self._resolve_symbol_name(self.symbol.get().strip()) or "", side, qty, price, status))
        except Exception:
            pass

    # --------- ファイル保存 ---------
    def save_hist_csv(self):
        try:
            path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
            if not path: return
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time","sym","side","qty","entry","exit","ticks","pnl","reason"])
                for row in self.sim_trades: w.writerow(row)
            self._log("FILE", f"SIM履歴を保存: {path}")
        except Exception as e:
            self._log_exc("FILE", e)

    def save_hist_xlsx(self):
        try:
            import xlsxwriter
        except Exception:
            self._log("FILE", "xlsxwriter未導入のためCSV保存をご利用ください"); return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel","*.xlsx")])
        if not path: return
        try:
            wb = xlsxwriter.Workbook(path); ws = wb.add_worksheet("SIM")
            cols=["time","sym","side","qty","entry","exit","ticks","pnl","reason"]
            for i,c in enumerate(cols): ws.write(0,i,c)
            for r,row in enumerate(self.sim_trades, start=1):
                for i,val in enumerate(row): ws.write(r,i,val)
            wb.close()
            self._log("FILE", f"SIM履歴を保存: {path}")
        except Exception as e:
            self._log_exc("FILE", e)

    def save_live_csv(self):
        try:
            path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
            if not path: return
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time","id","sym","name","side","qty","price","status"])
                for iid in self.tree_live.get_children():
                    w.writerow(self.tree_live.item(iid,"values"))
            self._log("FILE", f"LIVE履歴を保存: {path}")
        except Exception as e:
            self._log_exc("FILE", e)

    def save_live_xlsx(self):
        try:
            import xlsxwriter
        except Exception:
            self._log("FILE", "xlsxwriter未導入のためCSV保存をご利用ください"); return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel","*.xlsx")])
        if not path: return
        try:
            wb = xlsxwriter.Workbook(path); ws = wb.add_worksheet("LIVE")
            cols=["time","id","sym","name","side","qty","price","status"]
            for i,c in enumerate(cols): ws.write(0,i,c)
            for r,iid in enumerate(self.tree_live.get_children(), start=1):
                row=self.tree_live.item(iid,"values")
                for i,val in enumerate(row): ws.write(r,i,val)
            wb.close()
            self._log("FILE", f"LIVE履歴を保存: {path}")
        except Exception as e:
            self._log_exc("FILE", e)

    # ==============================
    # スクリーニング（簡易）
    # ==============================
    def update_preset_names(self):
        if not self.token: return self._log("HTTP", "Token無し")
        names = {}
        for code in PRESET_CODES:
            try:
                url=self._base_url()+f"/symbol/{code}@{EXCHANGE}"
                r=requests.get(url, headers={"X-API-KEY": self.token}, timeout=5)
                if r.ok:
                    d=r.json()
                    names[code] = d.get("SymbolName") or d.get("IssueName") or ""
                else:
                    names[code] = ""
            except Exception:
                names[code] = ""
        def _apply():
            self.list_preset.delete(0,"end")
            for c in PRESET_CODES:
                nm = names.get(c,"")
                self.list_preset.insert("end", f"{c} {nm}".strip())
        self.ui_call(_apply)

    def start_scan(self):
        if self.scan_running: return
        self.scan_running = True
        def run():
            self._log("SCAN", "start")
            while self.scan_running:
                # ダミー統計：push/minなど本実装は環境に合わせて
                states = {}
                now = time.time()
                ppm = sum(1 for t in self.push_times if t >= now - 60)
                for c in PRESET_CODES:
                    states[c] = {"name": self._resolve_symbol_name(c) or "",
                                 "tickr": f"{self.s_thr_tickrate.get():.2f}", "upd": ppm,
                                 "imbstd": f"{self.s_thr_imbstd.get():.2f}", "rev": f"{self.s_thr_revrate.get():.2f}",
                                 "tick": self.tick_size, "state": "観測中"}
                self.ui_call(self._fill_scan, states)
                time.sleep(3.0)
            self._log("SCAN", "stop")
        self.scan_thread = threading.Thread(target=run, daemon=True)
        self.scan_thread.start()

    def stop_scan(self):
        self.scan_running = False

    def _fill_scan(self, states:Dict[str,Dict[str,Any]]):
        try:
            for iid in self.tree_scan.get_children(): self.tree_scan.delete(iid)
            for code, st in states.items():
                self.tree_scan.insert("", "end",
                    values=(code, st.get("name",""), st.get("tickr",""), st.get("upd",""),
                            st.get("imbstd",""), st.get("rev",""), st.get("tick",""), st.get("state","")))
        except Exception:
            pass

    def set_main_from_scan_selection(self):
        try:
            sels = self.tree_scan.selection()
            if not sels: return
            v = self.tree_scan.item(sels[0], "values")
            code = str(v[0])
            self.symbol.set(code)
            self._log("SCAN", f"set main symbol: {code}")
        except Exception:
            pass

    def _open_help(self):
        """使い方/HELPの2言語タブを開く（UIは既存そのまま）"""
        try:
            if hasattr(self, "_help_win") and self._help_win and tk.Toplevel.winfo_exists(self._help_win):
                self._help_win.lift()
                return
        except Exception:
            pass

        win = tk.Toplevel(self)
        self._help_win = win
        win.title("使い方 / HELP")
        win.geometry("920x700")
        win.transient(self)
        win.grab_set()  # モーダル風

        nb = ttk.Notebook(win); nb.pack(fill="both", expand=True)

        # 日本語
        fja = ttk.Frame(nb); nb.add(fja, text="日本語")
        txt_ja = ScrolledText(fja, wrap="word", font=("Segoe UI", 10))
        txt_ja.pack(fill="both", expand=True)
        txt_ja.insert("end", self._help_text_ja())
        txt_ja.configure(state="disabled")

        # English
        fen = ttk.Frame(nb); nb.add(fen, text="English")
        txt_en = ScrolledText(fen, wrap="word", font=("Segoe UI", 10))
        txt_en.pack(fill="both", expand=True)
        txt_en.insert("end", self._help_text_en())
        txt_en.configure(state="disabled")

        btns = ttk.Frame(win); btns.pack(fill="x")
        ttk.Button(btns, text="閉じる / Close", command=win.destroy).pack(side="right", padx=8, pady=6)

    def _fmt_ticks(self, x):
        try:
            return f"{x/self.tick_size:.2f}t" if self.tick_size else f"{x:.3f}"
        except Exception:
            return str(x)

    def _help_text_ja(self) -> str:
        # 動的値を反映
        imb = globals().get("IMBALANCE_TH", 0.60)
        spmx = globals().get("SPREAD_TICKS_MAX", 1)
        cd = globals().get("SIGNAL_COOLDOWN_S", 2.0)
        momw = globals().get("MOM_WINDOW_S", 0.7) if "MOM_WINDOW_S" in globals() else 0.7

        return f"""\
        【概要 / Overview】
        本ツールは kabuステーション API の板・歩み値・簡易チャート・スクリーニング・SIM/LIVEを一体化したGUIです。
        UI操作はメインスレッドで実行し、WS/HTTPをバックグラウンドに分離して固まりにくくしています。

        【基本フロー】
        1) ①トークン取得 → 2) ②銘柄登録(/register) → 3) ③WS接続
        - /register 成功後は他銘柄を自動で /unregister し、購読を現行銘柄1本に収束。
        - 接続後に PUSH の銘柄不一致が20連続すると自動で再登録。
        2) スナップショットはHTTPで取得し、UI更新は after(0) 経由で安全に反映。

        【SIMと実発注】
        - 既定は SIM（紙トレード）。「AUTO ON」でシグナルに応じて SIM エントリー/決済。
        - 実発注ONにしても、セッションが ARMED されていなければ実弾は出ません（Ctrl+Shift+RでARM、F12でDISARM）。
        - 実弾の送信条件：本番(:18080) + 実発注ON + ARM済み + Token + /sendorder の Password あり。

        【画面タブの説明】
        - 板・歩み値: Best Ask/Bid, Spread, Imbalance、10本板、擬似歩み値（最新が上）。
        - 資金: /wallet/cash, /wallet/margin から現物余力・預り金・信用可能額・保証金率を表示。
        - 建玉/注文/LIVE履歴: 簡易表示（/positions, /orders）。
        - SIM履歴: 手仕舞い毎に記録。CSV/XLSX保存可。
        - スクリーニング: プリセット銘柄に対し更新頻度等の簡易指標を表示（開発用のダミー指標含む）。

        【用語 / 指標】
        - Qty（Quantity）: 数量。板の各段の出来高/注文数量、注文数量、建玉数量などを指します。
        - Spread: BestAsk - BestBid（負の場合は板反転の可能性）。
        - inv: 「inverted（反転）」= 一時的に Ask < Bid の異常。板崩れ/更新順により瞬間的に出ます。
        - Imbalance: (BidQty - AskQty) / (BidQty + AskQty)。+1に近いほど買い偏り。現在の閾値 = {imb:.2f}
        - Momentum（短期）: 直近 {momw:.2f}s の価格差。トリガ補助に使用。
        - Microprice: (Ask*BidQty + Bid*AskQty) / (BidQty + AskQty)。気配の重心。

        【AUTO（自動シグナル）】
        - 前提：直近PUSHが新鮮、板/価格が揃っている、ポジション未保有。
        - 条件：Spread ≤ {spmx} tick かつ |Imbalance| ≥ {imb:.2f} を中心に、短期モメンタムで補助判断。
        - クールダウン：{cd:.2f} 秒（連続発注を抑制）。
        - 補助フィルタ（任意ON/OFF）：VWAP, SMA25(5m), MACD(12,26,9), RSI(14), Swing。
        - MLゲート（任意）：学習済みモデルで GO/NOGO を判定（モデルが無い環境では無効化）。

        【ホットキー（SIM専用/実発注は呼びません）】
        - Ctrl + B … 手動で BUY エントリー（SIM）
        - Ctrl + S … 手動で SELL エントリー（SIM）
        - Ctrl + E … 現在のポジションを成行相当で決済（BUY→Bid, SELL→Ask, 無ければlast）
        - Ctrl + Shift + E … 反転（決済→反対側に即エントリー）
        - Ctrl + D … AUTOデバッグログのON/OFF（見送り理由の簡易表示）
        - Ctrl + Shift + R … 実発注 ARM（最終確認の上、このセッションで許可）
        - F12 … DISARM（実発注の即時無効化）
        ※ ホットキーはメインウィンドウがアクティブな状態で有効。

        【よくある質問】
        Q. シグナルが少ない/出ない  
        A. フィルタを一旦OFF、IMBALANCE_TH を下げる（例 0.45→0.35）、SPREAD許容を広げる（1→2/3）。主力の流動性が高い銘柄を使うと出やすいです。

        Q. Spread/Imbalance が更新されない  
        A. PUSHが来ているか（右上 pushes=カウンタ）を確認。Qty=0でもImbalanceは計算されます。Ask<Bid の瞬間は inv 表示に。

        Q. 実弾に切り替えるには？  
        A. 本番(:18080) + 実発注ON + Ctrl+Shift+RでARM + /sendorder で Password を付与。

        【ログのタグ】
        [HTTP] REST、[WS] WebSocket、[ML] モデル、[AUTO] 自動シグナル、[SIM] 紙トレ、[LOOP] メインループなど。
        同系メッセージは5秒抑制、巨大JSONは200文字まで表示。

        【安全設計】
        - TkのUI更新は after(0) でメインスレッドに集約。
        - メインループは1ティック最大200件処理→after(100)で再スケジュール。
        - チャート再描画は最短200msにレート制限。
        - 例外は「要旨+末尾スタック1行」をログへ、UIは落とさない。
        """

    def _help_text_en(self) -> str:
        imb = globals().get("IMBALANCE_TH", 0.60)
        spmx = globals().get("SPREAD_TICKS_MAX", 1)
        cd = globals().get("SIGNAL_COOLDOWN_S", 2.0)
        momw = globals().get("MOM_WINDOW_S", 0.7) if "MOM_WINDOW_S" in globals() else 0.7

        return f"""\
    [Overview]
    A unified GUI for kabu Station API: order book (DOM), tape, lightweight chart, screening, and SIM/LIVE.
    UI actions are confined to Tk main thread; HTTP/WS run in background threads to avoid freezes.

    [Basic flow]
    1) Get Token → 2) Register symbol (/register) → 3) Connect WS.
    After /register, extra symbols in RegistList are auto-/unregister'ed to converge to the current symbol.
    If WS pushes for a different symbol persist (≥20), auto re-register.
    2) Snapshots are fetched via HTTP; UI updates via Tk.after(0).

    [SIM vs. LIVE]
    - Default is SIM. Turn "AUTO ON" to let the strategy open/close SIM positions.
    - Even if "Real Trade ON" is checked, **no live order** unless session is **ARMED** (Ctrl+Shift+R). F12 DISARMs.
    - To actually send orders: Production (:18080) + Real Trade ON + ARMED + Token + /sendorder with Password.

    [Tabs]
    - Board/Tape: Best quotes, Spread, Imbalance, 10-level DOM, pseudo-tape (latest on top).
    - Wallet: Shows stock cash, cash deposits, margin buying power, margin rate from /wallet/cash and /wallet/margin.
    - Positions / Orders / LIVE history: simple lists from /positions, /orders.
    - SIM history: records each close; export CSV/XLSX.
    - Screening: simple per-symbol stats for preset codes (demo indicators included).

    [Terms]
    - Qty (Quantity): size/amount (DOM quantities, order size, position size).
    - Spread: BestAsk - BestBid (negative → inverted book).
    - inv: inverted; temporary Ask < Bid due to book collapse/update ordering.
    - Imbalance: (BidQty - AskQty) / (BidQty + AskQty). Current threshold = {imb:.2f}
    - Momentum (short): price delta over last {momw:.2f}s, used as an auxiliary trigger.
    - Microprice: (Ask*BidQty + Bid*AskQty)/(BidQty + AskQty).

    [AUTO (signal generation)]
    - Prereqs: fresh push, valid best quotes/last, no open position.
    - Core conditions: Spread ≤ {spmx} tick(s), |Imbalance| ≥ {imb:.2f}, plus short momentum.
    - Cooldown: {cd:.2f}s to avoid rapid re-entries.
    - Optional filters: VWAP, SMA25(5m), MACD(12,26,9), RSI(14), Swing.
    - ML gate (optional): external model to decide GO/NOGO.

    [Hotkeys (SIM only; never sends live orders)]
    - Ctrl + B … manual BUY entry (SIM)
    - Ctrl + S … manual SELL entry (SIM)
    - Ctrl + E … close current SIM position at market-like price (BUY→Bid, SELL→Ask, fallback last)
    - Ctrl + Shift + E … reverse (close → open the opposite)
    - Ctrl + D … toggle AUTO debug logs (why a signal was skipped)
    - Ctrl + Shift + R … ARM real-trade for this session (after confirmation)
    - F12 … DISARM real-trade immediately

    [FAQ]
    - Few/no signals → temporarily disable filters, lower IMBALANCE_TH (e.g., 0.45→0.35), allow wider SPREAD (1→2/3),
    and choose liquid symbols (tight spread, continuous prints).
    - Spread/Imbalance not updating → ensure WS pushes (see 'pushes=' counter). With zero Qty, Imbalance shows '—' until total>0.

    [Logging]
    Tags: [HTTP]/[WS]/[ML]/[AUTO]/[SIM]/[LOOP], 5s de-dup, long JSON is truncated to 200 chars.

    [Safety]
    UI ops via Tk.after(0). Main loop caps 200 msgs/tick then reschedules. Chart redraw rate-limited (≥200ms).
    Exceptions are logged (summary + last traceback line) without killing the GUI.
    """


    # --------- 雑 ---------
    def _resolve_symbol_name(self, code:str) -> Optional[str]:
        return self.symbol_name_cache.get(code)

    def _on_close(self):
        self.ws_should_reconnect=False
        try:
            if self.ws: self.ws.close()
        except Exception:
            pass
        self.scan_running=False
        try:
            if self.train_f: self.train_f.flush(); self.train_f.close()
        except Exception:
            pass
        self.destroy()

def _parse_cli_args():
    import argparse, os
    p = argparse.ArgumentParser(description="Kabus GUI 起動オプション")
    # プリセット（日本語/英語どちらでもOK）
    p.add_argument("--preset", choices=["標準","高ボラ","低ボラ","std","volatile","calm"], help="パラメータプリセット")
    # 主要パラメータ
    p.add_argument("--symbol", help="初期銘柄（例: 7203@1）")
    p.add_argument("--qty", type=int, help="数量（株）")
    p.add_argument("--tp", type=int, help="利確 tick")
    p.add_argument("--sl", type=int, help="損切 tick")
    p.add_argument("--imb", type=float, help="Imbalance 閾値（例 0.35）")
    p.add_argument("--cooldown", type=int, help="クールダウン ms（例 400）")
    p.add_argument("--spread", type=int, help="許容スプレッド上限（tick）")
    p.add_argument("--size-ratio", type=float, help="Best枚数に対する使用率（0.1〜1.0）")
    # フラグ
    p.add_argument("--production", action="store_true", help="本番(:18080)で起動")
    p.add_argument("--sandbox", action="store_true", help="検証(:18081)で起動（デフォルト）")
    p.add_argument("--real", action="store_true", help="実発注ONで起動（ARMしない限り送信しません）")
    p.add_argument("--ml", choices=["on","off"], help="MLゲート 有効/無効")
    p.add_argument("--debug", action="store_true", help="デバッグ（意思決定ログ）ON")
    p.add_argument("--auto-start", action="store_true", help="起動時に ①トークン→②登録→③WS→④スナップショット まで自動実行")
    # パスワード（コマンドライン直書きは推奨しません。環境変数経由推奨）
    p.add_argument("--api-pass", help="APIパスワード（推奨しない）")
    p.add_argument("--api-pass-env", default="KABU_API_PASSWORD", help="APIパスワードを読む環境変数名（既定: KABU_API_PASSWORD）")
    args = p.parse_args()

    # パスワード解決：--api-pass が無ければ環境変数を読む
    if not getattr(args, "api_pass", None):
        env_name = args.api_pass_env or "KABU_API_PASSWORD"
        args.api_pass = os.environ.get(env_name, "")
    return args

def main():
    # 既存: App クラスはそのまま利用
    args = _parse_cli_args()
    app = App()  # App.__init__ 内で _build_ui() 済の前提

    # 起動オプションを適用
    try:
        app._apply_startup_options(args)
    except Exception as e:
        app._log("CFG", f"起動オプション適用エラー: {e}")

    # 自動起動（トークン→登録→WS→スナップショット）
    if getattr(args, "auto_start", False):
        import threading
        threading.Thread(target=app._boot_seq, daemon=True).start()

    app.mainloop()

if __name__ == "__main__":
    main()
