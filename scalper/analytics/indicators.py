# scalper/analytics/indicators.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

from scalper.core.types import Bar5m, MarketSnapshot


@dataclass
class _EMA:
    n: int
    val: Optional[float] = None
    def update(self, x: float) -> float:
        k = 2.0 / (self.n + 1.0)
        if self.val is None:
            self.val = x
        else:
            self.val = self.val + k * (x - self.val)
        return self.val


class IndicatorEngine:
    """
    PUSH（best/tape/ref）から各種指標をインクリメンタルに更新
    - VWAP（累積）
    - 5分足（OHLC）
    - SMA25（5分終値）
    - MACD(12,26,9)
    - RSI(14)
    - Microprice
    - Imbalance
    - pushes/min
    - 短期モメンタム（get_return(sec)）
    """
    def __init__(self, symbol: str, default_tick: float = 0.5, chart_lookback_min: int = 180) -> None:
        self.symbol = symbol
        self.default_tick = default_tick
        self.chart_lookback_min = chart_lookback_min

        # 基本状態
        self.prev_close: Optional[float] = None
        self.last_price: Optional[float] = None
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.bid_qty: Optional[float] = None
        self.ask_qty: Optional[float] = None

        # VWAP
        self._cum_turnover: float = 0.0
        self._cum_vol: float = 0.0
        self.vwap: Optional[float] = None

        # pushes/min
        self._push_ts: Deque[float] = deque()

        # 5分足
        self.bars: List[Bar5m] = []

        # テクニカル
        self._ema12 = _EMA(12)
        self._ema26 = _EMA(26)
        self._ema_sig = _EMA(9)
        self.macd: Optional[float] = None
        self.macd_sig: Optional[float] = None

        self._rsi_n = 14
        self._rsi_avg_gain: Optional[float] = None
        self._rsi_avg_loss: Optional[float] = None
        self.rsi14: Optional[float] = None
        self._last_close_for_rsi: Optional[float] = None

        self.sma25: Optional[float] = None  # 5分終値の単純移動平均（25本）
        self._last_25closes: Deque[float] = deque(maxlen=25)

        # スイング
        self.swing_higher_lows: Optional[bool] = None
        self.swing_lower_highs: Optional[bool] = None

        # 価格履歴（短期モメンタム）
        self._px_hist: Deque[Tuple[float, float]] = deque(maxlen=800)

    # --------- feeds from market.feed ---------
    def feed_ref(self, previous_close: Optional[float]) -> None:
        if previous_close is None:
            return
        try:
            self.prev_close = float(previous_close)
        except Exception:
            pass

    def feed_best(self, bid: Optional[float], bid_qty: Optional[float],
                  ask: Optional[float], ask_qty: Optional[float], ts: Optional[float] = None) -> None:
        if bid is not None:
            self.best_bid = float(bid)
        if ask is not None:
            self.best_ask = float(ask)
        if bid_qty is not None:
            self.bid_qty = float(bid_qty)
        if ask_qty is not None:
            self.ask_qty = float(ask_qty)
        self._mark_push(ts)

    def feed_trade_like(self, price: float, size: int, ts: Optional[float] = None) -> None:
        # VWAP更新（size>0のみ）
        if size > 0:
            self._cum_turnover += price * size
            self._cum_vol += size
            if self._cum_vol > 0:
                self.vwap = self._cum_turnover / self._cum_vol

        # Last更新
        self.last_price = float(price)
        tnow = ts if ts is not None else time.time()
        self._px_hist.append((tnow, self.last_price))
        self._mark_push(ts)

        # 5分足更新
        self._update_5m_bar(self.last_price, tnow)

        # テクニカル更新
        self._update_technicals()

    # --------- helpers ----------
    def _mark_push(self, ts: Optional[float]) -> None:
        t = ts if ts is not None else time.time()
        self._push_ts.append(t)
        while self._push_ts and self._push_ts[0] < t - 60.0:
            self._push_ts.popleft()

    def _update_5m_bar(self, px: float, ts: float) -> None:
        # 5分境界（epoch分で丸め）
        minute = int(ts // 60)
        bucket = (minute // 5) * 5
        if not self.bars or self.bars[-1].ts_minute != bucket:
            # 古いバーを掃除
            lookback_min = self.chart_lookback_min
            cutoff = ((int(time.time()) // 60) - lookback_min)
            self.bars = [b for b in self.bars if b.ts_minute >= cutoff]
            # 新バー
            self.bars.append(Bar5m(ts_minute=bucket, o=px, l=px, h=px, c=px))
        else:
            b = self.bars[-1]
            b.l = min(b.l, px)
            b.h = max(b.h, px)
            b.c = px

    def _update_technicals(self) -> None:
        # SMA25/MACD/RSIは5分終値ベース
        if not self.bars:
            return
        close = self.bars[-1].c
        # SMA25
        self._last_25closes.append(close)
        if len(self._last_25closes) == self._last_25closes.maxlen:
            self.sma25 = sum(self._last_25closes) / len(self._last_25closes)
        else:
            self.sma25 = None

        # MACD
        m12 = self._ema12.update(close)
        m26 = self._ema26.update(close)
        if (m12 is not None) and (m26 is not None):
            macd = m12 - m26
            self.macd = macd
            self.macd_sig = self._ema_sig.update(macd)

        # RSI(14)
        if self._last_close_for_rsi is None:
            self._last_close_for_rsi = close
            return
        ch = close - self._last_close_for_rsi
        gain = max(0.0, ch)
        loss = max(0.0, -ch)
        if self._rsi_avg_gain is None:
            # 初期化（単純平均）
            # 14本分貯まるまではNoneにしておく簡易実装
            pass
        # 平滑化（Wilder）
        if self._rsi_avg_gain is None or self._rsi_avg_loss is None:
            self._rsi_avg_gain = gain
            self._rsi_avg_loss = loss
        else:
            n = float(self._rsi_n)
            self._rsi_avg_gain = (self._rsi_avg_gain * (n - 1) + gain) / n
            self._rsi_avg_loss = (self._rsi_avg_loss * (n - 1) + loss) / n

        if self._rsi_avg_loss is not None and self._rsi_avg_loss == 0.0:
            self.rsi14 = 100.0
        elif self._rsi_avg_gain is not None and self._rsi_avg_loss is not None:
            rs = self._rsi_avg_gain / max(1e-12, self._rsi_avg_loss)
            self.rsi14 = 100.0 - (100.0 / (1.0 + rs))

        self._last_close_for_rsi = close

        # Swing（直近3本で高値切下げ/安値切上げ）
        if len(self.bars) >= 3:
            L = [b.l for b in self.bars][-3:]
            H = [b.h for b in self.bars][-3:]
            self.swing_higher_lows = bool(L[0] < L[1] < L[2])
            self.swing_lower_highs = bool(H[0] > H[1] > H[2])
        else:
            self.swing_higher_lows = None
            self.swing_lower_highs = None

    # --------- calculated values ----------
    def microprice(self) -> Optional[float]:
        if None in (self.best_bid, self.best_ask, self.bid_qty, self.ask_qty):
            return None
        den = (self.bid_qty or 0) + (self.ask_qty or 0)
        if den <= 0:
            return None
        return (self.best_ask * (self.bid_qty or 0) + self.best_bid * (self.ask_qty or 0)) / den

    def imbalance(self) -> Optional[float]:
        if None in (self.bid_qty, self.ask_qty):
            return None
        s = (self.bid_qty or 0) + (self.ask_qty or 0)
        if s <= 0:
            return None
        return ((self.bid_qty or 0) - (self.ask_qty or 0)) / s

    def pushes_per_min(self) -> float:
        return float(len(self._push_ts))

    def get_return(self, sec: float = 0.7) -> float:
        """直近sec秒の価格変化（last - base）"""
        if not self._px_hist:
            return 0.0
        now = self._px_hist[-1][0]
        base_px = None
        for ts, px in reversed(self._px_hist):
            if ts <= now - sec:
                base_px = px
                break
        if base_px is None:
            base_px = self._px_hist[0][1]
        return (self.last_price or 0.0) - base_px

    # --------- snapshot ----------
    def snapshot(self) -> MarketSnapshot:
        spread = None
        if (self.best_bid is not None) and (self.best_ask is not None):
            spread = max(0.0, self.best_ask - self.best_bid)
        return MarketSnapshot(
            symbol=self.symbol,
            last_price=self.last_price,
            prev_close=self.prev_close,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            bid_qty=self.bid_qty,
            ask_qty=self.ask_qty,
            spread=spread,
            imbalance=self.imbalance(),
            microprice=self.microprice(),
            vwap=self.vwap,
            pushes_per_min=self.pushes_per_min(),
            bars_5m=list(self.bars),
            sma25=self.sma25,
            macd=self.macd,
            macd_sig=self.macd_sig,
            rsi14=self.rsi14,
            swing_higher_lows=self.swing_higher_lows,
            swing_lower_highs=self.swing_lower_highs,
        )
