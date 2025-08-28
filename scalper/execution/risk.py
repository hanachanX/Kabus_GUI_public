# scalper/execution/risk.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskConfig:
    # 基本
    max_pos_qty: int = 1000            # 建玉上限（合計数量）
    max_consec_losses: int = 5         # 連続損失の上限でストップ
    cooldown_sec: float = 2.0          # エントリー後のクールダウン
    enforce_market_hours: bool = False # Trueなら市場時間のみ許可（簡易チェック）
    daytrade_only: bool = True         # 実弾時はデイのみ（建玉オーバーナイト禁止）

    # ソフトリミット（ログ警告）
    warn_drawdown_ticks: float = 50.0  # 日中ドローダウン警告（tick換算）


class RiskManager:
    """
    発注前の安全チェックと、結果に応じた状態更新を行う。
    """
    def __init__(self, cfg: RiskConfig, tick_size: float = 0.5) -> None:
        self.cfg = cfg
        self.tick_size = tick_size

        self._open_qty: int = 0
        self._last_entry_ts: float = 0.0
        self._consec_losses: int = 0
        self._day_pnl_ticks: float = 0.0

    # ---- 市場時間（簡易、JSTの寄り付き/引け前後のみの大まか判定） ----
    def _is_market_open(self, now_ts: Optional[float] = None) -> bool:
        if not self.cfg.enforce_market_hours:
            return True
        t = time.localtime(now_ts or time.time())
        # 土日NG
        if t.tm_wday >= 5:
            return False
        # 9:00-11:30, 12:30-15:00（JST想定）
        hm = t.tm_hour * 100 + t.tm_min
        return (900 <= hm < 1130) or (1230 <= hm < 1500)

    # ---- チェック ----
    def can_enter(self, qty: int, now_ts: Optional[float] = None) -> tuple[bool, str]:
        if not self._is_market_open(now_ts):
            return False, "market_closed"
        if qty <= 0:
            return False, "qty<=0"
        if self._open_qty + qty > self.cfg.max_pos_qty:
            return False, "pos_limit"
        if self._consec_losses >= self.cfg.max_consec_losses:
            return False, "too_many_losses"
        if (now_ts or time.time()) - self._last_entry_ts < self.cfg.cooldown_sec:
            return False, "cooldown"
        return True, "ok"

    # 実弾運用時のデイ限定チェックは、建玉保有時に日付跨ぎを拒否するなどの実装側責務とする
    def can_exit(self) -> tuple[bool, str]:
        return True, "ok"

    # ---- 更新（約定結果を反映） ----
    def on_entry_filled(self, qty: int) -> None:
        self._open_qty += qty
        self._last_entry_ts = time.time()

    def on_exit_filled(self, qty: int, pnl_ticks: float) -> None:
        self._open_qty = max(0, self._open_qty - qty)
        self._day_pnl_ticks += pnl_ticks
        if pnl_ticks < 0:
            self._consec_losses += 1
        else:
            self._consec_losses = 0

    def daily_pnl_ticks(self) -> float:
        return self._day_pnl_ticks

    def state_snapshot(self) -> dict:
        return {
            "open_qty": self._open_qty,
            "consec_losses": self._consec_losses,
            "day_pnl_ticks": self._day_pnl_ticks,
        }
