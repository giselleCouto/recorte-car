# Recorte CAR — consulta pontual aos dados do Cadastro Ambiental Rural

MVP de código aberto para o **Desafio 3 do InovaCAR (Ciclo 1)**: *Ampliando o acesso aos dados do CAR*.

Hoje, para conferir um imóvel do CAR, o usuário baixa o arquivo do município ou do estado inteiro no Sicar. O **Recorte CAR** é um espelho de consulta. Ele carrega os dados públicos do Sicar uma vez em PostgreSQL/PostGIS, com índices espaciais, e responde a perguntas pontuais por uma API. A resposta traz só os imóveis pedidos, em poucos KB. Tudo é aberto, roda em qualquer nuvem ou servidor público e não guarda dado pessoal.

| O que o briefing pede | Como o MVP atende |
|---|---|
| APIs para consultas pontuais, sem baixar a base inteira | `/v1/imoveis/{código}`, `/v1/imoveis/ponto`, busca por município, bbox, polígono e filtros |
| Filtros avançados antes do download | `/v1/exportar`: recorte em CSV ou GeoJSON por situação, etapa da análise (ex.: "área a recompor"), porte, área e data de atualização |
| Indexação espacial, cache e desempenho real no backend | GiST em SIRGAS 2000 e em Web Mercator pré-projetado; GeoJSON montado no banco; cache LRU ou Redis com ETag; vector tiles (MVT) |
| Interoperabilidade (e-PING) | REST + OpenAPI 3, GeoJSON RFC 7946, UTF-8, SIRGAS 2000 (EPSG:4674) e **OGC API – Features** (abre direto no QGIS) |
| Interface para usuário leigo, chatbot em linguagem natural | `/v1/perguntar`: pergunta em português → consulta validada → resposta explicada, com o link da API equivalente |
| Dicionário de dados | `/v1/dicionario`, com vocabulários, glossário em linguagem simples e proveniência |
| LGPD | lista fechada de campos públicos, descarte de colunas pessoais na carga, mascaramento de CPF/CNPJ/e-mail/telefone, IP pseudonimizado |
| Não sobrecarregar o Sicar | uma carga por ciclo de atualização; o tráfego das consultas fica no espelho, com limite de requisições e tempo máximo por consulta |

## Desempenho medido

Medição de 13/11/2026 com os 38.865 imóveis reais de DF e AP, numa máquina de desenvolvimento com 1 processo da API ([detalhes](docs/benchmark.md)):

| Consulta | Mediana sem cache | Mediana com cache | Resposta |
|---|---:|---:|---:|
| Imóvel por código | 8 ms | 6 ms | ~1 KB |
| Imóveis em um ponto | 12 ms | 6 ms | ~17 KB (polígonos completos) |
| Resumo de um município | 23 ms | 8 ms | ~1 KB |

Conferir um imóvel trafega **mais de 17 mil vezes menos** que baixar o arquivo da UF (18,5 MB no AP e 28 MB no DF). O banco ocupa 55 MB com os índices, cerca de 1,4 KB por imóvel.

## Arquitetura

```
 Sicar (WFS público / shapefiles do car.gov.br)
        │  ingest/baixar_wfs.py  (uma vez por ciclo, paginado, com pausa)
        ▼
 ingest/carregar.py ── descarta colunas pessoais, corrige geometrias, UPSERT
        ▼
 PostgreSQL + PostGIS  car.imovel (GiST 4674 + GiST 3857) · car.municipio_resumo
        ▼
 FastAPI (app/) ── cache (memória | Redis) + ETag ── limite por IP pseudonimizado
   ├─ /v1/...          consultas pontuais, resumo, exportação de recortes
   ├─ /v1/tiles        vector tiles para mapas
   ├─ /v1/perguntar    linguagem simples (regras determinísticas, sem LLM externo)
   ├─ /ogc/...         OGC API – Features
   └─ /demo            mapa de demonstração (MapLibre GL)
```

A camada de linguagem simples **não usa modelo de linguagem de terceiros**. É um interpretador determinístico com vocabulário do CAR: municípios carregados, situação, etapa da análise, tipo, porte em módulos fiscais, área, datas, coordenadas e código do imóvel. A saída é sempre um conjunto de filtros validado pelo mesmo modelo da API, nunca SQL. Com isso, ela roda sem GPU, sem chave de API e sem enviar a pergunta do cidadão a terceiros, e cada decisão pode ser auditada.

## Dados

