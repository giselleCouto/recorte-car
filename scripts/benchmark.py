"""Mede latência e tráfego da API com dados reais.

Uso (com a API no ar):
    python scripts/benchmark.py --url http://localhost:8000 --n 300

Sorteia imóveis reais (via exportação CSV de um recorte), consulta cada um
por código e por um ponto dentro dele, sem cache e com cache, e compara o
tamanho das respostas com o download completo da UF. Grava o resultado em
docs/benchmark.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx


def br(v: float, casas: int = 1) -> str:
    """Número no formato brasileiro: 1.234,5"""
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v: list[float], p: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(p * (len(v) - 1)))]


def medir(cliente: httpx.Client, urls: list[str]) -> tuple[list[float], list[int], list[str]]:
    tempos, tamanhos, caches = [], [], []
    for u in urls:
        t0 = time.perf_counter()
        r = cliente.get(u)
        tempos.append((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        tamanhos.append(len(r.content))
        caches.append(r.headers.get("X-Cache", "-"))
    return tempos, tamanhos, caches


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--concorrencia", type=int, default=16)
    args = ap.parse_args()
    random.seed(42)

    with httpx.Client(base_url=args.url, timeout=60) as c:
        saude = c.get("/saude").json()
        amostra = []
        for uf in saude["ufs"]:
            texto = c.get(f"/v1/exportar?uf={uf}&formato=csv").content.decode("utf-8-sig")
            linhas = list(csv.DictReader(io.StringIO(texto), delimiter=";"))
            amostra += random.sample(linhas, min(args.n // len(saude["ufs"]), len(linhas)))
        random.shuffle(amostra)

        # Identificador da rodada: a passada "fria" nunca encontra o cache de uma rodada anterior.
        rodada = f"b{time.time_ns()}"
        url_cod = [f"/v1/imoveis/{r['cod_imovel']}?geometria=completa&_r={rodada}" for r in amostra]
        url_ponto = [f"/v1/imoveis/ponto?lat={float(r['lat']):.6f}&lon={float(r['lon']):.6f}&_r={rodada}" for r in amostra]
        municipios = c.get("/v1/municipios").json()["municipios"]
        url_mun = [f"/v1/imoveis?cod_municipio={m['cod_municipio_ibge']}&limite=100&geometria=simplificada&_r={rodada}"
                   for m in municipios]
        url_resumo = [f"/v1/resumo?cod_municipio={m['cod_municipio_ibge']}&status=AT&_r={rodada}" for m in municipios]
        perguntas = [
            "Quantos imóveis ativos há em Macapá?",
            "Os 10 maiores imóveis com área a recompor no Amapá",
            "Imóveis de assentamento em Porto Grande",
            "Pequenas propriedades em conformidade no DF",
        ]
        url_nl = [f"/v1/perguntar?q={p}" for p in perguntas]

        resultados = {}
        for nome, urls in (
            ("Imóvel por código", url_cod),
            ("Imóveis em um ponto (lat/lon)", url_ponto),
            ("Lista por município (100 por página)", url_mun),
            ("Resumo por município", url_resumo),
            ("Pergunta em linguagem simples", url_nl),
        ):
            frio, tam, _ = medir(c, urls)
            sem_cache = nome == "Pergunta em linguagem simples"  # /v1/perguntar não usa cache
            quente, _, caches = medir(c, urls) if not sem_cache else ([], tam, [])
            resultados[nome] = {
                "n": len(urls),
                "frio_p50": statistics.median(frio), "frio_p95": pct(frio, 0.95),
                "quente_p50": statistics.median(quente) if quente else None,
                "quente_p95": pct(quente, 0.95) if quente else None,
                "tamanho_medio_kb": statistics.mean(tam) / 1024,
                "acertos_cache": sum(1 for x in caches if x == "HIT"),
            }

        # Rajada concorrente de consultas por ponto, sem cache (pontos deslocados).
        urls_rajada = [u + f"&_={i}" for i, u in enumerate(url_ponto)]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(args.concorrencia) as ex:
            tempos_rajada = list(ex.map(lambda u: medir(c, [u])[0][0], urls_rajada))
        duracao = time.perf_counter() - t0
        vazao = len(urls_rajada) / duracao

    import os

    import psycopg

    try:
        with psycopg.connect(os.environ.get("INOVACAR_DATABASE_URL", "postgresql://car:car@localhost:5432/car")) as conn:
            tamanho_banco = conn.execute("SELECT pg_size_pretty(pg_total_relation_size('car.imovel'))").fetchone()[0]
    except Exception:
        tamanho_banco = "não medido"
    bytes_uf = saude.get("bytes_base_completa", {})
    media_cod_kb = resultados["Imóvel por código"]["tamanho_medio_kb"]
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    linhas = [
        "# Benchmark da API (dados reais do CAR)",
        "",
        f"Medido em {agora}, numa máquina de desenvolvimento (Windows, PostgreSQL 17 + PostGIS 3.6 locais, "
        f"1 processo da API). Base: {br(saude['imoveis'], 0)} imóveis de {' e '.join(saude['ufs'])}.",
        "",
        "\"Frio\" é a primeira chamada (vai ao banco); \"quente\" é a repetição (sai do cache da API). "
        "Os tempos são medidos no cliente, incluindo HTTP local. \"—\": rota sem cache.",
        "",
        "| Consulta | Chamadas | p50 frio (ms) | p95 frio (ms) | p50 quente (ms) | p95 quente (ms) | Resposta média |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for nome, r in resultados.items():
        linhas.append(
            f"| {nome} | {r['n']} | {br(r['frio_p50'])} | {br(r['frio_p95'])} | "
            f"{br(r['quente_p50']) if r['quente_p50'] is not None else '—'} | "
            f"{br(r['quente_p95']) if r['quente_p95'] is not None else '—'} | {br(r['tamanho_medio_kb'])} KB |"
        )
    linhas += [
        "",
        f"**Concorrência:** {len(urls_rajada)} consultas por ponto, sem cache, com {args.concorrencia} clientes "
        f"simultâneos: {br(vazao, 0)} consultas/s, p50 {br(statistics.median(tempos_rajada))} ms, "
        f"p95 {br(pct(tempos_rajada, 0.95))} ms. Com um único processo da API, a cauda cresce sob concorrência; "
        "em produção o docker-compose sobe 2 workers e a API pode ganhar réplicas atrás de um balanceador.",
        "",
        "## Economia de tráfego",
        "",
        "| UF | Download completo da UF | Consulta de 1 imóvel | Proporção |",
        "|---|---:|---:|---:|",
    ]
    for uf, b in bytes_uf.items():
        linhas.append(f"| {uf} | {br(b / 1048576)} MB | {br(media_cod_kb)} KB | {br(b / 1024 / media_cod_kb, 0)}× menor |")
    linhas += [
        "",
        f"Tamanho no banco (tabela + índices): {tamanho_banco}.",
        "",
        "Hoje, quem precisa conferir um imóvel baixa o arquivo do município ou da UF inteira no Sicar. "
        "Com a API, a mesma pergunta trafega alguns KB e não chega ao Sicar: é respondida pelo espelho.",
    ]
    destino = Path(__file__).resolve().parents[1] / "docs" / "benchmark.md"
    destino.parent.mkdir(exist_ok=True)
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print("\n".join(linhas))


if __name__ == "__main__":
    main()
