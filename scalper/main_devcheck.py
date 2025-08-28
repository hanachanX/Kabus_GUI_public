# scalper/main_devcheck.py
from __future__ import annotations

import argparse
import time
from getpass import getpass

from scalper.market.kabus_client import KabuSClient
from scalper.market.feed import MarketFeed


class PrintBus:
    def publish(self, topic, event):
        # ここに来るイベント: "best"（最良気配）, "tape"（擬似歩み値）, "depth"(10段), "ref"(前日終値)
        print(topic, event)


def main():
    ap = argparse.ArgumentParser(description="kabuS 接続デモ（トークン→銘柄登録→WS受信）")
    ap.add_argument("--prod", action="store_true", help="本番(18080)を使う。省略時は検証(18081)")
    ap.add_argument("--symbol", default="7203", help="監視する銘柄コード（既定: 7203）")
    ap.add_argument("--password", default=None, help="APIパスワード（未指定なら伏字入力）")
    args = ap.parse_args()

    pw = args.password or getpass("kabuS APIパスワード: ")

    cli = KabuSClient(api_password=pw, production=args.prod)
    try:
        cli.get_token()
    except Exception as e:
        port = 18080 if args.prod else 18081
        print(f"[ERROR] トークン取得に失敗（port={port}）。kabuステのAPI設定とログイン種別/パスワードを確認してください。")
        raise

    # 監視銘柄を登録
    cli.register_symbols([(args.symbol, 1)])

    # WSを開いて正規化フィードで受信
    feed = MarketFeed(bus=PrintBus(), symbol=args.symbol, exchange=1)
    cli.open_ws(on_message=feed.on_ws_message)

    print("[INFO] 受信待機中。Ctrl+C で終了。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cli.close_ws()


if __name__ == "__main__":
    main()
