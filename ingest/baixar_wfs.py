"""Baixa os imóveis do CAR de uma UF a partir do WFS público do Sicar.

Uso:
    python -m ingest.baixar_wfs DF AP --saida dados/brutos

O serviço WFS público (geoserver.car.gov.br) publica uma camada por UF
(`sicar:sicar_imoveis_<uf>`) com os atributos públicos do imóvel e o
polígono em SIRGAS 2000 (EPSG:4674). Não há nome nem CPF do declarante.

O download é feito uma única vez, em páginas pequenas e com pausa entre
elas, para não pesar no Sicar. A partir daí, todas as consultas pontuais
são atendidas pela API local, e é esse o ponto da solução: o Sicar deixa
de receber downloads completos repetidos.
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
from pathlib import Path

import httpx

WFS_URL = "https://geoserver.car.gov.br/geoserver/sicar/wfs"
USER_AGENT = "InovaCAR-MVP/0.1"


def contexto_tls() -> ssl.SSLContext:
    """O geoserver do Sicar só negocia cifras antigas: baixamos o nível de
    segurança das cifras para 1, mas a verificação do certificado continua ativa."""
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


def contar(cliente: httpx.Client, uf: str) -> int:
    r = cliente.get(
        WFS_URL,
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": f"sicar:sicar_imoveis_{uf.lower()}",
            "resultType": "hits",
        },
    )
    r.raise_for_status()
    marcador = 'numberMatched="'
    ini = r.text.index(marcador) + len(marcador)
    return int(r.text[ini : r.text.index('"', ini)])


def baixar_uf(uf: str, saida: Path, pagina: int = 5000, pausa: float = 1.0) -> Path:
    uf = uf.upper()
    destino = saida / uf
    destino.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=300, headers={"User-Agent": USER_AGENT}, verify=contexto_tls()) as cliente:
        total = contar(cliente, uf)
        print(f"[{uf}] {total} imóveis no WFS")
        inicio = 0
        n = 0
        while inicio < total:
            arquivo = destino / f"{uf}_{inicio:07d}.geojson"
            if arquivo.exists() and arquivo.stat().st_size > 0:
                print(f"[{uf}] página {inicio} já baixada, pulando")
                inicio += pagina
                continue
            for tentativa in range(5):
                try:
                    r = cliente.get(
                        WFS_URL,
                        params={
                            "service": "WFS",
                            "version": "2.0.0",
                            "request": "GetFeature",
                            "typeNames": f"sicar:sicar_imoveis_{uf.lower()}",
                            "outputFormat": "application/json",
                            "sortBy": "cod_imovel",
                            "count": pagina,
                            "startIndex": inicio,
                        },
                    )
                    r.raise_for_status()
                    dados = r.json()
                    break
                except (httpx.HTTPError, json.JSONDecodeError) as erro:
                    espera = 5 * (tentativa + 1)
                    print(f"[{uf}] erro na página {inicio}: {erro}; nova tentativa em {espera}s")
                    time.sleep(espera)
            else:
                raise RuntimeError(f"Falha ao baixar a página {inicio} de {uf}")
            arquivo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
            n += len(dados.get("features", []))
            print(f"[{uf}] {inicio + len(dados.get('features', []))}/{total}")
            inicio += pagina
            time.sleep(pausa)
    (destino / "_origem.json").write_text(
        json.dumps(
            {
                "fonte": WFS_URL,
                "camada": f"sicar:sicar_imoveis_{uf.lower()}",
                "baixado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "total_wfs": total,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return destino


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ufs", nargs="+", help="Siglas das UFs, ex.: DF AP")
    p.add_argument("--saida", default="dados/brutos", type=Path)
    p.add_argument("--pagina", default=5000, type=int)
    p.add_argument("--pausa", default=1.0, type=float, help="segundos entre páginas")
    args = p.parse_args()
    for uf in args.ufs:
        baixar_uf(uf, args.saida, args.pagina, args.pausa)


if __name__ == "__main__":
    main()
