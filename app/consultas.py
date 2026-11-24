"""Consultas ao espelho do CAR.

Todas as consultas são parametrizadas; nenhum texto vindo do usuário entra
no SQL. O GeoJSON é montado pelo próprio PostGIS (ST_AsGeoJSON) e só é
concatenado em Python, o que evita serializar milhares de objetos.
"""

from __future__ import annotations

import json
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Iterator, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from . import db
from .config import config

# ------------------------------------------------------------- vocabulário

STATUS = {
    "AT": "Ativo",
    "PE": "Pendente",
    "SU": "Suspenso",
    "CA": "Cancelado",
}

TIPOS = {
    "IRU": "Imóvel rural",
    "AST": "Assentamento de reforma agrária",
    "PCT": "Território de povos e comunidades tradicionais",
}

# Categoria -> (rótulo em linguagem simples, padrão ILIKE sobre o texto da condição)
CONDICOES: dict[str, tuple[str, str]] = {
    "aguardando_analise": ("Aguardando análise", "Aguardando análise%"),
    "em_analise": ("Em análise", "Em análise%"),
    "aguardando_notificacao": ("Analisado, aguardando o produtor responder a uma notificação", "%aguardando atendimento a notifica%"),
    "aguardando_regularizacao": ("Analisado, com passivo ambiental a regularizar (área a recompor)", "%aguardando regulariza%"),
    "conforme": ("Analisado e em conformidade com o Código Florestal", "%em conformidade%"),
    "conforme_com_ativos": ("Em conformidade e com ativos ambientais (excedente de vegetação)", "%com ativos ambientais%"),
    "cancelado": ("Cancelado", "Cancelado%"),
    "analisado": ("Já analisado (qualquer resultado)", "Analisado%"),
}

ORDENACOES = {
    "cod": "cod_imovel",
    "area_desc": "area_ha DESC NULLS LAST, cod_imovel",
    "area_asc": "area_ha ASC NULLS LAST, cod_imovel",
    "atualizado_desc": "atualizado_em DESC NULLS LAST, cod_imovel",
    "criado_desc": "criado_em DESC NULLS LAST, cod_imovel",
}

GeometriaSaida = Literal["completa", "simplificada", "nenhuma"]


def normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower().strip()


# ------------------------------------------------------------- filtros


class Filtros(BaseModel):
    """Filtros aceitos pela API. Os mesmos filtros são usados pela camada de
    linguagem simples: o que ela produz é validado aqui, nunca vira SQL livre."""

    cod_imovel: list[str] | None = Field(None, description="Um ou mais códigos de imóvel")
    uf: str | None = Field(None, min_length=2, max_length=2)
    cod_municipio: int | None = Field(None, ge=1000000, le=9999999)
    status: list[Literal["AT", "PE", "SU", "CA"]] | None = None
    tipo: list[Literal["IRU", "AST", "PCT"]] | None = None
    condicao: list[str] | None = Field(None, description="Categorias de condição: " + ", ".join(CONDICOES))
    area_min: float | None = Field(None, ge=0)
    area_max: float | None = Field(None, ge=0)
    mf_min: float | None = Field(None, ge=0, description="Mínimo de módulos fiscais")
    mf_max: float | None = Field(None, ge=0, description="Máximo de módulos fiscais")
    atualizado_desde: date | None = None
    atualizado_ate: date | None = None
    criado_desde: date | None = None
    bbox: tuple[float, float, float, float] | None = Field(None, description="lon_min, lat_min, lon_max, lat_max")
    lat: float | None = Field(None, ge=-35, le=6)
    lon: float | None = Field(None, ge=-75, le=-28)

    @field_validator("uf")
    @classmethod
    def _uf(cls, v):
        return v.upper() if v else v

    @field_validator("cod_imovel")
    @classmethod
    def _cod(cls, v):
        if v is None:
            return v
        if len(v) > 100:
            raise ValueError("no máximo 100 códigos por consulta")
        return [c.strip().upper() for c in v]

    @field_validator("condicao")
    @classmethod
    def _condicao(cls, v):
        if v is None:
            return v
        invalidas = [c for c in v if c not in CONDICOES]
        if invalidas:
            raise ValueError(f"condição desconhecida: {invalidas}. Use: {list(CONDICOES)}")
        return v

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v):
        if v is None:
            return v
        x1, y1, x2, y2 = v
        if not (x1 < x2 and y1 < y2):
            raise ValueError("bbox deve ser lon_min,lat_min,lon_max,lat_max")
        if (x2 - x1) * (y2 - y1) > 25:
            raise ValueError("bbox grande demais (máx. 25 graus²); use filtros de município ou a exportação")
        return v

    @model_validator(mode="after")
    def _ponto(self):
        if (self.lat is None) != (self.lon is None):
            raise ValueError("informe lat e lon juntos")
        return self


