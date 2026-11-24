"""Testes de integração contra o PostGIS com dados reais (DF e AP).

Precisam do banco carregado (ver README). Sem banco, são pulados.
"""

import json
import os

import psycopg
import pytest

URL = os.environ.get("INOVACAR_DATABASE_URL", "postgresql://car:car@localhost:5432/car")


def _banco_ok() -> bool:
    try:
        with psycopg.connect(URL, connect_timeout=3) as c:
            return c.execute("SELECT count(*) FROM car.imovel").fetchone()[0] > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _banco_ok(), reason="PostGIS com dados do CAR indisponível")

COD_DF = "DF-5300108-0000AE7221F4458582BE077E36495213"
PONTO_DF = (-15.9640, -47.7980)
PERMITIDOS = {
    "cod_imovel", "uf", "cod_municipio_ibge", "municipio", "status", "condicao", "tipo",
    "area_ha", "modulos_fiscais", "criado_em", "atualizado_em", "sobreposicao_ha",
}


@pytest.fixture(scope="module")
def cliente():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _sem_dado_pessoal(fc):
    for f in fc.get("features", []):
        assert set(f["properties"]) <= PERMITIDOS, set(f["properties"]) - PERMITIDOS


def test_saude(cliente):
    r = cliente.get("/saude").json()
    assert r["status"] == "ok" and r["imoveis"] > 30000
    assert {"AP", "DF"} <= set(r["ufs"])


def test_imovel_por_codigo(cliente):
    r = cliente.get(f"/v1/imoveis/{COD_DF}")
    assert r.status_code == 200
    f = r.json()
    assert f["id"] == COD_DF and f["geometry"]["type"] == "MultiPolygon"
    assert f["properties"]["municipio"].startswith("Bras")
    _sem_dado_pessoal({"features": [f]})


def test_codigo_mal_formado(cliente):
    assert cliente.get("/v1/imoveis/abc").status_code == 400


def test_ponto(cliente):
    lat, lon = PONTO_DF
    fc = cliente.get(f"/v1/imoveis/ponto?lat={lat}&lon={lon}").json()
    assert COD_DF in [f["id"] for f in fc["features"]]
    _sem_dado_pessoal(fc)


def test_busca_paginada_por_municipio(cliente):
    fc = cliente.get("/v1/imoveis?municipio=Macapá&limite=5").json()
    assert fc["numberReturned"] == 5 and fc["numberMatched"] > 1000
    assert any(l["rel"] == "next" for l in fc["links"])
    assert all(f["properties"]["cod_municipio_ibge"] == 1600303 for f in fc["features"])
    _sem_dado_pessoal(fc)


def test_filtros_combinados(cliente):
    fc = cliente.get("/v1/imoveis?uf=AP&status=AT&condicao=aguardando_regularizacao&area_min=100"
                     "&ordenar=area_desc&limite=3&geometria=nenhuma").json()
    areas = [f["properties"]["area_ha"] for f in fc["features"]]
    assert areas == sorted(areas, reverse=True) and all(a >= 100 for a in areas)
    assert all(f["geometry"] is None for f in fc["features"])


def test_bbox(cliente):
    lat, lon = PONTO_DF
    fc = cliente.get(f"/v1/imoveis?bbox={lon-0.01},{lat-0.01},{lon+0.01},{lat+0.01}&limite=50").json()
    assert fc["numberMatched"] >= 1


def test_bbox_grande_demais_recusado(cliente):
    assert cliente.get("/v1/imoveis?bbox=-60,-20,-40,0").status_code == 422


def test_resumo(cliente):
    r = cliente.get("/v1/resumo?uf=AP").json()
    assert r["imoveis"] > 17000 and r["area_total_ha"] > 0
    assert sum(r["por_status"].values()) == r["imoveis"]


def test_cache_e_etag(cliente):
    url = "/v1/imoveis?municipio=Santana&limite=3"
    a = cliente.get(url)
    b = cliente.get(url)
    assert b.headers["X-Cache"] == "HIT"
    assert a.headers["ETag"] == b.headers["ETag"]
    c = cliente.get(url, headers={"If-None-Match": b.headers["ETag"]})
    assert c.status_code == 304 and c.content == b""


