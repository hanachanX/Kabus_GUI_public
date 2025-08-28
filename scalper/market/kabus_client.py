# market/kabus_client.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import requests
import websocket  # from websocket-client

Json = Dict[str, Any]
Logger = Callable[[str], None]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class KabuSClient:
    """
    kabuステAPIの薄いクライアント。
    - トークン取得 / 銘柄登録 / 各種GET / 発注
    - WebSocket接続（自動再接続・ping/pong）
    - on_message へWSの生JSON文字列を渡す（feed側で正規化）
    """

    def __init__(
        self,
        api_password: str,
        production: bool = True,
        host: str = "localhost",
        logger: Optional[Logger] = None,
        ping_interval: int = 20,
        ping_timeout: int = 15,
        reconnect: bool = True,
    ) -> None:
        self.api_password = api_password
        self.production = production
        self.host = host
        self._logger = logger or (lambda s: print(f"[{_ts()}] {s}"))
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.reconnect = reconnect

        self.token: Optional[str] = None
        self._ws: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_open = False
        self._stop_flag = False
        self._backoff = 2
        self._on_message: Optional[Callable[[str], None]] = None
        self._on_open: Optional[Callable[[], None]] = None
        self._on_close: Optional[Callable[[Optional[int], Optional[str]], None]] = None
        self._registered: List[Tuple[str, int]] = []  # (Symbol, Exchange)

    # -------- URLs --------
    @property
    def port(self) -> int:
        return 18080 if self.production else 18081

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/kabusapi"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/kabusapi/websocket"

    def _log(self, msg: str) -> None:
        self._logger(msg)

    # -------- REST 基本 --------
    def get_token(self) -> str:
        """
        POST /token -> Token
        """
        url = f"{self.base_url}/token"
        payload = {"APIPassword": self.api_password}
        self._log(f"POST {url} payload={payload}")
        r = requests.post(url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:400]}...")
        r.raise_for_status()
        self.token = r.json()["Token"]
        self._log(f"Token OK: {self.token[:8]}...（省略）")
        return self.token

    def _hdr(self) -> Dict[str, str]:
        if not self.token:
            raise RuntimeError("Token未取得。先に get_token() を呼んでください。")
        return {"X-API-KEY": self.token}

    # -------- 銘柄登録（PUSH対象） --------
    def register_symbols(self, symbols: Iterable[Tuple[str, int]] | Iterable[str], exchange_default: int = 1) -> Json:
        """
        PUT /register
        symbols: [("7203",1), ("9432",1)] または ["7203","9432"]（後者は exchange_default が適用）
        """
        syms: List[Tuple[str, int]] = []
        for s in symbols:
            if isinstance(s, tuple):
                syms.append((s[0], s[1]))
            else:
                syms.append((str(s), exchange_default))
        payload = {"Symbols": [{"Symbol": c, "Exchange": ex} for c, ex in syms]}
        url = f"{self.base_url}/register"
        self._log(f"PUT {url} payload={payload}")
        r = requests.put(url, headers={**self._hdr(), "Content-Type": "application/json"}, data=json.dumps(payload), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:400]}...")
        r.raise_for_status()
        self._registered = syms
        return r.json() if r.text else {}

    # -------- 参照系 --------
    def get_symbol(self, code: str, exchange: int = 1) -> Json:
        url = f"{self.base_url}/symbol/{code}@{exchange}"
        self._log(f"GET {url}")
        r = requests.get(url, headers=self._hdr(), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:400]}...")
        r.raise_for_status()
        return r.json()

    def get_board(self, code: str, exchange: int = 1) -> Json:
        url = f"{self.base_url}/board/{code}@{exchange}"
        self._log(f"GET {url}")
        r = requests.get(url, headers=self._hdr(), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:600]}...")
        r.raise_for_status()
        return r.json()

    def get_orders(self, product: Optional[int] = None) -> List[Json]:
        url = f"{self.base_url}/orders"
        if product is not None:
            url += f"?product={product}"
        self._log(f"GET {url}")
        r = requests.get(url, headers=self._hdr(), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:400]}...")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else (data.get("List") or [])

    def get_positions(self) -> List[Json]:
        url = f"{self.base_url}/positions"
        self._log(f"GET {url}")
        r = requests.get(url, headers=self._hdr(), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:400]}...")
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else (data.get("List") or [])

    def get_wallet_cash(self) -> Json:
        url = f"{self.base_url}/wallet/cash"
        self._log(f"GET {url}")
        r = requests.get(url, headers=self._hdr(), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:300]}...")
        r.raise_for_status()
        return r.json()

    def get_wallet_margin(self) -> Json:
        url = f"{self.base_url}/wallet/margin"
        self._log(f"GET {url}")
        r = requests.get(url, headers=self._hdr(), timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:300]}...")
        r.raise_for_status()
        return r.json()

    # -------- 発注（最小限） --------
    def send_order(self, payload: Json) -> Json:
        """
        任意の注文ペイロードをそのまま投げる。
        """
        url = f"{self.base_url}/sendorder"
        self._log(f"POST {url} payload={payload}")
        r = requests.post(url, headers=self._hdr(), json=payload, timeout=10)
        self._log(f"status={r.status_code} resp={r.text[:400]}...")
        r.raise_for_status()
        return r.json() if r.text else {}

    def place_simple_entry(
        self,
        code: str,
        side: str,  # "BUY" or "SELL"
        qty: int,
        price: Optional[float],
        front_order_type: int = 20,  # 20=指値, 10=成行 (仕様により異なる)
        exchange: int = 1,
        security_type: int = 1,
        account_type: int = 4,  # 特定
        cash_margin: int = 2,   # 1=現物, 2=信用
        margin_trade_type: Optional[int] = 3,  # 3=一般デイトレ
        expire_day: int = 0,
        deliv_type: int = 0,
    ) -> Json:
        side_code = 2 if side.upper() == "BUY" else 1
        payload: Json = {
            "Symbol": code,
            "Exchange": exchange,
            "SecurityType": security_type,
            "Side": side_code,
            "CashMargin": cash_margin,
            "AccountType": account_type,
            "Qty": int(qty),
            "FrontOrderType": int(front_order_type),
            "Price": 0 if price is None else float(price),
            "ExpireDay": int(expire_day),
            "DelivType": int(deliv_type),
        }
        if margin_trade_type is not None:
            payload["MarginTradeType"] = int(margin_trade_type)
        return self.send_order(payload)

    # -------- WebSocket --------
    def open_ws(
        self,
        on_message: Callable[[str], None],
        on_open: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[Optional[int], Optional[str]], None]] = None,
    ) -> None:
        """
        kabuS WSへ接続。on_message に**生のJSON文字列**を渡します。
        すでに register_symbols() 済みなら、OPEN時に再登録を試みます。
        """
        if not self.token:
            raise RuntimeError("Token未取得。先に get_token() を呼んでください。")
        self._on_message = on_message
        self._on_open = on_open
        self._on_close = on_close
        self._stop_flag = False
        self._spawn_ws()

    def _spawn_ws(self) -> None:
        url = self.ws_url
        hdr = [f"X-API-KEY: {self.token}"]

        self._log(f"WS CONNECT {url}")
        self._ws = websocket.WebSocketApp(
            url,
            header=hdr,
            on_open=self._handle_open,
            on_message=self._handle_message,
            on_error=self._handle_error,
            on_close=self._handle_close,
        )

        def run():
            # ping/pong 設定でタイムアウト監視
            self._ws.run_forever(ping_interval=self.ping_interval, ping_timeout=self.ping_timeout)

        self._ws_thread = threading.Thread(target=run, daemon=True)
        self._ws_thread.start()
        self._log("WS接続スレッド開始。PUSH待機中…")

    def close_ws(self) -> None:
        self._stop_flag = True
        try:
            if self._ws:
                self._ws.close()
        finally:
            self._ws = None
            self._ws_open = False

    # --- WS handlers ---
    def _handle_open(self, ws: websocket.WebSocketApp) -> None:
        self._ws_open = True
        self._backoff = 2
        self._log("WS OPEN (auth ok)")
        # 既登録銘柄があれば念のため再登録
        if self._registered:
            try:
                self.register_symbols(self._registered)
            except Exception as e:
                self._log(f"WARN: WS OPEN後の再登録に失敗: {e}")
        if self._on_open:
            try:
                self._on_open()
            except Exception as e:
                self._log(f"on_open callback error: {e}")

    def _handle_message(self, ws: websocket.WebSocketApp, msg: str) -> None:
        if self._on_message:
            try:
                self._on_message(msg)
            except Exception as e:
                self._log(f"on_message callback error: {e}")

    def _handle_error(self, ws: websocket.WebSocketApp, err: Exception) -> None:
        self._log(f"WS ERROR: {err}")

    def _handle_close(self, ws: websocket.WebSocketApp, code: Optional[int], reason: Optional[str]) -> None:
        self._ws_open = False
        self._log(f"WS CLOSED: code={code} reason={reason}")
        if self._on_close:
            try:
                self._on_close(code, reason)
            except Exception as e:
                self._log(f"on_close callback error: {e}")
        if self.reconnect and not self._stop_flag:
            delay = min(self._backoff, 30)
            self._log(f"WS RECONNECT in {delay}s…")
            self._backoff = min(self._backoff * 2, 30)
            threading.Timer(delay, self._spawn_ws).start()
