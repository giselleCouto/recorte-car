"""Coleção de imóveis no padrão OGC API – Features (Parte 1: Core).

Com isso, QGIS, ArcGIS, GDAL/OGR e bibliotecas web leem os dados do CAR
direto da API, sem baixar arquivos: no QGIS, basta adicionar uma camada
"WFS / OGC API – Features" apontando para <servidor>/ogc.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import consultas, db
from .consultas import Filtros

router = APIRouter(prefix="/ogc", tags=["OGC API – Features"])

CONFORMIDADE = [
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30",
]


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _colecao(request: Request) -> dict:
    ext = db.buscar_um(
        """SELECT ST_XMin(e) x1, ST_YMin(e) y1, ST_XMax(e) x2, ST_YMax(e) y2, t1, t2
           FROM (SELECT ST_Extent(geom)::box3d e, min(atualizado_em) t1, max(atualizado_em) t2
                 FROM car.imovel) s"""
    )
    b = _base(request)
    return {
        "id": "imoveis",
        "title": "Imóveis do Cadastro Ambiental Rural",
        "description": "Perímetro e atributos públicos dos imóveis inscritos no CAR (sem dados pessoais).",
        "itemType": "feature",
        "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
        "extent": {
            "spatial": {"bbox": [[ext["x1"], ext["y1"], ext["x2"], ext["y2"]]] if ext else []},
            "temporal": {"interval": [[str(ext["t1"]) if ext else None, str(ext["t2"]) if ext else None]]},
        },
        "links": [
            {"rel": "self", "type": "application/json", "href": f"{b}/ogc/collections/imoveis"},
            {"rel": "items", "type": "application/geo+json", "href": f"{b}/ogc/collections/imoveis/items"},
            {"rel": "describedby", "type": "application/json", "href": f"{b}/v1/dicionario"},
        ],
    }


@router.get("", summary="Página inicial OGC")
def inicio(request: Request):
    b = _base(request)
    return {
        "title": "InovaCAR — dados públicos do CAR",
        "description": "Acesso padronizado (OGC API – Features) aos imóveis do CAR.",
        "links": [
            {"rel": "self", "type": "application/json", "href": f"{b}/ogc"},
            {"rel": "service-desc", "type": "application/vnd.oai.openapi+json;version=3.0", "href": f"{b}/openapi.json"},
            {"rel": "service-doc", "type": "text/html", "href": f"{b}/docs"},
            {"rel": "conformance", "type": "application/json", "href": f"{b}/ogc/conformance"},
            {"rel": "data", "type": "application/json", "href": f"{b}/ogc/collections"},
        ],
    }


@router.get("/conformance", summary="Classes de conformidade")
def conformidade():
    return {"conformsTo": CONFORMIDADE}


@router.get("/collections", summary="Coleções")
def colecoes(request: Request):
    b = _base(request)
    return {"collections": [_colecao(request)], "links": [{"rel": "self", "href": f"{b}/ogc/collections"}]}


@router.get("/collections/imoveis", summary="Metadados da coleção de imóveis")
def colecao(request: Request):
    return _colecao(request)


@router.get("/collections/imoveis/items", summary="Feições (paginadas)")
def itens(
    request: Request,
    bbox: str | None = Query(None, description="lon_min,lat_min,lon_max,lat_max (CRS84)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    uf: str | None = None,
    cod_municipio_ibge: int | None = None,
    status: str | None = None,
    tipo: str | None = None,
):
    caixa = tuple(float(x) for x in bbox.split(",")) if bbox else None
    if caixa and len(caixa) != 4:
        return JSONResponse({"code": "InvalidParameterValue", "description": "bbox precisa de 4 números"}, 400)
    f = Filtros(
        bbox=caixa, uf=uf, cod_municipio=cod_municipio_ibge,
        status=[status.upper()] if status else None, tipo=[tipo.upper()] if tipo else None,
    )
    r = consultas.buscar_imoveis(f, limit, offset, "cod", "completa")
    corpo = json.loads(r.corpo)
    base = str(request.url.remove_query_params(["offset"]))
    sep = "&" if "?" in base else "?"
    links = [{"rel": "self", "type": "application/geo+json", "href": str(request.url)}]
    if offset + limit < r.total:
        links.append({"rel": "next", "type": "application/geo+json", "href": f"{base}{sep}offset={offset + limit}"})
    if offset > 0:
        links.append({"rel": "prev", "type": "application/geo+json", "href": f"{base}{sep}offset={max(0, offset - limit)}"})
    corpo["links"] = links
    corpo["timeStamp"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return Response(json.dumps(corpo, ensure_ascii=False), media_type="application/geo+json")


@router.get("/collections/imoveis/items/{feature_id}", summary="Uma feição")
def item(feature_id: str):
    corpo = consultas.imovel_por_codigo(feature_id)
    if corpo is None:
        raise HTTPException(404, "feição não encontrada")
    return Response(corpo, media_type="application/geo+json")
