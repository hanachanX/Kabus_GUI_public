# scalper/execution/position_tracker.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Position:
    symbol: str
    side: str                 # "BUY" or "SELL"
    qty: int
    entry_px: float
    entry_ts: float
    tp_ticks: int
    sl_ticks: int
    trail: bool
    trail_trigger: int
    trail_gap: int
    # 運用中のトレール水準
    peak_ticks: int = 0       # 利が最大で何tick乗ったか
    trail_stop_px: Optional[float] = None


@dataclass
class Fill:
    symbol: str
    side: str
    qty: int
    price: float
    ts: float
    kind: str  # "ENTRY" | "EXIT_TP" | "EXIT_SL" | "EXIT_TRAIL" | "EXIT_MANUAL"


@dataclass
class Ledger:
    positions: List[Position] = field(default_factory=list)
    fills: List[Fill] = field(default_factory=list)
    realized_pnl_ticks: float = 0.0

    def add_position(self, p: Position) -> None:
        self.positions.append(p)

    def record_fill(self, f: Fill, tick_size: float) -> float:
        self.fills.append(f)
        # EXIT のときに実現損益を更新（同数量・1建玉ずつを前提に簡易計算）
        if f.kind.startswith("EXIT"):
            # 対応する建玉をpop
            # 同銘柄＆サイド反対の直近エントリを対象にする（FIFOでも良い）
            idx = None
            for i in range(len(self.positions) - 1, -1, -1):
                pos = self.positions[i]
                if pos.symbol == f.symbol:
                    idx = i
                    break
            if idx is not None:
                pos = self.positions.pop(idx)
                mult = 1 if pos.side == "BUY" else -1
                pnl_px = (f.price - pos.entry_px) * mult
                pnl_ticks = pnl_px / max(tick_size, 1e-9)
                self.realized_pnl_ticks += pnl_ticks
                return pnl_ticks
        return 0.0

    def snapshot(self) -> dict:
        return {
            "open_count": len(self.positions),
            "fills": len(self.fills),
            "realized_pnl_ticks": self.realized_pnl_ticks,
        }
