# scalper/main_wire_indicators.py
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


def main():
    ap = argparse.ArgumentParser(description="指標配線テスト（WS＋必要なら/boardポーリング）")
    ap.add_argument("--prod", action="store_true", help="本番(18080)。省略時は検証(18081)")
    ap.add_argument("--symbol", default="7203", help="銘柄コード（例: 7203）")
    ap.add_argument("--password", default=None, help="APIパスワード（未指定ならプロンプト）")
    ap.add_argument("--poll", type=int, default=10,
                    help="PUSHがない時の /board ポーリング秒（0で無効、既定10秒）")
    args = ap.parse_args()

    pw = args.password or getpass("kabuS APIパスワード: ")
    symbol = args.symbol

    # バスと指標エンジン
    bus = EventBus()
    ind = IndicatorEngine(symbol=symbol, default_tick=0.5)

    # bus→indicators の配線
    def on_best(ev: dict):
        ind.feed_best(ev.get("bid"), ev.get("bid_qty"), ev.get("ask"), ev.get("ask_qty"), ev.get("ts"))
    def on_tape(ev: dict):
        ind.feed_trade_like(ev.get("price"), ev.get("size", 0), ev.get("ts"))
    def on_ref(ev: dict):
        ind.feed_ref(ev.get("previous_close"))

    bus.subscribe("best", on_best)
    bus.subscribe("tape", on_tape)
    bus.subscribe("ref", on_ref)

    # kabuS 接続
    cli = KabuSClient(api_password=pw, production=args.prod)
    cli.get_token()
    cli.register_symbols([(symbol, 1)])
    feed = MarketFeed(bus=bus, symbol=symbol, exchange=1)
    cli.open_ws(on_message=feed.on_ws_message)

    # ---- オフ時間のための /board ポーリング（任意） ----
    stop_flag = False

    def poll_board_loop():
        # 開始時に一回ブートストラップ
        try:
            data = cli.get_board(symbol, 1)
            feed.on_ws_message(json.dumps(data))
        except Exception:
            pass
        # 周期ポーリング
        while not stop_flag and args.poll > 0:
            time.sleep(max(1, args.poll))
            try:
                data = cli.get_board(symbol, 1)
                # /board のJSONをWSメッセージと同じようにfeedへ渡す
                feed.on_ws_message(json.dumps(data))
            except Exception:
                # ネットワークエラー等は無視して継続
                pass

    if args.poll > 0:
        th = threading.Thread(target=poll_board_loop, daemon=True)
        th.start()
    else:
        th = None

    print("[INFO] 指標更新テスト開始。Ctrl+C で終了。5秒おきにスナップショットを表示します。")
    try:
        while True:
            snap = ind.snapshot()
            fx = snap.to_features()
            print(
                "[SNAP] "
                f"last={fx['last']} vwap={fx['vwap']} spread={fx['spread']} "
                f"imb={fx['imbalance']} mp={fx['microprice']} "
                f"push/min={fx['pushes_per_min']:.0f} "
                f"sma25={fx['sma25']} macd={fx['macd']}/{fx['macd_sig']} rsi={fx['rsi14']}"
            )
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_flag = True
        cli.close_ws()
        bus.stop()
        if th:
            th.join(timeout=1.0)


if __name__ == "__main__":
    main()
