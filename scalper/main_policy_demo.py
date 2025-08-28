# scalper/main_policy_demo.py
from __future__ import annotations

import argparse
import json
import time
from getpass import getpass

from scalper.core.bus import EventBus
from scalper.analytics.indicators import IndicatorEngine
from scalper.market.kabus_client import KabuSClient
from scalper.market.feed import MarketFeed
from scalper.strategy.policy import StrategyPolicy, PolicyConfig
from scalper.strategy.rules import RuleConfig
from scalper.strategy.ml_gate import MLGateConfig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true")
    ap.add_argument("--symbol", default="7203")
    ap.add_argument("--password", default=None)
    ap.add_argument("--poll", type=int, default=10)
    args = ap.parse_args()

    pw = args.password or getpass("kabuS APIパスワード: ")
    symbol = args.symbol

    bus = EventBus()
    ind = IndicatorEngine(symbol=symbol, default_tick=0.5)

    # bus→indicators
    bus.subscribe("best", lambda ev: ind.feed_best(ev.get("bid"), ev.get("bid_qty"), ev.get("ask"), ev.get("ask_qty"), ev.get("ts")))
    bus.subscribe("tape", lambda ev: ind.feed_trade_like(ev.get("price"), ev.get("size", 0), ev.get("ts")))
    bus.subscribe("ref",  lambda ev: ind.feed_ref(ev.get("previous_close")))

    # kabuS
    cli = KabuSClient(api_password=pw, production=args.prod)
    cli.get_token()
    cli.register_symbols([(symbol, 1)])
    feed = MarketFeed(bus=bus, symbol=symbol, exchange=1)
    cli.open_ws(on_message=feed.on_ws_message)

    # オフ時間は/boardで擬似更新
    stop_flag = False
    def poll():
        while not stop_flag and args.poll > 0:
            try:
                data = cli.get_board(symbol, 1)
                feed.on_ws_message(json.dumps(data))
            except Exception:
                pass
            time.sleep(args.poll)
    import threading
    th = threading.Thread(target=poll, daemon=True); th.start()

    # StrategyPolicy
    pol = StrategyPolicy(
        bus=bus,
        policy_cfg=PolicyConfig(loop_interval_sec=0.10),
        rule_cfg=RuleConfig(
            tick_size=0.5,
            spread_ticks_max=1,
            imbalance_th=0.60,
            pushes_per_min_min=40,
            use_vwap_filter=True,
            use_sma25_filter=False,
            use_macd_filter=False,
            use_rsi_filter=False,
            use_recent_return_filter=True,
            default_qty=100,
            entry_type="LIMIT",
            tp_ticks=3,
            sl_ticks=2,
            trail_enabled=True,
            trail_trigger=2,
            trail_gap=1,
        ),
        ml_cfg=MLGateConfig(enabled=False, min_prob=0.55, min_ev_ticks=0.0, cost_ticks=0.10),
        snapshot_provider=ind.snapshot,
        recent_return_provider=ind.get_return,
    )

    # ログ購読（実行層の代わりに表示）
    bus.subscribe("strategy.debug", print)
    bus.subscribe("strategy.decision", print)
    bus.subscribe("strategy.intent", print)

    pol.start()
    print("[INFO] StrategyPolicy 稼働。Ctrl+Cで終了。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        pol.stop()
        stop_flag = True
        cli.close_ws()
        th.join(timeout=1.0)
        bus.stop()


if __name__ == "__main__":
    main()

#python -m scalper.main_policy_demo --prod --symbol 7203 --password 9694825a --poll 10