@dataclass
class SQLFiltro:
    where: str
    params: dict = field(default_factory=dict)


def montar_where(f: Filtros, geom_consulta: str | None = None) -> SQLFiltro:
    cond: list[str] = []
    p: dict = {}
    if f.cod_imovel:
        cond.append("cod_imovel = ANY(%(cods)s)")
        p["cods"] = f.cod_imovel
    if f.uf:
        cond.append("uf = %(uf)s")
        p["uf"] = f.uf
    if f.cod_municipio:
        cond.append("cod_municipio_ibge = %(mun)s")
        p["mun"] = f.cod_municipio
    if f.status:
        cond.append("status = ANY(%(status)s)")
        p["status"] = list(f.status)
    if f.tipo:
        cond.append("tipo = ANY(%(tipo)s)")
        p["tipo"] = list(f.tipo)
    if f.condicao:
        partes = []
        for i, c in enumerate(f.condicao):
            partes.append(f"condicao ILIKE %(cond{i})s")
            p[f"cond{i}"] = CONDICOES[c][1]
        cond.append("(" + " OR ".join(partes) + ")")
    for campo, col, op in (
        ("area_min", "area_ha", ">="),
        ("area_max", "area_ha", "<="),
        ("mf_min", "modulos_fiscais", ">="),
        ("mf_max", "modulos_fiscais", "<="),
        ("atualizado_desde", "atualizado_em", ">="),
        ("atualizado_ate", "atualizado_em", "<="),
        ("criado_desde", "criado_em", ">="),
    ):
        v = getattr(f, campo)
        if v is not None:
            cond.append(f"{col} {op} %({campo})s")
            p[campo] = v
    if f.bbox:
        cond.append("geom && ST_MakeEnvelope(%(x1)s, %(y1)s, %(x2)s, %(y2)s, 4674)")
        p.update(x1=f.bbox[0], y1=f.bbox[1], x2=f.bbox[2], y2=f.bbox[3])
    if f.lat is not None:
        cond.append("ST_Intersects(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4674))")
        p.update(lat=f.lat, lon=f.lon)
    if geom_consulta:
        cond.append("ST_Intersects(geom, ST_SetSRID(ST_GeomFromGeoJSON(%(gq)s), 4674))")
        p["gq"] = geom_consulta
    return SQLFiltro(" AND ".join(cond) or "TRUE", p)


def _expr_geometria(geometria: GeometriaSaida) -> str:
    if geometria == "nenhuma":
        return "NULL::json"
    if geometria == "simplificada":
        return "ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0001), 6)::json"
    return "ST_AsGeoJSON(geom, 7)::json"


PROPRIEDADES_SQL = """json_build_object(
    'cod_imovel', cod_imovel,
    'uf', uf,
    'cod_municipio_ibge', cod_municipio_ibge,
    'municipio', municipio,
    'status', status,
    'condicao', condicao,
    'tipo', tipo,
    'area_ha', area_ha::float8,
    'modulos_fiscais', modulos_fiscais::float8,
    'criado_em', to_char(criado_em AT TIME ZONE 'UTC', 'YYYY-MM-DD'),
    'atualizado_em', to_char(atualizado_em AT TIME ZONE 'UTC', 'YYYY-MM-DD')
)"""


@dataclass
class Resultado:
    corpo: bytes
    total: int
    retornados: int
    area_total_ha: float | None
    ms: float


def buscar_imoveis(
    f: Filtros,
    limite: int = 100,
    deslocamento: int = 0,
    ordenar: str = "cod",
    geometria: GeometriaSaida = "completa",
    geom_consulta: str | None = None,
    extra: dict | None = None,
) -> Resultado:
    """FeatureCollection GeoJSON (RFC 7946) paginada."""
    t0 = time.perf_counter()
    limite = max(1, min(limite, config().limite_pagina))
    sf = montar_where(f, geom_consulta)
    ordem = ORDENACOES.get(ordenar, ORDENACOES["cod"])
    propriedades = f"{PROPRIEDADES_SQL}::jsonb"
    if geom_consulta:
        # Área do imóvel que cai dentro do polígono consultado (ex.: gleba a financiar).
        propriedades += """ || jsonb_build_object('sobreposicao_ha', round((ST_Area(ST_Intersection(geom,
            ST_SetSRID(ST_GeomFromGeoJSON(%(gq)s), 4674))::geography) / 10000)::numeric, 4))"""
    sql = f"""
        SELECT json_build_object(
            'type', 'Feature',
            'id', cod_imovel,
            'geometry', {_expr_geometria(geometria)},
            'properties', {propriedades}
        )::text AS f
        FROM car.imovel
        WHERE {sf.where}
        ORDER BY {ordem}
        LIMIT %(limite)s OFFSET %(desloc)s
    """
    sql_total = f"SELECT count(*) AS n, round(sum(area_ha), 2)::float8 AS area FROM car.imovel WHERE {sf.where}"
    params = {**sf.params, "limite": limite, "desloc": max(0, deslocamento)}
    with db.conexao() as conn:
        linhas = conn.execute(sql, params).fetchall()
        tot = conn.execute(sql_total, sf.params).fetchone()
    ms = (time.perf_counter() - t0) * 1000
    cabecalho = {
        "type": "FeatureCollection",
        "numberMatched": tot["n"],
        "numberReturned": len(linhas),
        "area_total_ha": tot["area"],
        "tempo_consulta_ms": round(ms, 1),
        **(extra or {}),
    }
    inicio = json.dumps(cabecalho, ensure_ascii=False)[:-1]
    corpo = (inicio + ',"features":[' + ",".join(r["f"] for r in linhas) + "]}").encode("utf-8")
    return Resultado(corpo, tot["n"], len(linhas), tot["area"], ms)


