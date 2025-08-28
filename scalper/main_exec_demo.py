# scalper/main_exec_demo.py
from __future__ import annotations

import argparse
import json
import threading
import time
from getpass import getpass

from scalper.core.bus import EventBus
from scalper.analytics.indicators import IndicatorEngine
from scalper.market.kabus_client import KabuSClient
from scalper.market.feed import MarketFeed
from scalper.strategy.policy import StrategyPolicy, PolicyConfig
from scalper.strategy.rules import RuleConfig
from scalper.strategy.ml_gate import MLGateConfig
from scalper.execution.simulator import Simulator, SimConfig
from scalper.execution.risk import RiskManager, RiskConfig


def emit_manual_intent(bus: EventBus, side: str, qty: int, entry_type: str,
                       price, tp: int, sl: int, trail: bool, trigger: int, gap: int):
    bus.publish("strategy.intent", {
        "ts": time.time(),
        "intent": {
            "side": side.upper(),
            "qty": int(qty),
            "entry_type": entry_type.upper(),
            "price": price,
            "tp_ticks": int(tp),
            "sl_ticks": int(sl),
            "trail": bool(trail),
            "trail_trigger": int(trigger),
            "trail_gap": int(gap),
        },
        "features": {},
    })


def emit_synthetic_move(bus: EventBus, symbol: str, start_bid: float, start_ask: float,
                        tick: float, steps: int, direction: str, delay: float = 0.05):
    """SIM用の擬似best更新。direction='UP' で上へ、'DOWN' で下へ。"""
    bid = float(start_bid)
    ask = float(start_ask)
    for _ in range(steps):
        if direction == "UP":
            bid += tick
            ask += tick
        else:
            bid -= tick
            ask -= tick
        bus.publish("best", {
            "symbol": symbol, "bid": bid, "ask": ask,
            "bid_qty": 1000, "ask_qty": 1000, "ts": time.time()
        })
        time.sleep(delay)


