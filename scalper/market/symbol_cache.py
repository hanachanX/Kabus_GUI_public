# market/symbol_cache.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, Optional

from .kabus_client import KabuSClient


class SymbolCache:
    """
    /symbol を叩いて銘柄名などをキャッシュするだけの小物。
    将来は最小ティックや呼値の刻みもここで管理する想定。
    """

    def __init__(self) -> None:
        self._name: Dict[str, str] = {}

    def resolve_name(self, code: str, client: KabuSClient, exchange: int = 1) -> str:
        code = str(code).strip()
        if code in self._name:
            return self._name[code]
        try:
            d = client.get_symbol(code, exchange=exchange)
            nm = d.get("SymbolName") or d.get("IssueName") or ""
            if nm:
                self._name[code] = nm
            return nm
        except Exception:
            return ""

    def prime(self, codes: list[str], client: KabuSClient, exchange: int = 1) -> None:
        for c in codes:
            self.resolve_name(c, client, exchange)

    def clear(self) -> None:
        self._name.clear()
