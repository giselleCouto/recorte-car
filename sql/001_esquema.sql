-- InovaCAR MVP — esquema do espelho de consulta do CAR
-- PostgreSQL 15+ com PostGIS 3.3+. SIRGAS 2000 (EPSG:4674), padrão e-PING para dados geográficos.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS car;

-- Uma linha por imóvel. Só atributos públicos (ver app/privacidade.py).
CREATE TABLE IF NOT EXISTS car.imovel (
    cod_imovel          text PRIMARY KEY,
    uf                  char(2)      NOT NULL,
    cod_municipio_ibge  integer      NOT NULL,
    municipio           text         NOT NULL,
    status              text         NOT NULL,           -- AT, PE, SU, CA
    condicao            text,                            -- etapa da análise
    tipo                text         NOT NULL,           -- IRU, AST, PCT
    area_ha             numeric(14,4),
    modulos_fiscais     numeric(12,4),
    criado_em           timestamptz,
    atualizado_em       timestamptz,
    geom                geometry(MultiPolygon, 4674),
    geom_web            geometry(MultiPolygon, 3857),       -- pré-projetada para vector tiles
    fonte              text         NOT NULL,
    carregado_em        timestamptz  NOT NULL DEFAULT now()
);

-- Índice espacial: é ele que faz "ponto dentro do imóvel" e "bbox" responderem em milissegundos.
CREATE INDEX IF NOT EXISTS imovel_geom_gist      ON car.imovel USING gist (geom);
CREATE INDEX IF NOT EXISTS imovel_geom_web_gist  ON car.imovel USING gist (geom_web);
CREATE INDEX IF NOT EXISTS imovel_municipio_idx  ON car.imovel (cod_municipio_ibge);
CREATE INDEX IF NOT EXISTS imovel_uf_idx         ON car.imovel (uf);
CREATE INDEX IF NOT EXISTS imovel_status_idx     ON car.imovel (status);
CREATE INDEX IF NOT EXISTS imovel_condicao_idx   ON car.imovel (condicao);
CREATE INDEX IF NOT EXISTS imovel_atualizado_idx ON car.imovel (atualizado_em);
CREATE INDEX IF NOT EXISTS imovel_area_idx       ON car.imovel (area_ha);

-- Resumo por município, recalculado a cada carga. Atende "quantos imóveis há em X" sem varrer a tabela.
DROP MATERIALIZED VIEW IF EXISTS car.municipio_resumo;
CREATE MATERIALIZED VIEW car.municipio_resumo AS
SELECT
    cod_municipio_ibge,
    min(municipio)                                   AS municipio,
    min(uf)                                          AS uf,
    count(*)                                         AS imoveis,
    round(sum(area_ha), 2)                           AS area_total_ha,
    count(*) FILTER (WHERE status = 'AT')            AS ativos,
    count(*) FILTER (WHERE status = 'PE')            AS pendentes,
    count(*) FILTER (WHERE status = 'SU')            AS suspensos,
    count(*) FILTER (WHERE status = 'CA')            AS cancelados,
    ST_Extent(geom)::box2d                           AS extensao
FROM car.imovel
GROUP BY cod_municipio_ibge;

CREATE UNIQUE INDEX IF NOT EXISTS municipio_resumo_pk ON car.municipio_resumo (cod_municipio_ibge);

-- Registro de cargas (proveniência dos dados exibida na API).
CREATE TABLE IF NOT EXISTS car.carga (
    id            bigserial PRIMARY KEY,
    uf            char(2) NOT NULL,
    fonte         text    NOT NULL,
    registros     integer NOT NULL,
    descartados   integer NOT NULL DEFAULT 0,
    bytes_origem  bigint,                      -- tamanho do download completo, para medir a economia de banda
    iniciada_em   timestamptz NOT NULL,
    concluida_em  timestamptz NOT NULL DEFAULT now()
);
