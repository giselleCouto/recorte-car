"""Carrega imóveis do CAR no PostGIS.

Aceita duas origens:
  * pasta com as páginas GeoJSON baixadas do WFS público (ingest/baixar_wfs.py);
  * shapefile AREA_IMOVEL (solto ou dentro do .zip) do download público
    do car.gov.br, por município ou por UF.

Uso:
    python -m ingest.carregar dados/brutos/DF dados/brutos/AP
    python -m ingest.carregar ~/Downloads/SHAPE_1600303.zip

A carga passa por uma tabela temporária e termina com um UPSERT pela chave
cod_imovel, então pode ser repetida sem duplicar nada. Geometrias inválidas
são corrigidas com ST_MakeValid. Colunas com dado pessoal são descartadas
antes de chegar ao banco (app/privacidade.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import psycopg
from shapely import from_wkb
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.privacidade import coluna_pessoal, mascarar_texto  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]

# Nome do campo no destino -> nomes possíveis na origem (WFS e versões do shapefile do Sicar).
ALIASES: dict[str, list[str]] = {
    "cod_imovel": ["cod_imovel"],
    "area_ha": ["area", "num_area"],
    "modulos_fiscais": ["m_fiscal", "mod_fiscal", "num_modulo", "num_modulos"],
    "status": ["status_imovel", "ind_status", "situacao", "status"],
    "tipo": ["tipo_imovel", "ind_tipo", "tipo_imove", "tipo"],
    "condicao": ["condicao", "des_condic", "condicao_i"],
    "municipio": ["municipio", "nom_munici", "nom_municipio"],
    "uf": ["uf", "cod_estado", "sig_uf"],
    "cod_municipio_ibge": ["cod_municipio_ibge", "cod_munici", "cod_ibge"],
    "criado_em": ["dat_criacao", "dat_criaca"],
    "atualizado_em": ["data_atualizacao", "dat_atuali", "dat_atualizacao"],
}

STATUS_TEXTO = {"ativo": "AT", "pendente": "PE", "suspenso": "SU", "cancelado": "CA"}
COD_IMOVEL = re.compile(r"^([A-Z]{2})-(\d{7})-([0-9A-F]{32})$")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def _pegar(props: dict, campo: str):
    for nome in ALIASES[campo]:
        if nome in props and props[nome] not in (None, ""):
            return props[nome]
    return None


def _data(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def normalizar(props: dict) -> dict | None:
    """Converte os atributos de origem para o esquema car.imovel.

    Retorna None se o registro não tem um código de imóvel válido."""
    props = {k.lower(): v for k, v in props.items() if not coluna_pessoal(k) or k.lower() in ("nom_munici",)}
    cod = _pegar(props, "cod_imovel")
    if not cod:
        return None
    cod = str(cod).strip().upper()
    m = COD_IMOVEL.match(cod)
    if not m:
        return None

    status = _pegar(props, "status")
    status = str(status).strip() if status else None
    if status and len(status) > 2:
        status = STATUS_TEXTO.get(_norm(status), status[:2].upper())

    cod_mun = _pegar(props, "cod_municipio_ibge") or m.group(2)
    uf = _pegar(props, "uf") or m.group(1)

    def num(v):
        if v in (None, ""):
            return None
        return float(str(v).replace(",", "."))

    return {
        "cod_imovel": cod,
        "uf": str(uf).strip().upper()[:2],
        "cod_municipio_ibge": int(cod_mun),
        "municipio": mascarar_texto(str(_pegar(props, "municipio") or "")),
        "status": status or "AT",
        "condicao": mascarar_texto(_pegar(props, "condicao")),
        "tipo": str(_pegar(props, "tipo") or "IRU").strip().upper(),
        "area_ha": num(_pegar(props, "area_ha")),
        "modulos_fiscais": num(_pegar(props, "modulos_fiscais")),
        "criado_em": _data(_pegar(props, "criado_em")),
        "atualizado_em": _data(_pegar(props, "atualizado_em")),
    }


# ---------------------------------------------------------------- leitores


def ler_geojson_pasta(pasta: Path) -> Iterator[tuple[dict, str, int]]:
    """Gera (atributos, WKT, srid) para cada feição das páginas GeoJSON."""
    for arq in sorted(pasta.glob("*.geojson")):
        dados = json.loads(arq.read_text(encoding="utf-8"))
        for f in dados.get("features", []):
            if not f.get("geometry"):
                continue
            yield f["properties"], shape(f["geometry"]).wkt, 4674


def ler_shapefile(caminho: Path) -> Iterator[tuple[dict, str, int]]:
    """Lê o shapefile AREA_IMOVEL do download público do car.gov.br."""
    import pyogrio

    alvo = str(caminho)
    if caminho.suffix.lower() == ".zip":
        import zipfile

        with zipfile.ZipFile(caminho) as z:
            shps = [n for n in z.namelist() if n.lower().endswith(".shp")]
        escolhido = next((n for n in shps if "area_imovel" in n.lower()), shps[0] if shps else None)
        if escolhido is None:
            raise ValueError(f"Nenhum .shp dentro de {caminho}")
        alvo = f"/vsizip/{caminho.as_posix()}/{escolhido}"

    info = pyogrio.read_info(alvo)
    srid = 4674
    crs = info.get("crs") or ""
    if crs.upper().startswith("EPSG:"):
        srid = int(crs.split(":")[1])
    meta, _, geoms, campos = pyogrio.raw.read(alvo, encoding=info.get("encoding") or "UTF-8")
    nomes = list(meta["fields"])
    for i, wkb in enumerate(geoms):
        if wkb is None:
            continue
        props = {nome: campos[j][i] for j, nome in enumerate(nomes)}
        yield props, from_wkb(wkb).wkt, srid


def ler_origem(caminho: Path) -> Iterator[tuple[dict, str, int]]:
    if caminho.is_dir():
        if any(caminho.glob("*.geojson")):
            return ler_geojson_pasta(caminho)
        shps = list(caminho.glob("*.shp")) + list(caminho.glob("*.zip"))
        if not shps:
            raise ValueError(f"Nada para carregar em {caminho}")

        def todos():
            for s in shps:
                yield from ler_shapefile(s)

        return todos()
    return ler_shapefile(caminho)


# ---------------------------------------------------------------- carga


def aplicar_esquema(conn: psycopg.Connection) -> None:
    conn.execute((RAIZ / "sql" / "001_esquema.sql").read_text(encoding="utf-8"))


def tamanho_origem(caminho: Path) -> int:
    if caminho.is_dir():
        return sum(p.stat().st_size for p in caminho.iterdir() if p.is_file() and not p.name.startswith("_"))
    return caminho.stat().st_size


def carregar(
    conn: psycopg.Connection, registros: Iterable[tuple[dict, str, int]], fonte: str, bytes_origem: int | None = None
) -> dict:
    inicio = datetime.now(timezone.utc)
    conn.execute(
        """CREATE TEMP TABLE IF NOT EXISTS stg_imovel (
               cod_imovel text, uf text, cod_municipio_ibge int, municipio text,
               status text, condicao text, tipo text, area_ha numeric, modulos_fiscais numeric,
               criado_em timestamptz, atualizado_em timestamptz, wkt text, srid int
           ) ON COMMIT DROP"""
    )
    ok = descartados = 0
    ufs: set[str] = set()
    colunas = (
        "cod_imovel, uf, cod_municipio_ibge, municipio, status, condicao, tipo, "
        "area_ha, modulos_fiscais, criado_em, atualizado_em, wkt, srid"
    )
    with conn.cursor().copy(f"COPY stg_imovel ({colunas}) FROM STDIN") as copia:
        for props, wkt, srid in registros:
            r = normalizar(props)
            if r is None:
                descartados += 1
                continue
            ufs.add(r["uf"])
            copia.write_row(
                (
                    r["cod_imovel"], r["uf"], r["cod_municipio_ibge"], r["municipio"], r["status"],
                    r["condicao"], r["tipo"], r["area_ha"], r["modulos_fiscais"], r["criado_em"],
                    r["atualizado_em"], wkt, srid,
                )
            )
            ok += 1
    conn.execute(
        """
        WITH s AS (
            SELECT DISTINCT ON (cod_imovel)
                   cod_imovel, uf, cod_municipio_ibge, municipio, status, condicao, tipo,
                   area_ha, modulos_fiscais, criado_em, atualizado_em,
                   ST_Multi(ST_CollectionExtract(ST_MakeValid(
                       ST_Transform(ST_SetSRID(ST_GeomFromText(wkt), srid), 4674)), 3)) AS g
            FROM stg_imovel
            ORDER BY cod_imovel, atualizado_em DESC NULLS LAST
        )
        INSERT INTO car.imovel AS i (cod_imovel, uf, cod_municipio_ibge, municipio, status, condicao, tipo,
                                     area_ha, modulos_fiscais, criado_em, atualizado_em, geom, geom_web, fonte)
        SELECT cod_imovel, uf, cod_municipio_ibge, municipio, status, condicao, tipo,
               area_ha, modulos_fiscais, criado_em, atualizado_em, g, ST_Transform(g, 3857), %(fonte)s
        FROM s
        ON CONFLICT (cod_imovel) DO UPDATE SET
            uf = EXCLUDED.uf, cod_municipio_ibge = EXCLUDED.cod_municipio_ibge,
            municipio = EXCLUDED.municipio, status = EXCLUDED.status, condicao = EXCLUDED.condicao,
            tipo = EXCLUDED.tipo, area_ha = EXCLUDED.area_ha, modulos_fiscais = EXCLUDED.modulos_fiscais,
            criado_em = EXCLUDED.criado_em, atualizado_em = EXCLUDED.atualizado_em,
            geom = EXCLUDED.geom, geom_web = EXCLUDED.geom_web, fonte = EXCLUDED.fonte, carregado_em = now()
        """,
        {"fonte": fonte},
    )
    for uf in sorted(ufs):
        conn.execute(
            "INSERT INTO car.carga (uf, fonte, registros, descartados, bytes_origem, iniciada_em) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uf, fonte, ok, descartados, bytes_origem if len(ufs) == 1 else None, inicio),
        )
    return {"carregados": ok, "descartados": descartados, "ufs": sorted(ufs)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("origens", nargs="+", type=Path, help="pastas GeoJSON do WFS, shapefiles ou .zip do Sicar")
    p.add_argument("--database-url", default=os.environ.get("INOVACAR_DATABASE_URL", "postgresql://car:car@localhost:5432/car"))
    args = p.parse_args()

    with psycopg.connect(args.database_url) as conn:
        aplicar_esquema(conn)
        conn.commit()
        for origem in args.origens:
            fonte_info = origem / "_origem.json"
            fonte = (
                json.loads(fonte_info.read_text(encoding="utf-8"))["fonte"] + " (WFS público do Sicar)"
                if fonte_info.exists()
                else f"car.gov.br — download público ({origem.name})"
            )
            with conn.transaction():
                res = carregar(conn, ler_origem(origem), fonte, tamanho_origem(origem))
            print(f"{origem}: {res}")
        conn.execute("REFRESH MATERIALIZED VIEW car.municipio_resumo")
        conn.execute("ANALYZE car.imovel")
        conn.commit()
        total = conn.execute("SELECT count(*) FROM car.imovel").fetchone()[0]
        print(f"Total de imóveis no banco: {total}")


if __name__ == "__main__":
    main()
