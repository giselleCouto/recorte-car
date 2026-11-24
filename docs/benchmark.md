# Benchmark da API (dados reais do CAR)

Medido em 13/11/2026 15:44, numa máquina de desenvolvimento (Windows, PostgreSQL 17 + PostGIS 3.6 locais, 1 processo da API). Base: 38.865 imóveis de AP e DF.

"Frio" é a primeira chamada (vai ao banco); "quente" é a repetição (sai do cache da API). Os tempos são medidos no cliente, incluindo HTTP local. "—": rota sem cache.

| Consulta | Chamadas | p50 frio (ms) | p95 frio (ms) | p50 quente (ms) | p95 quente (ms) | Resposta média |
|---|---:|---:|---:|---:|---:|---:|
| Imóvel por código | 300 | 8,1 | 10,2 | 5,8 | 7,4 | 1,1 KB |
| Imóveis em um ponto (lat/lon) | 300 | 12,5 | 17,9 | 6,1 | 7,7 | 16,8 KB |
| Lista por município (100 por página) | 17 | 50,2 | 82,8 | 7,6 | 9,2 | 77,1 KB |
| Resumo por município | 17 | 23,2 | 44,6 | 7,7 | 8,7 | 0,8 KB |
| Pergunta em linguagem simples | 4 | 75,5 | 89,6 | — | — | 12,3 KB |

**Concorrência:** 300 consultas por ponto, sem cache, com 16 clientes simultâneos: 101 consultas/s, p50 14,5 ms, p95 252,4 ms. Com um único processo da API, a cauda cresce sob concorrência; em produção o docker-compose sobe 2 workers e a API pode ganhar réplicas atrás de um balanceador.

## Economia de tráfego

| UF | Download completo da UF | Consulta de 1 imóvel | Proporção |
|---|---:|---:|---:|
| AP | 18,5 MB | 1,1 KB | 17.440× menor |
| DF | 28,0 MB | 1,1 KB | 26.424× menor |

Tamanho no banco (tabela + índices): 55 MB.

Hoje, quem precisa conferir um imóvel baixa o arquivo do município ou da UF inteira no Sicar. Com a API, a mesma pergunta trafega alguns KB e não chega ao Sicar: é respondida pelo espelho.