def imovel_por_codigo(cod: str, geometria: GeometriaSaida = "completa") -> bytes | None:
    sql = f"""
        SELECT json_build_object('type','Feature','id',cod_imovel,
               'geometry', {_expr_geometria(geometria)},
               'properties', {PROPRIEDADES_SQL},
               'bbox', json_build_array(ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom)))::text AS f
        FROM car.imovel WHERE cod_imovel = %(cod)s
    """
    linha = db.buscar_um(sql, {"cod": cod.strip().upper()})
    return linha["f"].encode("utf-8") if linha else None


def validar_poligono(geojson: dict) -> str:
    """Confere tipo, número de vértices e área do polígono enviado pelo usuário."""
    cfg = config()
    if geojson.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("a geometria deve ser Polygon ou MultiPolygon (GeoJSON, SIRGAS 2000 / WGS84)")
    texto = json.dumps(geojson)
    linha = db.buscar_um(
        """SELECT ST_NPoints(g) AS n, ST_IsValid(g) AS valido,
                  ST_Area(g::geography) / 1e6 AS km2
           FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%(g)s), 4674) AS g) s""",
        {"g": texto},
    )
    if linha["n"] > cfg.limite_vertices_poligono:
        raise ValueError(f"polígono com {linha['n']} vértices; o limite é {cfg.limite_vertices_poligono}")
    if not linha["valido"]:
        raise ValueError("polígono inválido (autointerseção ou anel aberto)")
    if linha["km2"] > cfg.limite_area_poligono_km2:
        raise ValueError(f"polígono de {linha['km2']:.0f} km²; o limite é {cfg.limite_area_poligono_km2:.0f} km²")
    return texto


def tile_mvt(z: int, x: int, y: int) -> bytes:
    """Vector tile (Mapbox Vector Tile) da camada de imóveis, em EPSG:3857."""
    linha = db.buscar_um(
        """
        WITH limites AS (SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS env),
        feicoes AS (
            -- Nos zooms baixos, simplifica a ~meio pixel e omite imóveis menores que um pixel.
            SELECT ST_AsMVTGeom(
                       CASE WHEN %(z)s < 13 THEN ST_Simplify(i.geom_web, %(tol)s, true) ELSE i.geom_web END,
                       l.env, 4096, 64, true) AS geom,
                   -- O clique no mapa consulta /ponto; o código só vai no tile em zoom de detalhe.
                   CASE WHEN %(z)s >= 12 THEN i.cod_imovel END AS cod_imovel, i.status
            FROM car.imovel i, limites l
            WHERE i.geom_web && l.env
              AND (%(z)s >= 11 OR i.area_ha >= power(2, 11 - %(z)s))
        )
        SELECT ST_AsMVT(feicoes.*, 'imoveis', 4096, 'geom') AS mvt FROM feicoes
        """,
        {"z": z, "x": x, "y": y, "tol": 156543.03 / (2**z) * 0.5},
    )
    return bytes(linha["mvt"]) if linha and linha["mvt"] else b""


