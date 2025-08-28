# market/feed.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Protocol, Tuple

Json = Dict[str, Any]


class Bus(Protocol):
    """後で core/bus.py に置き換える想定の最小インターフェース"""
    def publish(self, topic: str, event: Json) -> None: ...


@dataclass
class BestQuote:
    symbol: str
    exchange: int
    bid: Optional[float]
    bid_qty: Optional[float]
    ask: Optional[float]
    ask_qty: Optional[float]
    ts: float

    def to_dict(self) -> Json:
        return {
            "type": "best_quote",
            "symbol": self.symbol,
            "exchange": self.exchange,
            "bid": self.bid,
            "bid_qty": self.bid_qty,
            "ask": self.ask,
            "ask_qty": self.ask_qty,
            "ts": self.ts,
        }


@dataclass
class LastTradeLike:
    """擬似歩み値（WSのCurrentPriceと出来高差分から生成）"""
    symbol: str
    price: float
    size: int
    direction: str  # "UP" | "DOWN" | "FLAT"
    ts: float

    def to_dict(self) -> Json:
        return {
            "type": "last_trade_like",
            "symbol": self.symbol,
            "price": self.price,
            "size": self.size,
            "direction": self.direction,
            "ts": self.ts,
        }


@dataclass
class BoardDepth:
    symbol: str
    asks: List[Tuple[float, float]]  # [(price, qty)] 10段
    bids: List[Tuple[float, float]]  # [(price, qty)] 10段
    ts: float

    def to_dict(self) -> Json:
        return {"type": "board_depth", "symbol": self.symbol, "asks": self.asks, "bids": self.bids, "ts": self.ts}


@dataclass
class RefPrice:
    symbol: str
    previous_close: Optional[float]
    ts: float

    def to_dict(self) -> Json:
        return {"type": "ref_price", "symbol": self.symbol, "previous_close": self.previous_close, "ts": self.ts}


class MarketFeed:
    """
    kabuSのWS生メッセージ(JSON文字列)を受け取り、正規化イベントをBusにpublishします。
    - best_quote
    - last_trade_like（擬似歩み値）
    - board_depth（10段）
    - ref_price（前日終値など）
    """

    def __init__(self, bus: Bus, symbol: str, exchange: int = 1, default_tick: float = 0.5) -> None:
        self.bus = bus
        self.symbol = symbol
        self.exchange = exchange
        self.default_tick = default_tick

        self.prev_close: Optional[float] = None
        self._last_price: Optional[float] = None
        self._last_vol: Optional[float] = None
        self._push_ts: float = 0.0
        self.push_count: int = 0

        # 最近の価格履歴（モメンタム計算など上位で使う）
        self.price_hist: Deque[Tuple[float, float]] = deque(maxlen=400)

    @property
    def last_price(self) -> Optional[float]:
        return self._last_price

    @property
    def last_push_ts(self) -> float:
        return self._push_ts

    def on_ws_message(self, msg: str) -> None:
        """
        WebSocketの生JSON文字列を受け取り、正規化してbusへpublish
        """
        try:
            data: Json = json.loads(msg)
        except Exception:
            # 生の文字を捨てずに上位へ流したい場合は raw チャネルを用意しても良い
            return

        self._push_ts = time.time()
        self.push_count += 1

        # 参照値
        prev = data.get("PreviousClose") or data.get("ReferencePrice") or data.get("RefPrice")
        if prev is not None:
            try:
                pv = float(prev)
                self.prev_close = pv
                self.bus.publish("ref", RefPrice(self.symbol, pv, self._push_ts).to_dict())
            except Exception:
                pass

        # best quote
        bp = data.get("BidPrice") or data.get("BestBidPrice")
        ap = data.get("AskPrice") or data.get("BestAskPrice")
        bq = data.get("BidQty") or data.get("BestBidQty")
        aq = data.get("AskQty") or data.get("BestAskQty")
        bpd = float(bp) if bp is not None else None
        apd = float(ap) if ap is not None else None
        bqd = float(bq) if bq is not None else None
        aqd = float(aq) if aq is not None else None

        if (bp is not None) or (ap is not None) or (bq is not None) or (aq is not None):
            self.bus.publish(
                "best",
                BestQuote(self.symbol, self.exchange, bpd, bqd, apd, aqd, self._push_ts).to_dict(),
            )

        # 擬似歩み値（CurrentPrice & TradingVolume 差分から）
        last = data.get("CurrentPrice") or data.get("LastPrice")
        vol = data.get("TradingVolume") or data.get("Volume")
        direction = "FLAT"
        trade_size = 0
        if last is not None:
            try:
                lastf = float(last)
                if (self._last_price is not None) and (vol is not None) and (self._last_vol is not None):
                    dv = float(vol) - float(self._last_vol)
                    if dv < 0:  # セッション切替等
                        dv = 0
                    if lastf > self._last_price:
                        direction = "UP"
                    elif lastf < self._last_price:
                        direction = "DOWN"
                    else:
                        direction = "FLAT"
                    trade_size = int(dv) if dv > 0 else 0
                    if trade_size > 0:
                        self.bus.publish(
                            "tape",
                            LastTradeLike(self.symbol, lastf, trade_size, direction, self._push_ts).to_dict(),
                        )
                # 更新
                self._last_price = lastf
                self.price_hist.append((self._push_ts, lastf))
            except Exception:
                pass
        if vol is not None:
            try:
                self._last_vol = float(vol)
            except Exception:
                pass

        # 板10段
        asks: List[Tuple[float, float]] = []
        bids: List[Tuple[float, float]] = []
        for i in range(1, 11):
            s = data.get(f"Sell{i}")
            b = data.get(f"Buy{i}")
            if s and isinstance(s, dict):
                p, q = s.get("Price"), s.get("Qty")
                if p is not None and q is not None:
                    asks.append((float(p), float(q)))
            if b and isinstance(b, dict):
                p, q = b.get("Price"), b.get("Qty")
                if p is not None and q is not None:
                    bids.append((float(p), float(q)))
        if asks or bids:
            # kabuSは価格降順のことが多いが、UI側で並び替える前提
            self.bus.publish("depth", BoardDepth(self.symbol, asks=asks, bids=bids, ts=self._push_ts).to_dict())
