# scalper/execution/simulator.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass
from typing import Optional, Callable

from scalper.core.bus import EventBus
from scalper.core.types import OrderIntent
from scalper.execution.position_tracker import Position, Fill, Ledger
from scalper.execution.risk import RiskManager, RiskConfig


@dataclass
class SimConfig:
    tick_size: float = 0.5
    slippage_ticks_entry: float = 0.0     # エントリの想定スリッページ（tick）
    slippage_ticks_exit: float = 0.0      # エグジットの想定スリッページ（tick）
    csv_path: Optional[str] = None        # 取引履歴をCSVに追記（Noneなら未保存）
    topic_intent: str = "strategy.intent"
    topic_best: str = "best"              # best_quote イベント
    topic_tape: str = "tape"              # 擬似歩み値（なくてもbestで評価可能）
    topic_exec_fill: str = "exec.fill"
    topic_exec_pos: str = "exec.position"
    topic_exec_log: str = "exec.log"


class Simulator:
    """
    イベント駆動のSIM。best/tape を購読し、intent で建玉→ OCO/トレールで決済。
    """
    def __init__(self, bus: EventBus, cfg: SimConfig, risk: Optional[RiskManager] = None) -> None:
        self.bus = bus
        self.cfg = cfg
        self.risk = risk or RiskManager(RiskConfig(), tick_size=cfg.tick_size)

        self.ledger = Ledger()
        self.symbol: Optional[str] = None
        # 現在の気配
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None

        # 購読
        bus.subscribe(cfg.topic_best, self._on_best)
        bus.subscribe(cfg.topic_tape, self._on_tape)
        bus.subscribe(cfg.topic_intent, self._on_intent)

        # CSV 初期化
        if self.cfg.csv_path:
            self._ensure_csv()

    # ---- イベント ----
    def _on_best(self, ev: dict) -> None:
        self.symbol = ev.get("symbol", self.symbol)
        b = ev.get("bid")
        a = ev.get("ask")
        if b is not None:
            self.best_bid = float(b)
        if a is not None:
            self.best_ask = float(a)
        self._evaluate_exits()

    def _on_tape(self, ev: dict) -> None:
        # 今回はbestで判断するので価格更新のみ参照（必要に応じて使用）
        self._evaluate_exits()

    def _on_intent(self, ev: dict) -> None:
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
            self._log(f"[SIM] entry rejected: {reason}")
            return
        # エントリ価格を決定
        px = self._decide_entry_px(side, entry_type, price)
        if px is None:
            self._log("[SIM] entry rejected: no market/best")
            return

        # スリッページ適用
        slip = self.cfg.slippage_ticks_entry * self.cfg.tick_size
        px_eff = float(px) + (slip if side == "BUY" else -slip)

        pos = Position(
            symbol=self.symbol or "",
            side=side,
            qty=qty,
            entry_px=px_eff,
            entry_ts=time.time(),
            tp_ticks=tp, sl_ticks=sl,
            trail=trail, trail_trigger=trigger, trail_gap=gap
        )
        self.ledger.add_position(pos)
        self.risk.on_entry_filled(qty)
        self.bus.publish(self.cfg.topic_exec_pos, {"event": "ENTRY", "symbol": pos.symbol, "side": pos.side,
                                                   "qty": qty, "price": px_eff, "ts": pos.entry_ts})
        self._csv_write("ENTRY", pos.symbol, side, qty, px_eff)

    # ---- 補助 ----
    def _decide_entry_px(self, side: str, entry_type: str, limit_price: Optional[float]) -> Optional[float]:
        # LIMIT: その価格にヒットしているか → best から約定できる方の価格を使う
        if entry_type == "LIMIT":
            if limit_price is None:
                # bestを指値にする（BUY→bid, SELL→ask）
                if side == "BUY":
                    return self.best_bid
                else:
                    return self.best_ask
            # 指値指定のときはヒットしていなければ見送り（簡易）
            if side == "BUY":
                if self.best_ask is not None and limit_price >= self.best_ask:
                    return self.best_ask
            else:
                if self.best_bid is not None and limit_price <= self.best_bid:
                    return self.best_bid
            return None
        # MARKET: 最良気配で約定
        if side == "BUY":
            return self.best_ask
        else:
            return self.best_bid

    def _evaluate_exits(self) -> None:
        if not self.ledger.positions:
            return
        if self.best_bid is None or self.best_ask is None:
            return
        tick = self.cfg.tick_size

        # 1建玉ずつ評価（複数にも対応）
        i = 0
        while i < len(self.ledger.positions):
            pos = self.ledger.positions[i]
            mult = 1 if pos.side == "BUY" else -1

            # 含み益/損（tick）
            mkt_px = self.best_bid if pos.side == "SELL" else self.best_ask  # クローズ側から見た約定価格
            if mkt_px is None:
                i += 1
                continue
            pnl_ticks = (mkt_px - pos.entry_px) * mult / max(tick, 1e-9)

            # トレール更新
            if pos.trail:
                if pnl_ticks > pos.peak_ticks:
                    pos.peak_ticks = int(pnl_ticks)
                # トレール作動後の逆行幅（gap）でストップ
                if pos.peak_ticks >= pos.trail_trigger:
                    # トレールの基準価格を設定（BUYなら高値- gap*tick, SELLなら安値+ gap*tick）
                    if pos.side == "BUY":
                        pos.trail_stop_px = mkt_px - pos.trail_gap * tick
                    else:
                        pos.trail_stop_px = mkt_px + pos.trail_gap * tick

            # TP/SL判定
            exit_kind: Optional[str] = None
            exit_px: Optional[float] = None

            if pnl_ticks >= pos.tp_ticks:
                exit_kind, exit_px = "EXIT_TP", mkt_px
            elif pnl_ticks <= -pos.sl_ticks:
                exit_kind, exit_px = "EXIT_SL", mkt_px
            elif pos.trail_stop_px is not None:
                if (pos.side == "BUY" and mkt_px <= pos.trail_stop_px) or \
                   (pos.side == "SELL" and mkt_px >= pos.trail_stop_px):
                    exit_kind, exit_px = "EXIT_TRAIL", mkt_px

            if exit_kind is not None and exit_px is not None:
                # スリッページ
                slip = self.cfg.slippage_ticks_exit * tick
                px_eff = float(exit_px) - slip if pos.side == "BUY" else float(exit_px) + slip
                f = Fill(symbol=pos.symbol, side=("SELL" if pos.side == "BUY" else "BUY"),
                         qty=pos.qty, price=px_eff, ts=time.time(), kind=exit_kind)
                pnl_ticks_realized = self.ledger.record_fill(f, tick)
                self.risk.on_exit_filled(pos.qty, pnl_ticks_realized)

                self.bus.publish(self.cfg.topic_exec_fill, {
                    "symbol": f.symbol, "side": f.side, "qty": f.qty,
                    "price": f.price, "ts": f.ts, "kind": f.kind,
                    "pnl_ticks": pnl_ticks_realized,
                })
                self._csv_write(f.kind, f.symbol, f.side, f.qty, px_eff, pnl_ticks_realized)
                # pop済みなのでiを進めず継続
                continue
            i += 1

    def _log(self, msg: str) -> None:
        self.bus.publish(self.cfg.topic_exec_log, {"ts": time.time(), "msg": msg})

    # ---- CSV ----
    def _ensure_csv(self) -> None:
        path = self.cfg.csv_path
        if not path:
            return
        need_header = not os.path.exists(path)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if need_header:
                w.writerow(["ts", "event", "symbol", "side", "qty", "price", "pnl_ticks"])

    def _csv_write(self, event: str, symbol: str, side: str, qty: int, price: float, pnl_ticks: Optional[float] = None) -> None:
        if not self.cfg.csv_path:
            return
        with open(self.cfg.csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), event, symbol, side, qty, f"{price:.3f}",
                        "" if pnl_ticks is None else f"{pnl_ticks:.2f}"])