O MVP usa **dados reais** do serviço WFS público do Sicar (`geoserver.car.gov.br`, camadas `sicar:sicar_imoveis_<uf>`). Esse serviço não pede CAPTCHA e não traz nome nem CPF. Carga atual: **DF (21.041 imóveis) e AP (17.824 imóveis, 16 municípios)**. O carregador também aceita o shapefile `AREA_IMOVEL` (solto ou em .zip) do download público do car.gov.br. Os dados não ficam neste repositório.

## Como rodar

### Com Docker (recomendado)

```bash
docker compose up -d
docker compose --profile carga run --rm carga   # baixa DF e AP do WFS e carrega (~3 min)
```

Abra http://localhost:8000 (demonstração) e http://localhost:8000/docs (documentação OpenAPI).

### Sem Docker

Requisitos: Python 3.11+ e PostgreSQL 15+ com PostGIS 3.3+.

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
export INOVACAR_DATABASE_URL=postgresql://car:car@localhost:5432/car
python -m ingest.baixar_wfs DF AP --saida dados/brutos
python -m ingest.carregar dados/brutos/DF dados/brutos/AP
uvicorn app.main:app --port 8000
```

Para outra UF, troque as siglas (ex.: `RR SE`). Para um shapefile baixado do car.gov.br: `python -m ingest.carregar ~/Downloads/SHAPE_1600303.zip`.

### Testes

```bash
pytest -q
```

Os testes de linguagem, privacidade e carga rodam sem banco. Os de integração (`tests/test_api.py`) rodam contra o PostGIS carregado e são pulados se ele não estiver disponível.

### Benchmark

```bash
INOVACAR_LIMITE_REQ_POR_MINUTO=100000 uvicorn app.main:app --port 8000
python scripts/benchmark.py --url http://localhost:8000 --n 300
```

O resultado vai para [docs/benchmark.md](docs/benchmark.md).

## Exemplos de uso

```bash
# Um imóvel pelo código do recibo
curl "http://localhost:8000/v1/imoveis/DF-5300108-0000AE7221F4458582BE077E36495213"

# Quem está neste ponto? (ex.: conferência para crédito rural)
curl "http://localhost:8000/v1/imoveis/ponto?lat=-15.964&lon=-47.798"

# Imóveis ativos com passivo a regularizar, acima de 100 ha, no Amapá, do maior para o menor
curl "http://localhost:8000/v1/imoveis?uf=AP&status=AT&condicao=aguardando_regularizacao&area_min=100&ordenar=area_desc&limite=20"

# Imóveis que tocam a gleba de um financiamento, com a área de sobreposição
curl -X POST http://localhost:8000/v1/imoveis/intersecao -H "Content-Type: application/json" \
     -d '{"geometria": {"type":"Polygon","coordinates":[[[-47.80,-15.965],[-47.797,-15.965],[-47.797,-15.962],[-47.80,-15.962],[-47.80,-15.965]]]}}'

# Recorte para download: pequenas propriedades analisadas em Macapá, em CSV
curl -o recorte.csv "http://localhost:8000/v1/exportar?municipio=Macapá&mf_max=4&condicao=analisado&formato=csv"

# Pergunta em linguagem simples
curl -G "http://localhost:8000/v1/perguntar" --data-urlencode "q=quantos imóveis ativos há em Macapá?"
```

No QGIS: *Camada → Adicionar camada → WFS / OGC API – Features*, URL `http://localhost:8000/ogc`.

## Proteção de dados (LGPD)

- Só os campos listados em `app/privacidade.py` (`CAMPOS_PUBLICOS`) existem no banco e na API.
- Na carga, colunas com nomes de dado pessoal (CPF, CNPJ, nome, proprietário, telefone, e-mail, endereço) são descartadas, e os textos livres passam por mascaramento.
- O registro de acesso guarda um hash do IP com sal diário, nunca o IP em claro. Perguntas em linguagem simples são mascaradas antes de ir para o registro.
- As conexões da API com o banco são somente leitura e têm tempo máximo por consulta.

## Estrutura

```
app/            API (FastAPI), consultas, cache, privacidade, linguagem simples, OGC, demonstração
ingest/         download do WFS e carga no PostGIS
sql/            esquema do banco
tests/          testes unitários e de integração
scripts/        benchmark
docs/           benchmark com dados reais
```

## Licença

Apache-2.0 (ver [LICENSE](LICENSE) e [NOTICE](NOTICE)). A propriedade intelectual continua com os autores. Dados: Sicar (SFB/MGI). Mapa base: © colaboradores do OpenStreetMap.

Desenvolvido pela **NOKAHI Consultoria e Soluções Ltda** (Belo Horizonte/MG).
