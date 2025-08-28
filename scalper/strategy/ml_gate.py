# scalper/strategy/ml_gate.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from scalper.core.types import OrderIntent, Decision


@dataclass
class MLGateConfig:
    enabled: bool = False                 # 最初はOFF。あとで学習済モデルを載せる
    min_prob: float = 0.55                # P(TP先行) の最小値
    min_ev_ticks: float = 0.0             # 期待値（tick）の最小
    cost_ticks: float = 0.10              # 手数料・スリッページ等の総コスト（tick換算）
    # モデル読み込み用
    model_path: Optional[str] = None


class MLGate:
    """
    MLによる GO/NOGO 評価。
    - モデルが無い場合は簡易ロジスティックにより確率を生成（ヒューリスティック）
    - EV = P*TP - (1-P)*SL - cost で最終判定
    """
    def __init__(self, cfg: MLGateConfig, proba_fn: Optional[Callable[[Dict], float]] = None) -> None:
        self.cfg = cfg
        self._external_proba = proba_fn
        # 本来は joblib で model_path を読む。ここでは外部依存を避けて proba_fn のみ対応。

    # ---- 簡易スコア（モデルが無い場合の代替） ----
    def _heuristic_proba(self, feats: Dict) -> float:
        # 互換キーを吸収
        imb   = feats.get("imbalance")
        if imb is None:
            imb = feats.get("imb", 0.0)

        spread = feats.get("spread") or 0.0
        tick = feats.get("tick_size")
        if tick is None:
            tick = feats.get("tick", 0.5)
        spread_ticks = spread / max(tick, 1e-9)

        macd = feats.get("macd")
        macd_sig = feats.get("macd_sig")
        macd_diff = 0.0 if macd is None or macd_sig is None else (macd - macd_sig)

        rsi = feats.get("rsi14")
        if rsi is None:
            rsi = feats.get("rsi")  # GUI側は 'rsi'
        rsi_term = 0.0 if rsi is None else (0.5 - abs(0.5 - min(max(rsi / 100.0, 0.0), 1.0)))

        push = feats.get("pushes_per_min")
        if push is None:
            push = feats.get("upd_per_min", 0.0)  # スクリーナの「更新/分」を流用可

        last = feats.get("last") or 0.0
        vwap = feats.get("vwap")
        vwap_diff_ticks = 0.0 if vwap is None else (last - vwap) / max(tick, 1e-9)

        z = (
            1.2 * (imb or 0.0)
            - 0.8 * max(0.0, spread_ticks - 0.5)
            + 0.3 * macd_diff
            + 0.2 * rsi_term
            + 0.02 * (push or 0.0)
            + 0.15 * vwap_diff_ticks
        )
        p = 1.0 / (1.0 + math.exp(-z))
        return float(max(0.01, min(0.99, p)))

    def evaluate(self, intent: OrderIntent, feats: Dict) -> Decision:
        if not self.cfg.enabled:
            # MLゲート無効なら通す（EV=推定0）
            return Decision(go=True, prob_tp_first=0.5, ev_ticks=0.0, reason="ml_disabled")

        # 確率
        if self._external_proba:
            try:
                p = float(self._external_proba(feats))
            except Exception:
                p = 0.5
        else:
            p = self._heuristic_proba(feats)

        tp = max(1, int(intent.tp_ticks))
        sl = max(1, int(intent.sl_ticks))
        ev = p * tp - (1.0 - p) * sl - float(self.cfg.cost_ticks)

        go = bool((p >= self.cfg.min_prob) and (ev >= self.cfg.min_ev_ticks))
        reason = f"p={p:.2f} ev={ev:.2f} tp/sl={tp}/{sl}"
        return Decision(go=go, prob_tp_first=p, ev_ticks=ev, reason=reason)
