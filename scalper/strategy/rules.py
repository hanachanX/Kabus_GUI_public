# scalper/strategy/rules.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from scalper.core.types import OrderIntent, MarketSnapshot


@dataclass
class RuleConfig:
    # --- 参戦条件 ---
    tick_size: float = 0.5
    spread_ticks_max: int = 1          # スプレッドがこのtick以内
    imbalance_th: float = 0.60         # |imbalance| がこの閾値以上
    pushes_per_min_min: int = 40       # 更新/分（執行品質の粗い目安）

    # --- テクニカル・フィルタ（任意で有効化） ---
    use_vwap_filter: bool = True       # BUYは価格>=VWAP、SELLは価格<=VWAP
    use_sma25_filter: bool = False     # BUYは価格>=SMA25、SELLは価格<=SMA25
    use_macd_filter: bool = False      # BUY: MACD>=Signal / SELL: MACD<=Signal
    use_rsi_filter: bool = False       # BUY: RSI<=70 / SELL: RSI>=30（極端な逆張りを避ける）
    # 直近リターン（逆行率）フィルタ：BUY時は直近retがマイナス過大でない、SELL時は直近retがプラス過大でない
    use_recent_return_filter: bool = True
    recent_return_sec: float = 0.7
    recent_return_abs_max: float = 1.5  # tick単位でこの範囲以内（極端な逆行は回避）

    # --- エントリ/決済 ---
    default_qty: int = 100
    entry_type: str = "LIMIT"          # "LIMIT" or "MARKET"
    tp_ticks: int = 3
    sl_ticks: int = 2
    trail_enabled: bool = True
    trail_trigger: int = 2
    trail_gap: int = 1

    # --- ログ ---
    reason_detail: bool = True


class RuleBasedStrategy:
    """
    ルールベースのシグナル→発注意図（OrderIntent）生成器
    """
    def __init__(self, config: RuleConfig) -> None:
        self.cfg = config

    def _spread_ok(self, spread: Optional[float]) -> bool:
        if spread is None:
            return False
        if self.cfg.tick_size <= 0:
            return False
        ticks = spread / self.cfg.tick_size
        return ticks <= self.cfg.spread_ticks_max + 1e-9

    def _recent_return_ok(self, features: Dict, side: str) -> bool:
        """
        features['recent_return'] が tick単位で極端でないことを確認
        BUY: 極端なプラス逆行（上に走ってしまっている）を避ける
        SELL: 極端なマイナス逆行（下に走ってしまっている）を避ける
        """
        if not self.cfg.use_recent_return_filter:
            return True
        r = features.get("recent_return_ticks")
        if r is None:
            return True
        # 範囲チェック
        if abs(r) > self.cfg.recent_return_abs_max:
            return False
        # サイド別の「逆行しすぎ」抑制
        if side == "BUY" and r > self.cfg.recent_return_abs_max * 0.8:
            return False
        if side == "SELL" and r < -self.cfg.recent_return_abs_max * 0.8:
            return False
        return True

    def _apply_technicals(self, snap: MarketSnapshot, side: str) -> bool:
        # VWAP
        if self.cfg.use_vwap_filter and snap.vwap is not None and snap.last_price is not None:
            if side == "BUY" and not (snap.last_price >= snap.vwap):
                return False
            if side == "SELL" and not (snap.last_price <= snap.vwap):
                return False
        # SMA25
        if self.cfg.use_sma25_filter and snap.sma25 is not None and snap.last_price is not None:
            if side == "BUY" and not (snap.last_price >= snap.sma25):
                return False
            if side == "SELL" and not (snap.last_price <= snap.sma25):
                return False
        # MACD
        if self.cfg.use_macd_filter and snap.macd is not None and snap.macd_sig is not None:
            if side == "BUY" and not (snap.macd >= snap.macd_sig):
                return False
            if side == "SELL" and not (snap.macd <= snap.macd_sig):
                return False
        # RSI
        if self.cfg.use_rsi_filter and snap.rsi14 is not None:
            if side == "BUY" and not (snap.rsi14 <= 70.0):
                return False
            if side == "SELL" and not (snap.rsi14 >= 30.0):
                return False
        return True

    def propose(self, snap: MarketSnapshot, features: Dict) -> Optional[OrderIntent]:
        """
        ルールに合致すれば OrderIntent を返す。合致しない場合は None。
        features には、少なくとも recent_return_ticks を含められるとベター。
        """
        reasons: List[str] = []
        # 必須値チェック
        if snap.best_bid is None or snap.best_ask is None:
            return None

        # 1) 流動性・スプレッド
        if not self._spread_ok(snap.spread):
            return None
        reasons.append(f"spread_ok({snap.spread})")

        if snap.pushes_per_min < self.cfg.pushes_per_min_min:
            return None
        reasons.append(f"pushrate_ok({snap.pushes_per_min:.0f}/min)")

        # 2) 板の不均衡
        if snap.imbalance is None:
            return None
        imb = float(snap.imbalance)
        side: Optional[str] = None
        if imb >= self.cfg.imbalance_th:
            side = "BUY"
            reasons.append(f"imb>=th({imb:.2f})")
        elif imb <= -self.cfg.imbalance_th:
            side = "SELL"
            reasons.append(f"imb<=-th({imb:.2f})")
        else:
            return None

        # 3) 直近リターン（逆行率）フィルタ
        if not self._recent_return_ok(features, side):
            return None
        reasons.append("recent_return_ok")

        # 4) テクニカル・フィルタ
        if not self._apply_technicals(snap, side):
            return None
        reasons.append("technicals_ok")

        # 5) エントリー条件
        price = None
        entry_type = self.cfg.entry_type.upper()
        if entry_type == "LIMIT":
            price = snap.best_bid if side == "BUY" else snap.best_ask
        elif entry_type == "MARKET":
            price = None  # 成行
        else:
            entry_type = "MARKET"

        intent = OrderIntent(
            side=side,
            qty=self.cfg.default_qty,
            entry_type=entry_type,  # "LIMIT" or "MARKET"
            price=price,
            tp_ticks=self.cfg.tp_ticks,
            sl_ticks=self.cfg.sl_ticks,
            trail=self.cfg.trail_enabled,
            trail_trigger=self.cfg.trail_trigger,
            trail_gap=self.cfg.trail_gap,
            meta={"reasons": reasons} if self.cfg.reason_detail else {},
        )
        return intent