def test_intersecao_poligono(cliente):
    lat, lon = PONTO_DF
    d = 0.0005
    poli = {"type": "Polygon", "coordinates": [[[lon-d, lat-d], [lon+d, lat-d], [lon+d, lat+d], [lon-d, lat+d], [lon-d, lat-d]]]}
    fc = cliente.post("/v1/imoveis/intersecao", json={"geometria": poli}).json()
    alvo = [f for f in fc["features"] if f["id"] == COD_DF]
    assert alvo and alvo[0]["properties"]["sobreposicao_ha"] > 0


def test_poligono_invalido_recusado(cliente):
    r = cliente.post("/v1/imoveis/intersecao", json={"geometria": {"type": "Point", "coordinates": [-47.8, -15.9]}})
    assert r.status_code == 400


def test_tile(cliente):
    import math

    lat, lon = PONTO_DF
    z = 12
    x = int((lon + 180) / 360 * 2**z)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z)
    r = cliente.get(f"/v1/tiles/{z}/{x}/{y}.pbf")
    assert r.status_code == 200 and len(r.content) > 100


def test_exportar_exige_filtro_e_gera_csv(cliente):
    assert cliente.get("/v1/exportar").status_code == 400
    r = cliente.get("/v1/exportar?municipio=Serra do Navio&formato=csv")
    linhas = r.content.decode("utf-8-sig").strip().splitlines()
    assert linhas[0].startswith("cod_imovel;uf") and len(linhas) > 100


def test_exportar_geojson_valido(cliente):
    r = cliente.get("/v1/exportar?municipio=Serra do Navio&limite=10")
    fc = json.loads(r.content)
    assert len(fc["features"]) == 10


def test_injecao_no_parametro_nao_quebra(cliente):
    r = cliente.get("/v1/imoveis", params={"municipio": "Macapá'; DROP TABLE car.imovel; --"})
    assert r.status_code == 400
    assert cliente.get("/saude").json()["imoveis"] > 30000


def test_perguntar_resumo(cliente):
    r = cliente.get("/v1/perguntar", params={"q": "Quantos imóveis ativos há em Macapá?"}).json()
    assert r["acao"] == "resumo" and r["resumo"]["imoveis"] > 0
    assert "Macapá" in r["resposta"] and r["consulta_api_equivalente"].startswith("/v1/resumo")


def test_perguntar_listar_e_ponto(cliente):
    r = cliente.get("/v1/perguntar", params={"q": "Os 10 maiores imóveis com área a recompor no Amapá"}).json()
    assert r["acao"] == "listar" and len(r["geojson"]["features"]) <= 10
    lat, lon = PONTO_DF
    p = cliente.get("/v1/perguntar", params={"q": f"{lat}, {lon}"}).json()
    assert p["acao"] == "ponto" and COD_DF in p["resposta"]


def test_perguntar_sem_sentido(cliente):
    r = cliente.get("/v1/perguntar", params={"q": "qual a capital da França?"}).json()
    assert r["acao"] == "nada" and r["consulta_api_equivalente"] is None


def test_ogc_api_features(cliente):
    assert "conformsTo" in cliente.get("/ogc/conformance").json()
    col = cliente.get("/ogc/collections/imoveis").json()
    assert col["extent"]["spatial"]["bbox"][0][0] < -47
    fc = cliente.get("/ogc/collections/imoveis/items?limit=2&uf=AP").json()
    assert fc["numberReturned"] == 2 and any(l["rel"] == "next" for l in fc["links"])
    assert cliente.get(f"/ogc/collections/imoveis/items/{COD_DF}").status_code == 200


def test_dicionario_e_metricas(cliente):
    d = cliente.get("/v1/dicionario").json()
    assert "cod_imovel" in d["campos"] and d["proveniencia"]
    m = cliente.get("/v1/metricas").json()
    assert m["cache"]["acertos"] >= 1
