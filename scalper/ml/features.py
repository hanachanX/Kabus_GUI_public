# scalper/ml/features.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import math, time, datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

DEFAULT_TICK_SIZE = 0.5

@dataclass
class FeatureSpec:
    horizons_sec: Tuple[float, ...] = (0.3, 1.0, 3.0)
    depth_levels: int = 5  # L2〜L5合計を使いたい時の上限（asks/bidsから集計）

def _momentum_from_ticks(tick_hist: List[Tuple[float, float]], sec: float) -> float:
    """指定秒だけ遡った価格との差分（last - base）。なければ0."""
    if not tick_hist:
        return 0.0
    now = time.time()
    base = None
    for ts, px in reversed(tick_hist):
        if ts <= now - sec:
            base = px
            break
    if base is None:
        base = tick_hist[0][1]
    return (tick_hist[-1][1] - base)

def _depth_sums(asks: List[Tuple[float, float]], bids: List[Tuple[float, float]], levels: int):
    """トップからlevels段の数量合計と厚み傾き（単純差分）"""
    ak = asks[:levels]
    bk = bids[:levels]
    ask_sum = sum(q for _, q in ak)
    bid_sum = sum(q for _, q in bk)
    # 厚みの傾き（最良に近い方－遠い方）
    ask_slope = 0.0
    bid_slope = 0.0
    if len(ak) >= 2:
        ask_slope = ak[-1][1] - ak[0][1]
    if len(bk) >= 2:
        bid_slope = bk[-1][1] - bk[0][1]
    return ask_sum, bid_sum, ask_slope, bid_slope

def _microprice(best_bid: Optional[float], best_ask: Optional[float],
                bid_qty: Optional[float], ask_qty: Optional[float]) -> Optional[float]:
    if None in (best_bid, best_ask, bid_qty, ask_qty):
        return None
    den = bid_qty + ask_qty
    if den <= 0:
        return None
    return (best_ask * bid_qty + best_bid * ask_qty) / den

def compute_features(
    *,
    symbol: str,
    last_price: Optional[float],
    best_bid: Optional[float],
    best_ask: Optional[float],
    bid_qty: Optional[float],
    ask_qty: Optional[float],
    asks: List[Tuple[float, float]],
    bids: List[Tuple[float, float]],
    vwap: Optional[float],
    sma25: Optional[float],
    macd: Optional[float],
    macd_sig: Optional[float],
    rsi: Optional[float],
    tick_hist: List[Tuple[float, float]],
    tick_size: float = DEFAULT_TICK_SIZE,
    spec: FeatureSpec = FeatureSpec(),
) -> Dict:
    """GUI側の状態から1行分の特徴量を作る（辞書を返す）"""
    ts = dt.datetime.now().isoformat(timespec="seconds")

    spread = None
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid

    imb = None
    if bid_qty is not None and ask_qty is not None and (bid_qty + ask_qty) > 0:
        imb = (bid_qty - ask_qty) / (bid_qty + ask_qty)

    mp = _microprice(best_bid, best_ask, bid_qty, ask_qty)

    ask_sum, bid_sum, ask_slope, bid_slope = _depth_sums(asks, bids, spec.depth_levels)

    feats = {
        "ts": ts,
        "symbol": symbol,
        "last": float(last_price) if last_price is not None else None,
        "best_bid": float(best_bid) if best_bid is not None else None,
        "best_ask": float(best_ask) if best_ask is not None else None,
        "bid_qty": float(bid_qty) if bid_qty is not None else None,
        "ask_qty": float(ask_qty) if ask_qty is not None else None,
        "spread": float(spread) if spread is not None else None,
        "imbalance": float(imb) if imb is not None else None,
        "microprice": float(mp) if mp is not None else None,

        "vwap": float(vwap) if vwap is not None else None,
        "sma25": float(sma25) if sma25 is not None else None,
        "macd": float(macd) if macd is not None else None,
        "macd_sig": float(macd_sig) if macd_sig is not None else None,
        "rsi14": float(rsi) if rsi is not None else None,

        "ask_sum_L5": float(ask_sum),
        "bid_sum_L5": float(bid_sum),
        "ask_slope_L5": float(ask_slope),
        "bid_slope_L5": float(bid_slope),

        "tick_size": float(tick_size),
    }

    # モメンタム（複数ホライズン）
    for h in spec.horizons_sec:
        feats[f"mom_{str(h).replace('.', 'p')}s"] = float(_momentum_from_ticks(tick_hist, h))

    # pushes_per_min はGUI側で集計して渡しても良いが、ここでは省略（GUI側で足す運用）
    return feats

FEATURE_COLUMNS = [
    # 重要：学習/推論で同じ順番を使う
    "last","best_bid","best_ask","bid_qty","ask_qty","spread","imbalance","microprice",
    "vwap","sma25","macd","macd_sig","rsi14",
    "ask_sum_L5","bid_sum_L5","ask_slope_L5","bid_slope_L5",
    "tick_size","mom_0p3s","mom_1p0s","mom_3p0s","pushes_per_min",
]
