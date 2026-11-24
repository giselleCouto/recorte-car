"""Leitura e normalização das origens (sem banco)."""

import numpy as np
import pyogrio.raw
from shapely import to_wkb
from shapely.geometry import MultiPolygon, Polygon

from ingest.carregar import ler_origem, normalizar

COD = "AP-1600303-0123456789ABCDEF0123456789ABCDEF"


def test_normaliza_wfs():
    r = normalizar({
        "cod_imovel": COD, "status_imovel": "AT", "area": 12.5, "m_fiscal": 0.25, "condicao": "Em análise",
        "uf": "AP", "municipio": "Macapá", "cod_municipio_ibge": 1600303, "tipo_imovel": "IRU",
        "dat_criacao": "2019-01-01T00:00:00Z", "data_atualizacao": None,
    })
    assert r["cod_imovel"] == COD and r["area_ha"] == 12.5 and r["status"] == "AT"


def test_normaliza_shapefile_sicar_e_descarta_pessoal():
    r = normalizar({
        "COD_IMOVEL": COD.lower(), "NUM_AREA": "7,5", "IND_STATUS": "Ativo", "IND_TIPO": "IRU",
        "DES_CONDIC": "Aguardando análise", "NOM_MUNICI": "Macapá", "COD_ESTADO": "AP",
        "NOM_PROPRIETARIO": "Fulano de Tal", "CPF": "123.456.789-09",
    })
    assert r["cod_imovel"] == COD
    assert r["area_ha"] == 7.5
    assert r["status"] == "AT"
    assert r["cod_municipio_ibge"] == 1600303  # derivado do código do imóvel
    assert "Fulano" not in str(r) and "123.456" not in str(r)


def test_codigo_invalido_e_descartado():
    assert normalizar({"cod_imovel": "qualquer-coisa"}) is None


def test_le_shapefile(tmp_path):
    poli = MultiPolygon([Polygon([(-51.1, 0.0), (-51.0, 0.0), (-51.0, 0.1), (-51.1, 0.0)])])
    caminho = tmp_path / "AREA_IMOVEL.shp"
    pyogrio.raw.write(
        str(caminho),
        geometry=np.array([to_wkb(poli)], dtype=object),
        field_data=[np.array([COD], dtype=object), np.array([3.2]), np.array(["Fulano"], dtype=object)],
        fields=["cod_imovel", "num_area", "nom_propri"],
        geometry_type="MultiPolygon",
        crs="EPSG:4674",
        driver="ESRI Shapefile",
    )
    registros = list(ler_origem(caminho))
    assert len(registros) == 1
    props, wkt, srid = registros[0]
    # Shapefile não distingue Polygon de MultiPolygon; a carga aplica ST_Multi.
    assert srid == 4674 and wkt.split(" ")[0] in ("POLYGON", "MULTIPOLYGON")
    r = normalizar(props)
    assert r["area_ha"] == 3.2 and "Fulano" not in str(r)
