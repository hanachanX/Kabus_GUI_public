# scalper/core/types.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Dict, List


Side = Literal["BUY", "SELL"]
EntryType = Literal["LIMIT", "MARKET"]


@dataclass
class OrderIntent:
    side: Side
    qty: int
    entry_type: EntryType
    price: Optional[float]
    tp_ticks: int
    sl_ticks: int
    trail: bool
    trail_trigger: int
    trail_gap: int
    meta: Dict = field(default_factory=dict)


@dataclass
class Decision:
    go: bool
    prob_tp_first: float = 0.5
    ev_ticks: float = 0.0
    reason: str = ""


@dataclass
class Bar5m:
    """5分足の1本"""
    ts_minute: int  # epoch(秒)//60*5 のような5分境界（またはISO文字列でも可）
    o: float
    l: float
    h: float
    c: float


@dataclass
class MarketSnapshot:
    """戦略用の簡易スナップショット（必要に応じて拡張）"""
    symbol: str
    last_price: Optional[float]
    prev_close: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    bid_qty: Optional[float]
    ask_qty: Optional[float]
    spread: Optional[float]
    imbalance: Optional[float]
    microprice: Optional[float]
    vwap: Optional[float]
    pushes_per_min: float
    bars_5m: List[Bar5m] = field(default_factory=list)
    sma25: Optional[float] = None
    macd: Optional[float] = None
    macd_sig: Optional[float] = None
    rsi14: Optional[float] = None
    swing_higher_lows: Optional[bool] = None
    swing_lower_highs: Optional[bool] = None

    def to_features(self) -> Dict:
        """MLやルール判定に渡すための辞書化"""
        return {
            "last": self.last_price,
            "prev_close": self.prev_close,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_qty": self.bid_qty,
            "ask_qty": self.ask_qty,
            "spread": self.spread,
            "imbalance": self.imbalance,
            "microprice": self.microprice,
            "vwap": self.vwap,
            "pushes_per_min": self.pushes_per_min,
            "sma25": self.sma25,
            "macd": self.macd,
            "macd_sig": self.macd_sig,
            "rsi14": self.rsi14,
            "swing_higher_lows": self.swing_higher_lows,
            "swing_lower_highs": self.swing_lower_highs,
        }