def main():
    ap = argparse.ArgumentParser(description="Strategy + SIM demo (OCO/Trail, CSV出力)")
    ap.add_argument("--prod", action="store_true", help="本番(18080)。省略時は検証(18081)")
    ap.add_argument("--symbol", default="7203", help="銘柄コード")
    ap.add_argument("--password", default=None, help="APIパスワード（未指定ならプロンプト）")
    ap.add_argument("--poll", type=int, default=10, help="場外の/boardポーリング秒（0で無効）")
    ap.add_argument("--csv", default="sim_logs/trades.csv", help="SIM履歴CSVの保存先")
    # 夜間テスト用オプション
    ap.add_argument("--relax", action="store_true", help="ルール緩和（pushes/min=0, |imb|>=0.5, vwapフィルタOFF）")
    ap.add_argument("--manual-once", dest="manual_once", action="store_true", help="起動後に手動で1回だけエントリーを発火")
    ap.add_argument("--synthetic-exit", dest="synthetic_exit", action="store_true", help="手動エントリー直後に擬似bestを流しTP/SLを必ず発火")
    ap.add_argument("--duration", type=int, default=0, help="秒数指定で自動終了（0=無期限）")
    args = ap.parse_args()

    pw = args.password or getpass("kabuS APIパスワード: ")
    symbol = args.symbol
    tick = 0.5  # 既定の呼び値

    print("[BOOT] starting… prod=", args.prod, " symbol=", symbol)

    bus = EventBus()
    ind = IndicatorEngine(symbol=symbol, default_tick=tick)

    # bus→ind
    bus.subscribe("best", lambda ev: ind.feed_best(ev.get("bid"), ev.get("bid_qty"),
                                                   ev.get("ask"), ev.get("ask_qty"), ev.get("ts")))
    bus.subscribe("tape", lambda ev: ind.feed_trade_like(ev.get("price"), ev.get("size", 0), ev.get("ts")))
    bus.subscribe("ref",  lambda ev: ind.feed_ref(ev.get("previous_close")))

    # kabuS
    cli = KabuSClient(api_password=pw, production=args.prod)
    cli.get_token()
    cli.register_symbols([(symbol, 1)])
    feed = MarketFeed(bus=bus, symbol=symbol, exchange=1)
    cli.open_ws(on_message=feed.on_ws_message)
    print("[BOOT] WS open. poll=", args.poll)

    # 取引時間外は/boardをポーリング
    stop_flag = False
    def poll():
        # 起動直後に一度ブートストラップ
        try:
            data = cli.get_board(symbol, 1)
            feed.on_ws_message(json.dumps(data))
        except Exception:
            pass
        while not stop_flag and args.poll > 0:
            try:
                data = cli.get_board(symbol, 1)
                feed.on_ws_message(json.dumps(data))
            except Exception:
                pass
            time.sleep(args.poll)

    th = None
    if args.poll > 0:
        th = threading.Thread(target=poll, daemon=True)
        th.start()

    # Strategy（夜間用に緩和可）
    rule = RuleConfig(
        tick_size=tick,
        spread_ticks_max=1,
        imbalance_th=0.50 if args.relax else 0.60,
        pushes_per_min_min=0 if args.relax else 40,
        use_vwap_filter=False if args.relax else True,
        use_sma25_filter=False,
        use_macd_filter=False,
        use_rsi_filter=False,
        use_recent_return_filter=True,
        default_qty=100,
        entry_type="LIMIT",
        tp_ticks=3, sl_ticks=2,
        trail_enabled=True, trail_trigger=2, trail_gap=1,
    )
    pol = StrategyPolicy(
        bus=bus,
        policy_cfg=PolicyConfig(loop_interval_sec=0.10),
        rule_cfg=rule,
        ml_cfg=MLGateConfig(enabled=False, min_prob=0.55, min_ev_ticks=0.0, cost_ticks=0.10),
        snapshot_provider=ind.snapshot,
        recent_return_provider=ind.get_return,
    )

    # SIM（CSV書き出し）
    sim = Simulator(
        bus=bus,
        cfg=SimConfig(
            tick_size=tick,
            slippage_ticks_entry=0.0,
            slippage_ticks_exit=0.0,
            csv_path=args.csv,
            topic_intent="strategy.intent",
            topic_best="best",
            topic_tape="tape",
            topic_exec_fill="exec.fill",
            topic_exec_pos="exec.position",
            topic_exec_log="exec.log",
        ),
        risk=RiskManager(RiskConfig(), tick_size=tick),
    )

    # ログ表示
    bus.subscribe("strategy.debug", lambda ev: print("[DBG]", ev))
    bus.subscribe("strategy.decision", lambda ev: print("[DEC]", ev))
    bus.subscribe("strategy.intent", lambda ev: print("[INTENT]", ev))
    bus.subscribe("exec.position", lambda ev: print("[POS]", ev))
    bus.subscribe("exec.fill", lambda ev: print("[FILL]", ev))
    bus.subscribe("exec.log", lambda ev: print("[EXEC]", ev))

    pol.start()
    print("[INFO] Strategy+SIM 稼働。Ctrl+Cで終了。CSV→", args.csv)

    # ---- 手動1発（任意） ----
    def manual_once_worker():
        if not args.manual_once:
            return
        # 最初の/board入りを少し待つ
        time.sleep(2.0)
        # 足りなければもう一度/board投入
        snap = ind.snapshot()
        if (snap.best_bid is None) or (snap.best_ask is None):
            try:
                data = cli.get_board(symbol, 1)
                feed.on_ws_message(json.dumps(data))
            except Exception:
                pass
            time.sleep(1.0)
            snap = ind.snapshot()

        bid = snap.best_bid or 0.0
        ask = snap.best_ask or 0.0
        if bid == 0.0 and ask == 0.0:
            print("[MANUAL] bestが未取得のため手動発火をスキップ")
            return

        side = "SELL" if (snap.imbalance or 0.0) < 0 else "BUY"
        price = (bid if side == "BUY" else ask)
        emit_manual_intent(bus, side=side, qty=100, entry_type="LIMIT",
                           price=price, tp=3, sl=2, trail=True, trigger=2, gap=1)
        print(f"[MANUAL] intent fired side={side} price={price}")

        if args.synthetic_exit:
            # 手動エントリー直後にbestを4tick動かしてTPに到達させる
            direction = "UP" if side == "BUY" else "DOWN"
            b = bid if bid else (ask - tick)
            a = ask if ask else (bid + tick)
            emit_synthetic_move(bus, symbol, b, a, tick, steps=4, direction=direction, delay=0.05)
            print("[MANUAL] synthetic move sent")

    threading.Thread(target=manual_once_worker, daemon=True).start()

    # メインループ（任意の自動終了）
    t0 = time.time()
    try:
        while True:
            time.sleep(0.5)
            if args.duration and (time.time() - t0 >= args.duration):
                print("[INFO] duration reached → exit")
                break
    except KeyboardInterrupt:
        pass
    finally:
        pol.stop()
        nonlocal_stop = True  # noqa: F841  # for readability
        stop_flag = True
        cli.close_ws()
        if th:
            th.join(timeout=1.0)
        bus.stop()


if __name__ == "__main__":
    main()

#python -m scalper.main_exec_demo --prod --symbol 7203 --poll 10 --csv sim_logs\test.csv   --relax --manual-once --synthetic-exit  