"""API de consulta pontual aos dados públicos do CAR."""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import consultas, db, linguagem, ogc
from .cache import cache, chave_requisicao, etag
from .config import config
from .consultas import CONDICOES, STATUS, TIPOS, Filtros
from .privacidade import CAMPOS_PUBLICOS, mascarar_texto, pseudonimizar_ip

log = logging.getLogger("inovacar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ESTATICO = Path(__file__).parent / "static"
GEOJSON = "application/geo+json"


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    try:
        consultas.municipios(recarregar=True)
    except Exception as erro:  # a API sobe mesmo sem banco; /saude mostra o problema
        log.warning("Banco indisponível na partida: %s", erro)
    yield
    db.fechar()


app = FastAPI(
    title="InovaCAR — API de consulta ao CAR",
    version="0.1.0",
    description=(
        "Consultas pontuais aos dados públicos do Cadastro Ambiental Rural, sem baixar a base inteira. "
        "Respostas em GeoJSON (RFC 7946), SIRGAS 2000 (EPSG:4674), paginadas e com cache. "
        "Também expõe a coleção no padrão OGC API – Features em /ogc. "
        "Nenhum dado pessoal é armazenado ou retornado."
    ),
    license_info={"name": "Apache-2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"},
    lifespan=ciclo_de_vida,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=ESTATICO), name="static")

# ------------------------------------------------------------- proteção e métricas

_baldes: dict[str, list[float]] = {}
_latencias: dict[str, deque] = defaultdict(lambda: deque(maxlen=2000))
_contagem: dict[str, int] = defaultdict(int)
ISENTOS_LIMITE = ("/static", "/v1/tiles", "/demo", "/favicon")


@app.middleware("http")
async def proteger_e_medir(request: Request, chamar):
    caminho = request.url.path
    ip = pseudonimizar_ip(request.client.host if request.client else None)
    if not caminho.startswith(ISENTOS_LIMITE):
        # Balde de fichas por IP pseudonimizado.
        limite = config().limite_req_por_minuto
        agora = time.monotonic()
        fichas, ultimo = _baldes.get(ip, [float(limite), agora])
        fichas = min(limite, fichas + (agora - ultimo) * limite / 60)
        if fichas < 1:
            return JSONResponse(
                {"erro": "Muitas requisições. Aguarde alguns segundos.", "limite_por_minuto": limite},
                status_code=429,
                headers={"Retry-After": "5"},
            )
        _baldes[ip] = [fichas - 1, agora]

    t0 = time.perf_counter()
    resposta = await chamar(request)
    ms = (time.perf_counter() - t0) * 1000
    rota = request.scope.get("route")
    chave = getattr(rota, "path", caminho)
    _latencias[chave].append(ms)
    _contagem[chave] += 1
    resposta.headers["X-Tempo-Resposta-ms"] = f"{ms:.1f}"
    q = request.query_params.get("q")
    log.info("ip=%s %s %s %s %.1fms%s", ip, request.method, caminho, resposta.status_code, ms,
             f" q={mascarar_texto(q)[:120]!r}" if q else "")
    return resposta


@app.exception_handler(ValueError)
async def erro_valor(_: Request, erro: ValueError):
    return JSONResponse({"erro": str(erro)}, status_code=400)


@app.exception_handler(ValidationError)
async def erro_validacao(_: Request, erro: ValidationError):
    msgs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in erro.errors()]
    return JSONResponse({"erro": "parâmetros inválidos", "detalhes": msgs}, status_code=422)


def responder_com_cache(request: Request, produzir, ttl: int | None = None, tipo: str = GEOJSON) -> Response:
    ttl = ttl or config().cache_ttl_s
    chave = chave_requisicao(request.url.path, list(request.query_params.multi_items()))
    corpo = cache().obter(chave)
    acerto = corpo is not None
    if not acerto:
        corpo = produzir()
        if corpo is None:
            raise HTTPException(404, "não encontrado")
        cache().guardar(chave, corpo, ttl)
    marca = etag(corpo)
    cab = {"ETag": marca, "Cache-Control": f"public, max-age={ttl}", "X-Cache": "HIT" if acerto else "MISS"}
    if request.headers.get("if-none-match") == marca:
        return Response(status_code=304, headers=cab)
    return Response(corpo, media_type=tipo, headers=cab)


