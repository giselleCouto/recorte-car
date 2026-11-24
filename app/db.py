"""Pool de conexões com o PostgreSQL/PostGIS."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import config

_pool: ConnectionPool | None = None


def _configurar(conn) -> None:
    # Toda conexão é somente leitura e tem tempo máximo por consulta.
    conn.execute(f"SET statement_timeout = {int(config().statement_timeout_ms)}")
    conn.execute("SET default_transaction_read_only = on")
    conn.commit()


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        cfg = config()
        _pool = ConnectionPool(
            cfg.database_url,
            min_size=cfg.pool_min,
            max_size=cfg.pool_max,
            kwargs={"row_factory": dict_row, "autocommit": True},
            configure=_configurar,
            open=True,
        )
    return _pool


def fechar() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conexao() -> Iterator[Any]:
    with pool().connection() as conn:
        yield conn


def buscar_todos(sql: str, params: dict | None = None) -> list[dict]:
    with conexao() as conn:
        return conn.execute(sql, params or {}).fetchall()


def buscar_um(sql: str, params: dict | None = None) -> dict | None:
    with conexao() as conn:
        return conn.execute(sql, params or {}).fetchone()