def stream_exportacao(f: Filtros, formato: Literal["geojson", "csv"], limite: int) -> Iterator[bytes]:
    """Exporta um recorte filtrado sem carregar tudo em memória."""
    sf = montar_where(f)
    params = {**sf.params, "limite": limite}
    if formato == "csv":
        sql = f"""
            SELECT cod_imovel, uf, cod_municipio_ibge, municipio, status, condicao, tipo,
                   area_ha::float8 AS area_ha, modulos_fiscais::float8 AS modulos_fiscais,
                   criado_em::date AS criado_em, atualizado_em::date AS atualizado_em,
                   ST_X(ST_PointOnSurface(geom)) AS lon, ST_Y(ST_PointOnSurface(geom)) AS lat
            FROM car.imovel WHERE {sf.where} ORDER BY cod_imovel LIMIT %(limite)s"""
        import csv
        import io

        cols = ["cod_imovel", "uf", "cod_municipio_ibge", "municipio", "status", "condicao", "tipo",
                "area_ha", "modulos_fiscais", "criado_em", "atualizado_em", "lon", "lat"]
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        yield ("﻿" + buf.getvalue()).encode("utf-8")
        with db.conexao() as conn:
            cur = conn.cursor()
            for r in cur.stream(sql, params):
                buf.seek(0)
                buf.truncate()
                w.writerow([r[c] for c in cols])
                yield buf.getvalue().encode("utf-8")
        return

    sql = f"""
        SELECT json_build_object('type','Feature','id',cod_imovel,
               'geometry', ST_AsGeoJSON(geom, 7)::json, 'properties', {PROPRIEDADES_SQL})::text AS f
        FROM car.imovel WHERE {sf.where} ORDER BY cod_imovel LIMIT %(limite)s"""
    yield b'{"type":"FeatureCollection","features":['
    primeiro = True
    with db.conexao() as conn:
        cur = conn.cursor()
        for r in cur.stream(sql, params):
            yield (b"" if primeiro else b",") + r["f"].encode("utf-8")
            primeiro = False
    yield b"]}"


# ------------------------------------------------------------- municípios

_municipios: dict | None = None


def municipios(recarregar: bool = False) -> list[dict]:
    global _municipios
    if _municipios is None or recarregar or not _municipios["lista"]:
        linhas = db.buscar_todos(
            """SELECT cod_municipio_ibge, municipio, uf, imoveis, area_total_ha::float8 AS area_total_ha,
                      ativos, pendentes, suspensos, cancelados,
                      ST_XMin(extensao) AS x1, ST_YMin(extensao) AS y1, ST_XMax(extensao) AS x2, ST_YMax(extensao) AS y2
               FROM car.municipio_resumo ORDER BY uf, municipio"""
        )
        for l in linhas:
            l["bbox"] = [l.pop("x1"), l.pop("y1"), l.pop("x2"), l.pop("y2")]
        _municipios = {"lista": linhas, "por_nome": {normalizar(l["municipio"]): l for l in linhas}}
    return _municipios["lista"]


def municipio_por_nome(nome: str) -> dict | None:
    municipios()
    return _municipios["por_nome"].get(normalizar(nome))


def resumo(f: Filtros) -> dict:
    """Agregados para perguntas do tipo 'quantos…'."""
    t0 = time.perf_counter()
    sf = montar_where(f)
    with db.conexao() as conn:
        geral = conn.execute(
            f"""SELECT count(*) AS imoveis, round(sum(area_ha), 2)::float8 AS area_total_ha,
                       round(avg(area_ha), 2)::float8 AS area_media_ha,
                       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY area_ha)::numeric, 2)::float8 AS area_mediana_ha
                FROM car.imovel WHERE {sf.where}""",
            sf.params,
        ).fetchone()
        por_status = conn.execute(
            f"SELECT status, count(*) AS n FROM car.imovel WHERE {sf.where} GROUP BY status ORDER BY n DESC", sf.params
        ).fetchall()
        por_condicao = conn.execute(
            f"SELECT condicao, count(*) AS n FROM car.imovel WHERE {sf.where} GROUP BY condicao ORDER BY n DESC",
            sf.params,
        ).fetchall()
        por_tipo = conn.execute(
            f"SELECT tipo, count(*) AS n FROM car.imovel WHERE {sf.where} GROUP BY tipo ORDER BY n DESC", sf.params
        ).fetchall()
        porte = conn.execute(
            f"""SELECT CASE WHEN modulos_fiscais IS NULL THEN 'sem informação'
                            WHEN modulos_fiscais <= 4 THEN 'pequeno (até 4 módulos fiscais)'
                            WHEN modulos_fiscais <= 15 THEN 'médio (4 a 15 módulos fiscais)'
                            ELSE 'grande (mais de 15 módulos fiscais)' END AS porte,
                       count(*) AS n
                FROM car.imovel WHERE {sf.where} GROUP BY 1 ORDER BY n DESC""",
            sf.params,
        ).fetchall()
    return {
        **geral,
        "por_status": {STATUS.get(r["status"], r["status"]): r["n"] for r in por_status},
        "por_condicao": {r["condicao"]: r["n"] for r in por_condicao},
        "por_tipo": {TIPOS.get(r["tipo"], r["tipo"]): r["n"] for r in por_tipo},
        "por_porte": {r["porte"]: r["n"] for r in porte},
        "tempo_consulta_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def proveniencia() -> list[dict]:
    return db.buscar_todos(
        """SELECT uf, fonte, registros, to_char(concluida_em, 'YYYY-MM-DD HH24:MI') AS carregado_em
           FROM car.carga ORDER BY concluida_em DESC LIMIT 20"""
    )
