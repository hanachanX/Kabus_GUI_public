# scalper/strategy/policy.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from scalper.core.bus import EventBus
from scalper.core.types import MarketSnapshot, Decision
from scalper.strategy.rules import RuleBasedStrategy, RuleConfig
from scalper.strategy.ml_gate import MLGate, MLGateConfig


@dataclass
class PolicyConfig:
    tick_size: float = 0.5
    loop_interval_sec: float = 0.10       # 100ms
    publish_topic_intent: str = "strategy.intent"
    publish_topic_decision: str = "strategy.decision"
    publish_topic_debug: str = "strategy.debug"


class StrategyPolicy:
    """
    ルールベース + MLゲートを束ね、一定周期で「発注意図」を生成してバスにpublishする。
    - snapshot_provider: () -> MarketSnapshot（IndicatorEngine.snapshot を想定）
    - recent_return_provider: (sec: float) -> float（IndicatorEngine.get_return を想定）
    """
    def __init__(
        self,
        bus: EventBus,
        policy_cfg: PolicyConfig,
        rule_cfg: RuleConfig,
        ml_cfg: MLGateConfig,
        snapshot_provider: Callable[[], MarketSnapshot],
        recent_return_provider: Optional[Callable[[float], float]] = None,
    ) -> None:
        self.bus = bus
        self.cfg = policy_cfg
        self.rules = RuleBasedStrategy(rule_cfg)
        self.ml = MLGate(ml_cfg)
        self.snapshot_provider = snapshot_provider
        self.recent_return_provider = recent_return_provider

        self._stop = False
        self._th: Optional[threading.Thread] = None

    # ---- ランループ ----
    def start(self) -> None:
        if self._th and self._th.is_alive():
            return
        self._stop = False
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop = True
        if self._th:
            self._th.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop:
            t0 = time.time()
            try:
                snap = self.snapshot_provider()
                feats = snap.to_features()
                feats["tick_size"] = self.rules.cfg.tick_size

                # 直近リターン（tick換算）
                if self.recent_return_provider:
                    ret_px = float(self.recent_return_provider(self.rules.cfg.recent_return_sec))
                    feats["recent_return_ticks"] = ret_px / max(self.rules.cfg.tick_size, 1e-9)
                else:
                    feats["recent_return_ticks"] = None

                # ルールで提案
                intent = self.rules.propose(snap, feats)
                if intent is None:
                    self.bus.publish(self.cfg.publish_topic_debug, {
                        "ts": t0, "note": "no_intent",
                        "spread": feats.get("spread"), "imb": feats.get("imbalance"),
                        "pushes_per_min": feats.get("pushes_per_min"),
                    })
                else:
                    # MLゲートでGO/NOGO
                    dec: Decision = self.ml.evaluate(intent, feats)
                    self.bus.publish(self.cfg.publish_topic_decision, {
                        "ts": t0,
                        "go": dec.go,
                        "prob": dec.prob_tp_first,
                        "ev_ticks": dec.ev_ticks,
                        "reason": dec.reason,
                        "reasons_intent": intent.meta.get("reasons") if intent.meta else None,
                    })
                    if dec.go:
                        self.bus.publish(self.cfg.publish_topic_intent, {
                            "ts": t0,
                            "intent": {
                                "side": intent.side,
                                "qty": intent.qty,
                                "entry_type": intent.entry_type,
                                "price": intent.price,
                                "tp_ticks": intent.tp_ticks,
                                "sl_ticks": intent.sl_ticks,
                                "trail": intent.trail,
                                "trail_trigger": intent.trail_trigger,
                                "trail_gap": intent.trail_gap,
                            },
                            "features": feats,
                        })
            except Exception as e:
                self.bus.publish(self.cfg.publish_topic_debug, {"ts": time.time(), "error": str(e)})

            # インターバル調整
            dt = time.time() - t0
            time.sleep(max(0.0, self.cfg.loop_interval_sec - dt))
