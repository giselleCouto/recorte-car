"""Cache de respostas.

Por padrão é um LRU em memória com prazo de validade (sem dependências).
Com INOVACAR_CACHE_BACKEND=redis, usa um Redis compartilhado entre réplicas.
As respostas também saem com ETag e Cache-Control, para que navegadores,
proxies e CDNs reaproveitem o conteúdo sem voltar ao servidor.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict

from .config import config


class CacheMemoria:
    def __init__(self, max_itens: int) -> None:
        self._dados: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._max = max_itens
        self._trava = threading.Lock()
        self.acertos = 0
        self.falhas = 0

    def obter(self, chave: str) -> bytes | None:
        with self._trava:
            item = self._dados.get(chave)
            if item is None or item[0] < time.monotonic():
                if item is not None:
                    del self._dados[chave]
                self.falhas += 1
                return None
            self._dados.move_to_end(chave)
            self.acertos += 1
            return item[1]

    def guardar(self, chave: str, valor: bytes, ttl: int) -> None:
        with self._trava:
            self._dados[chave] = (time.monotonic() + ttl, valor)
            self._dados.move_to_end(chave)
            while len(self._dados) > self._max:
                self._dados.popitem(last=False)

    def limpar(self) -> None:
        with self._trava:
            self._dados.clear()

    def estatisticas(self) -> dict:
        total = self.acertos + self.falhas
        return {
            "backend": "memoria",
            "itens": len(self._dados),
            "acertos": self.acertos,
            "falhas": self.falhas,
            "taxa_acerto": round(self.acertos / total, 3) if total else None,
        }


class CacheRedis:
    def __init__(self, url: str) -> None:
        import redis  # dependência opcional

        self._r = redis.Redis.from_url(url)
        self.acertos = 0
        self.falhas = 0

    def obter(self, chave: str) -> bytes | None:
        v = self._r.get("inovacar:" + chave)
        if v is None:
            self.falhas += 1
        else:
            self.acertos += 1
        return v

    def guardar(self, chave: str, valor: bytes, ttl: int) -> None:
        self._r.setex("inovacar:" + chave, ttl, valor)

    def limpar(self) -> None:
        for k in self._r.scan_iter("inovacar:*"):
            self._r.delete(k)

    def estatisticas(self) -> dict:
        total = self.acertos + self.falhas
        return {
            "backend": "redis",
            "acertos": self.acertos,
            "falhas": self.falhas,
            "taxa_acerto": round(self.acertos / total, 3) if total else None,
        }


_cache = None


def cache():
    global _cache
    if _cache is None:
        cfg = config()
        _cache = CacheRedis(cfg.redis_url) if cfg.cache_backend == "redis" else CacheMemoria(cfg.cache_max_itens)
    return _cache


def chave_requisicao(caminho: str, params: list[tuple[str, str]]) -> str:
    base = caminho + "?" + "&".join(f"{k}={v}" for k, v in sorted(params))
    return hashlib.sha1(base.encode()).hexdigest()


def etag(conteudo: bytes) -> str:
    return '"' + hashlib.md5(conteudo).hexdigest() + '"'
