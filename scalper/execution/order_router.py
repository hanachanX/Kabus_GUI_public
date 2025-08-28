# scalper/execution/order_router.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from scalper.core.bus import EventBus
from scalper.core.types import OrderIntent
from scalper.execution.position_tracker import Position, Fill, Ledger
from scalper.execution.risk import RiskManager, RiskConfig
from scalper.market.kabus_client import KabuSClient


@dataclass
class LiveConfig:
    tick_size: float = 0.5
    topic_intent: str = "strategy.intent"
    topic_best: str = "best"
    topic_exec_fill: str = "exec.fill"
    topic_exec_pos: str = "exec.position"
    topic_exec_log: str = "exec.log"

    # kabuS (実弾)
    live_enabled: bool = False          # ★安全のため既定は False（ONにしない限り発注しません）
    production: bool = True             # kabuS 本番(18080)/検証(18081)
    symbol: str = "7203"
    exchange: int = 1
    account_type: int = 4               # 特定
    cash_margin: int = 2                # 信用
    margin_trade_type: int = 3          # 一般デイトレ（日計り）
    api_password: Optional[str] = None  # トークン取得用


class LiveRouter:
    """
    実弾ルータ（最小版）
    - intent を受けたら kabuS にエントリーを1発出す（LIMIT→最良気配、MARKET→成り）
    - TP/SL/Trail はクライアント側で監視し、条件到達で反対成行を出す（簡易OCO/トレール）
    """
    def __init__(self, bus: EventBus, cfg: LiveConfig, risk: Optional[RiskManager] = None) -> None:
        self.bus = bus
        self.cfg = cfg
        self.risk = risk or RiskManager(RiskConfig(), tick_size=cfg.tick_size)

        self.ledger = Ledger()
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None

        # kabuS
        self.cli: Optional[KabuSClient] = None
        if self.cfg.api_password:
            self.cli = KabuSClient(api_password=self.cfg.api_password, production=self.cfg.production, logger=self._print)
            try:
                self.cli.get_token()
            except Exception as e:
                self._log(f"[LIVE] token error: {e}")

        # 購読
        bus.subscribe(cfg.topic_intent, self._on_intent)
        bus.subscribe(cfg.topic_best, self._on_best)

    # ---- イベント ----
    def _on_best(self, ev: dict) -> None:
        b = ev.get("bid"); a = ev.get("ask")
        if b is not None:
            self.best_bid = float(b)
        if a is not None:
            self.best_ask = float(a)
        self._evaluate_exits_live()

    def _on_intent(self, ev: dict) -> None:
        if not self.cfg.live_enabled:
            self._log("[LIVE] live_enabled=False → 実発注しません")
            return
        if self.cli is None or self.cli.token is None:
            self._log("[LIVE] kabuS token not ready")
            return

        data = ev.get("intent") or {}
        side = str(data.get("side", "")).upper()
        qty = int(data.get("qty", 0))
        entry_type = str(data.get("entry_type", "LIMIT")).upper()
        price = data.get("price")
        tp = int(data.get("tp_ticks", 3))
        sl = int(data.get("sl_ticks", 2))
        trail = bool(data.get("trail", True))
        trigger = int(data.get("trail_trigger", 2))
        gap = int(data.get("trail_gap", 1))

        ok, reason = self.risk.can_enter(qty)
        if not ok:
            self._log(f"[LIVE] entry rejected: {reason}")
            return

        # エントリ価格（LIMITなら最良、MARKETはNone）
        if entry_type == "LIMIT":
            if side == "BUY":
                price = self.best_bid if price is None else price
                front = 20  # 指値
            else:
                price = self.best_ask if price is None else price
                front = 20
        else:
            price = None
            front = 10  # 成行

        # kabuSへ発注
        try:
            resp = self.cli.place_simple_entry(
                code=self.cfg.symbol,
                side=side,
                qty=qty,
                price=price,
                front_order_type=front,
                exchange=self.cfg.exchange,
                account_type=self.cfg.account_type,
                cash_margin=self.cfg.cash_margin,
                margin_trade_type=self.cfg.margin_trade_type,
            )
            self._log(f"[LIVE] entry sent: {resp}")
        except Exception as e:
            self._log(f"[LIVE] send error: {e}")
            return

        # ローカル側でも追跡（TP/SL/Trail のクライアント実行）
        pos = Position(
            symbol=self.cfg.symbol, side=side, qty=qty,
            entry_px=(self.best_ask if side == "BUY" else self.best_bid) if price is None else float(price),
            entry_ts=time.time(),
            tp_ticks=tp, sl_ticks=sl, trail=trail, trail_trigger=trigger, trail_gap=gap
        )
        self.ledger.add_position(pos)
        self.risk.on_entry_filled(qty)
        self.bus.publish(self.cfg.topic_exec_pos, {"event": "ENTRY_LIVE", "symbol": pos.symbol, "side": pos.side,
                                                   "qty": qty, "price": pos.entry_px, "ts": pos.entry_ts})

    # ---- TP/SL/Trail を監視して反対成行を出す（簡易OCO/Trail） ----
    def _evaluate_exits_live(self) -> None:
        if not self.ledger.positions:
            return
        if self.best_bid is None or self.best_ask is None:
            return

        tick = self.cfg.tick_size
        i = 0
        while i < len(self.ledger.positions):
            pos = self.ledger.positions[i]
            mult = 1 if pos.side == "BUY" else -1
            mkt_px = self.best_bid if pos.side == "SELL" else self.best_ask
            if mkt_px is None:
                i += 1; continue
            pnl_ticks = (mkt_px - pos.entry_px) * mult / max(tick, 1e-9)

            # トレール
            if pos.trail:
                if pnl_ticks > pos.peak_ticks:
                    pos.peak_ticks = int(pnl_ticks)
                if pos.peak_ticks >= pos.trail_trigger:
                    if pos.side == "BUY":
                        pos.trail_stop_px = mkt_px - pos.trail_gap * tick
                    else:
                        pos.trail_stop_px = mkt_px + pos.trail_gap * tick

            exit_kind = None
            if pnl_ticks >= pos.tp_ticks:
                exit_kind = "EXIT_TP"
            elif pnl_ticks <= -pos.sl_ticks:
                exit_kind = "EXIT_SL"
            elif pos.trail_stop_px is not None:
                if (pos.side == "BUY" and mkt_px <= pos.trail_stop_px) or \
                   (pos.side == "SELL" and mkt_px >= pos.trail_stop_px):
                    exit_kind = "EXIT_TRAIL"

            if exit_kind and self.cfg.live_enabled and self.cli and self.cli.token:
                try:
                    # 反対成行
                    side = "SELL" if pos.side == "BUY" else "BUY"
                    resp = self.cli.place_simple_entry(
                        code=self.cfg.symbol,
                        side=side,
                        qty=pos.qty,
                        price=None,
                        front_order_type=10,  # 成行
                        exchange=self.cfg.exchange,
                        account_type=self.cfg.account_type,
                        cash_margin=self.cfg.cash_margin,
                        margin_trade_type=self.cfg.margin_trade_type,
                    )
                    self._log(f"[LIVE] exit({exit_kind}) sent: {resp}")
                except Exception as e:
                    self._log(f"[LIVE] exit send error: {e}")
                    return

                # ローカルにも反映
                f = Fill(symbol=self.cfg.symbol, side=("SELL" if pos.side == "BUY" else "BUY"),
                         qty=pos.qty, price=mkt_px, ts=time.time(), kind=exit_kind)
                from scalper.execution.position_tracker import Ledger  # for type hints only
                pnl_ticks_realized = self.ledger.record_fill(f, tick)
                self.risk.on_exit_filled(pos.qty, pnl_ticks_realized)
                self.bus.publish(self.cfg.topic_exec_fill, {"symbol": f.symbol, "side": f.side, "qty": f.qty,
                                                            "price": f.price, "ts": f.ts, "kind": f.kind,
                                                            "pnl_ticks": pnl_ticks_realized})
                continue
            i += 1

    def _log(self, msg: str) -> None:
        self.bus.publish(self.cfg.topic_exec_log, {"ts": time.time(), "msg": msg})

    def _print(self, s: str) -> None:
        # KabuSClient用のロガー
        self._log(s)
