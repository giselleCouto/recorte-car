"""Configuração por variáveis de ambiente (ou arquivo .env)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ = Path(__file__).resolve().parents[1]


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=RAIZ / ".env", env_prefix="INOVACAR_", extra="ignore")

    # Conexão com o PostgreSQL/PostGIS
    database_url: str = "postgresql://car:car@localhost:5432/car"
    pool_min: int = 1
    pool_max: int = 10
    statement_timeout_ms: int = 8000  # nenhuma consulta derruba o banco

    # Cache: "memoria" (padrão) ou "redis"
    cache_backend: str = "memoria"
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_s: int = 300
    cache_ttl_tiles_s: int = 3600
    cache_max_itens: int = 5000

    # Proteção da infraestrutura
    limite_req_por_minuto: int = 240
    limite_pagina: int = 500
    limite_exportacao: int = 20000
    limite_area_poligono_km2: float = 5000.0
    limite_vertices_poligono: int = 5000

    # Rótulo de proveniência
    nome_servico: str = "InovaCAR — API de consulta ao CAR"


@lru_cache
def config() -> Config:
    return Config()
