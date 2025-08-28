# scalper/core/bus.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import queue
import threading
from typing import Callable, DefaultDict, Dict, List, Optional


Handler = Callable[[dict], None]


class EventBus:
    """
    シンプルなPub/Subイベントバス（スレッドセーフ）
    - subscribe(topic, handler): 購読開始
    - publish(topic, event_dict): 非同期で配信
    - stop(): ワーカー停止
    """
    def __init__(self, max_queue: int = 10000, start_worker: bool = True) -> None:
        self._subs: DefaultDict[str, List[Handler]] = DefaultDict(list)
        self._q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=max_queue)
        self._stop = False
        self._th: Optional[threading.Thread] = None
        if start_worker:
            self.start()

    def start(self) -> None:
        if self._th and self._th.is_alive():
            return
        self._stop = False
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def stop(self) -> None:
        self._stop = True
        try:
            self._q.put_nowait(("__stop__", {}))
        except queue.Full:
            pass
        if self._th:
            self._th.join(timeout=1.0)

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        if topic in self._subs and handler in self._subs[topic]:
            self._subs[topic].remove(handler)

    def publish(self, topic: str, event: dict) -> None:
        try:
            self._q.put_nowait((topic, event))
        except queue.Full:
            # バックプレッシャ：満杯なら古いものを捨てる
            try:
                self._q.get_nowait()
            except Exception:
                pass
            try:
                self._q.put_nowait((topic, event))
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop:
            try:
                topic, event = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if topic == "__stop__":
                break
            # トピック購読者へ配信
            for h in list(self._subs.get(topic, [])):
                try:
                    h(event)
                except Exception:
                    # ハンドラ内エラーは握りつぶし（バスは止めない）
                    pass
            # ワイルドカード購読者（"*": 何でも受け取る）
            for h in list(self._subs.get("*", [])):
                try:
                    h({"__topic__": topic, **event})
                except Exception:
                    pass