def _lista(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def filtros_da_query(
    uf: str | None = None,
    cod_municipio: int | None = None,
    municipio: str | None = None,
    status: str | None = None,
    tipo: str | None = None,
    condicao: str | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    mf_min: float | None = None,
    mf_max: float | None = None,
    atualizado_desde: str | None = None,
    atualizado_ate: str | None = None,
    criado_desde: str | None = None,
    bbox: str | None = None,
    cod_imovel: str | None = None,
) -> Filtros:
    if municipio and not cod_municipio:
        m = consultas.municipio_por_nome(municipio)
        if not m:
            raise ValueError(f"município '{municipio}' não encontrado nas UFs carregadas; veja /v1/municipios")
        cod_municipio = m["cod_municipio_ibge"]
    caixa = None
    if bbox:
        partes = [float(x) for x in bbox.split(",")]
        if len(partes) != 4:
            raise ValueError("bbox deve ter 4 números: lon_min,lat_min,lon_max,lat_max")
        caixa = tuple(partes)
    return Filtros(
        uf=uf, cod_municipio=cod_municipio,
        status=[s.upper() for s in _lista(status)] if status else None,
        tipo=[s.upper() for s in _lista(tipo)] if tipo else None,
        condicao=_lista(condicao), area_min=area_min, area_max=area_max, mf_min=mf_min, mf_max=mf_max,
        atualizado_desde=atualizado_desde, atualizado_ate=atualizado_ate, criado_desde=criado_desde,
        bbox=caixa, cod_imovel=_lista(cod_imovel),
    )


DESC_FILTROS = (
    "Filtros (todos opcionais e combináveis): uf, cod_municipio, municipio (nome), status (AT,PE,SU,CA), "
    "tipo (IRU,AST,PCT), condicao (" + ",".join(CONDICOES) + "), area_min, area_max (ha), mf_min, mf_max "
    "(módulos fiscais), atualizado_desde, atualizado_ate, criado_desde (AAAA-MM-DD), bbox, cod_imovel."
)

# ------------------------------------------------------------- páginas


@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/demo")


@app.get("/demo", include_in_schema=False)
def demo():
    return FileResponse(ESTATICO / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/saude", tags=["serviço"], summary="Verifica se a API e o banco estão no ar")
def saude():
    try:
        linha = db.buscar_um("SELECT count(*) AS n, array_agg(DISTINCT uf ORDER BY uf) AS ufs FROM car.imovel")
        tamanhos = db.buscar_todos(
            "SELECT DISTINCT ON (uf) uf, bytes_origem FROM car.carga WHERE bytes_origem IS NOT NULL "
            "ORDER BY uf, concluida_em DESC"
        )
        return {
            "status": "ok",
            "imoveis": linha["n"],
            "ufs": linha["ufs"] or [],
            "bytes_base_completa": {t["uf"]: t["bytes_origem"] for t in tamanhos},
        }
    except Exception as erro:
        return JSONResponse({"status": "erro", "detalhe": str(erro)[:200]}, status_code=503)


# ------------------------------------------------------------- v1


@app.get("/v1", tags=["serviço"], summary="Índice da API")
def indice():
    return {
        "servico": config().nome_servico,
        "documentacao": "/docs",
        "ogc_api_features": "/ogc",
        "rotas": {
            "/v1/imoveis/{cod_imovel}": "um imóvel pelo código do recibo do CAR",
            "/v1/imoveis/ponto?lat=&lon=": "imóveis que contêm um ponto",
            "/v1/imoveis?...": "busca filtrada e paginada. " + DESC_FILTROS,
            "POST /v1/imoveis/intersecao": "imóveis que tocam um polígono, com área de sobreposição",
            "/v1/resumo?...": "contagens e áreas agregadas, com os mesmos filtros",
            "/v1/municipios": "municípios carregados, com totais",
            "/v1/exportar?formato=csv|geojson&...": "recorte filtrado para download (sem baixar a UF inteira)",
            "/v1/tiles/{z}/{x}/{y}.pbf": "vector tiles para mapas",
            "/v1/perguntar?q=": "pergunta em linguagem simples",
            "/v1/dicionario": "dicionário de dados",
            "/v1/metricas": "desempenho e cache",
        },
    }


@app.get("/v1/dicionario", tags=["dados"], summary="Dicionário de dados, vocabulários e proveniência")
def dicionario():
    try:
        fontes = consultas.proveniencia()
    except Exception:
        fontes = []
    return {
        "campos": CAMPOS_PUBLICOS,
        "status": STATUS,
        "tipos": TIPOS,
        "condicoes": {k: v[0] for k, v in CONDICOES.items()},
        "glossario": linguagem.GLOSSARIO,
        "sistema_de_referencia": "SIRGAS 2000 geográfico (EPSG:4674); vector tiles em EPSG:3857",
        "formato": "GeoJSON (RFC 7946), UTF-8; CSV com separador ';'",
        "dados_pessoais": "nenhum. Nome, CPF e contatos do declarante não são carregados nem retornados (LGPD).",
        "proveniencia": fontes,
    }


@app.get("/v1/municipios", tags=["dados"], summary="Municípios carregados e seus totais")
def listar_municipios(request: Request):
    return responder_com_cache(
        request,
        lambda: json.dumps({"municipios": consultas.municipios()}, ensure_ascii=False, default=str).encode(),
        tipo="application/json",
    )


@app.get("/v1/imoveis/ponto", tags=["consultas"], summary="Imóveis que contêm um ponto (lat/lon)")
def imoveis_no_ponto(
    request: Request,
    lat: float = Query(..., ge=-35, le=6),
    lon: float = Query(..., ge=-75, le=-28),
    geometria: Literal["completa", "simplificada", "nenhuma"] = "completa",
):
    return responder_com_cache(
        request, lambda: consultas.buscar_imoveis(Filtros(lat=lat, lon=lon), limite=50, geometria=geometria).corpo
    )


@app.get("/v1/imoveis/{cod_imovel}", tags=["consultas"], summary="Um imóvel pelo código do CAR")
def imovel(request: Request, cod_imovel: str, geometria: Literal["completa", "simplificada", "nenhuma"] = "completa"):
    if not linguagem.RE_COD.fullmatch(cod_imovel.strip()):
        raise ValueError("código inválido; formato esperado UF-1234567-<32 caracteres hexadecimais>")
    return responder_com_cache(request, lambda: consultas.imovel_por_codigo(cod_imovel, geometria))


@app.get("/v1/imoveis", tags=["consultas"], summary="Busca filtrada e paginada", description=DESC_FILTROS)
def buscar(
    request: Request,
    uf: str | None = None,
    cod_municipio: int | None = None,
    municipio: str | None = None,
    status: str | None = None,
    tipo: str | None = None,
    condicao: str | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    mf_min: float | None = None,
    mf_max: float | None = None,
    atualizado_desde: str | None = None,
    atualizado_ate: str | None = None,
    criado_desde: str | None = None,
    bbox: str | None = Query(None, description="lon_min,lat_min,lon_max,lat_max"),
    cod_imovel: str | None = None,
    ordenar: Literal["cod", "area_desc", "area_asc", "atualizado_desc", "criado_desc"] = "cod",
    limite: int = Query(100, ge=1, le=500),
    deslocamento: int = Query(0, ge=0),
    geometria: Literal["completa", "simplificada", "nenhuma"] = "simplificada",
):
    f = filtros_da_query(uf, cod_municipio, municipio, status, tipo, condicao, area_min, area_max, mf_min, mf_max,
                         atualizado_desde, atualizado_ate, criado_desde, bbox, cod_imovel)

    def produzir():
        r = consultas.buscar_imoveis(f, limite, deslocamento, ordenar, geometria)
        links = []
        base = str(request.url.remove_query_params(["deslocamento"]))
        sep = "&" if "?" in base else "?"
        if deslocamento + limite < r.total:
            links.append({"rel": "next", "href": f"{base}{sep}deslocamento={deslocamento + limite}"})
        if deslocamento > 0:
            links.append({"rel": "prev", "href": f"{base}{sep}deslocamento={max(0, deslocamento - limite)}"})
        corpo = json.loads(r.corpo)
        corpo["links"] = links
        return json.dumps(corpo, ensure_ascii=False).encode()

    return responder_com_cache(request, produzir)


@app.post("/v1/imoveis/intersecao", tags=["consultas"],
          summary="Imóveis que tocam um polígono (ex.: área de um financiamento)")
def intersecao(
    geometria: dict = Body(..., embed=True, description="Polygon ou MultiPolygon em GeoJSON"),
    status: str | None = None,
    limite: int = Query(100, ge=1, le=500),
    saida: Literal["completa", "simplificada", "nenhuma"] = "simplificada",
):
    texto = consultas.validar_poligono(geometria)
    f = Filtros(status=[s.upper() for s in _lista(status)] if status else None)
    r = consultas.buscar_imoveis(f, limite, 0, "area_desc", saida, geom_consulta=texto)
    return Response(r.corpo, media_type=GEOJSON)


@app.get("/v1/resumo", tags=["consultas"], summary="Contagens e áreas agregadas", description=DESC_FILTROS)
def resumo(
    request: Request,
    uf: str | None = None,
    cod_municipio: int | None = None,
    municipio: str | None = None,
    status: str | None = None,
    tipo: str | None = None,
    condicao: str | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    mf_min: float | None = None,
    mf_max: float | None = None,
    atualizado_desde: str | None = None,
    atualizado_ate: str | None = None,
    criado_desde: str | None = None,
    bbox: str | None = None,
):
    f = filtros_da_query(uf, cod_municipio, municipio, status, tipo, condicao, area_min, area_max, mf_min, mf_max,
                         atualizado_desde, atualizado_ate, criado_desde, bbox)
    return responder_com_cache(
        request, lambda: json.dumps(consultas.resumo(f), ensure_ascii=False).encode(), tipo="application/json"
    )


@app.get("/v1/exportar", tags=["consultas"], summary="Recorte filtrado para download", description=DESC_FILTROS)
def exportar(
    uf: str | None = None,
    cod_municipio: int | None = None,
    municipio: str | None = None,
    status: str | None = None,
    tipo: str | None = None,
    condicao: str | None = None,
    area_min: float | None = None,
    area_max: float | None = None,
    mf_min: float | None = None,
    mf_max: float | None = None,
    atualizado_desde: str | None = None,
    atualizado_ate: str | None = None,
    criado_desde: str | None = None,
    bbox: str | None = None,
    formato: Literal["geojson", "csv"] = "geojson",
    limite: int | None = Query(None, ge=1),
):
    f = filtros_da_query(uf, cod_municipio, municipio, status, tipo, condicao, area_min, area_max, mf_min, mf_max,
                         atualizado_desde, atualizado_ate, criado_desde, bbox)
    if not f.model_dump(exclude_none=True):
        raise ValueError("informe ao menos um filtro: a exportação serve para recortes, não para a base inteira")
    teto = min(limite or config().limite_exportacao, config().limite_exportacao)
    tipo_mime = "text/csv; charset=utf-8" if formato == "csv" else GEOJSON
    nome = f"car_recorte.{'csv' if formato == 'csv' else 'geojson'}"
    return StreamingResponse(
        consultas.stream_exportacao(f, formato, teto),
        media_type=tipo_mime,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@app.get("/v1/tiles/{z}/{x}/{y}.pbf", tags=["mapas"], summary="Vector tiles (MVT) dos imóveis")
def tiles(request: Request, z: int, x: int, y: int):
    if not (4 <= z <= 18) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
        return Response(status_code=204)
    return responder_com_cache(
        request, lambda: consultas.tile_mvt(z, x, y), ttl=config().cache_ttl_tiles_s,
        tipo="application/vnd.mapbox-vector-tile",
    )


@app.get("/v1/perguntar", tags=["linguagem simples"], summary="Pergunta em português, resposta explicada")
def perguntar(q: str = Query(..., min_length=3, max_length=500)):
    t0 = time.perf_counter()
    lista = consultas.municipios()
    ufs = sorted({m["uf"] for m in lista})
    plano = linguagem.interpretar(q, lista)
    resultado: dict | None = None
    geojson = None
    if plano.acao == "imovel":
        corpo = consultas.imovel_por_codigo(plano.filtros.cod_imovel[0])
        resultado = json.loads(corpo) if corpo else None
        geojson = {"type": "FeatureCollection", "features": [resultado]} if resultado else None
    elif plano.acao == "ponto":
        resultado = json.loads(consultas.buscar_imoveis(plano.filtros, limite=20).corpo)
        geojson = resultado
    elif plano.acao == "resumo":
        resultado = consultas.resumo(plano.filtros)
    elif plano.acao == "listar":
        resultado = json.loads(
            consultas.buscar_imoveis(plano.filtros, plano.limite, 0, plano.ordenar, "simplificada").corpo
        )
        geojson = resultado
    resposta = linguagem.explicar(plano, resultado, ufs) if plano.acao != "nada" else " ".join(plano.avisos)
    return {
        "pergunta": plano.pergunta,
        "resposta": resposta,
        "entendido": plano.entendido,
        "acao": plano.acao,
        "filtros": plano.filtros.model_dump(exclude_none=True, mode="json"),
        "consulta_api_equivalente": plano.url_api() if plano.acao != "nada" else None,
        "termos": {t: linguagem.GLOSSARIO[t] for t in dict.fromkeys(plano.termos)},
        "avisos": plano.avisos,
        "resumo": resultado if plano.acao == "resumo" else None,
        "geojson": geojson,
        "tempo_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


@app.get("/v1/metricas", tags=["serviço"], summary="Latência por rota e eficiência do cache")
def metricas():
    rotas = {}
    for rota, amostras in _latencias.items():
        valores = sorted(amostras)
        if not valores:
            continue
        rotas[rota] = {
            "requisicoes": _contagem[rota],
            "p50_ms": round(statistics.median(valores), 1),
            "p95_ms": round(valores[int(0.95 * (len(valores) - 1))], 1),
        }
    return {"cache": cache().estatisticas(), "rotas": rotas}


app.include_router(ogc.router)
