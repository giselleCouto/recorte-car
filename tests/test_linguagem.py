from datetime import date

from app.linguagem import explicar, fmt_num, interpretar

MUNICIPIOS = [
    {"cod_municipio_ibge": 1600303, "municipio": "Macapá", "uf": "AP"},
    {"cod_municipio_ibge": 1600600, "municipio": "Santana", "uf": "AP"},
    {"cod_municipio_ibge": 1600535, "municipio": "Porto Grande", "uf": "AP"},
    {"cod_municipio_ibge": 1600105, "municipio": "Amapá", "uf": "AP"},
    {"cod_municipio_ibge": 5300108, "municipio": "Brasília", "uf": "DF"},
]
HOJE = date(2026, 11, 12)


def test_codigo_do_imovel():
    p = interpretar("situação do DF-5300108-0000AE7221F4458582BE077E36495213?", MUNICIPIOS, HOJE)
    assert p.acao == "imovel"
    assert p.filtros.cod_imovel == ["DF-5300108-0000AE7221F4458582BE077E36495213"]
    assert p.url_api() == "/v1/imoveis/DF-5300108-0000AE7221F4458582BE077E36495213"


def test_coordenada_lat_lon_e_invertida():
    p = interpretar("-15.9640, -47.7980", MUNICIPIOS, HOJE)
    assert p.acao == "ponto" and p.filtros.lat == -15.964 and p.filtros.lon == -47.798
    q = interpretar("o ponto -47.7980; -15.9640 tem CAR?", MUNICIPIOS, HOJE)
    assert q.filtros.lat == -15.964 and q.filtros.lon == -47.798


def test_coordenada_com_virgula_decimal():
    p = interpretar("tem imóvel em -15,9640; -47,7980?", MUNICIPIOS, HOJE)
    assert p.acao == "ponto" and p.filtros.lat == -15.964


def test_contagem_por_municipio_e_status():
    p = interpretar("Quantos imóveis ativos há em Macapá?", MUNICIPIOS, HOJE)
    assert p.acao == "resumo"
    assert p.filtros.cod_municipio == 1600303
    assert p.filtros.status == ["AT"]
    assert "AT" in p.termos


def test_area_a_recompor_maiores_no_estado():
    p = interpretar("Os 10 maiores imóveis com área a recompor no Amapá", MUNICIPIOS, HOJE)
    assert p.acao == "listar"
    assert p.filtros.uf == "AP" and p.filtros.cod_municipio is None  # Amapá aqui é o estado
    assert p.filtros.condicao == ["aguardando_regularizacao"]
    assert p.ordenar == "area_desc" and p.limite == 10


def test_municipio_amapa_quando_explicito():
    p = interpretar("imóveis no município de Amapá", MUNICIPIOS, HOJE)
    assert p.filtros.cod_municipio == 1600105


def test_ativos_ambientais_nao_vira_status_ativo():
    p = interpretar("imóveis com ativos ambientais em Santana", MUNICIPIOS, HOJE)
    assert p.filtros.status is None
    assert p.filtros.condicao == ["conforme_com_ativos"]


def test_area_em_hectares_e_modulos():
    p = interpretar("imóveis com mais de 1.000 hectares em Porto Grande", MUNICIPIOS, HOJE)
    assert p.filtros.area_min == 1000
    q = interpretar("imóveis entre 2 e 4 módulos fiscais no DF", MUNICIPIOS, HOJE)
    assert (q.filtros.mf_min, q.filtros.mf_max) == (2, 4)
    r = interpretar("imóveis com até 50 ha em Santana", MUNICIPIOS, HOJE)
    assert r.filtros.area_max == 50


def test_pequenas_propriedades_e_conformidade():
    p = interpretar("Pequenas propriedades em conformidade no DF", MUNICIPIOS, HOJE)
    assert p.filtros.mf_max == 4
    assert p.filtros.condicao == ["conforme"]
    assert p.filtros.cod_municipio == 5300108 or p.filtros.uf == "DF"


def test_periodo_de_atualizacao():
    p = interpretar("Imóveis atualizados nos últimos 12 meses em Santana", MUNICIPIOS, HOJE)
    assert p.filtros.atualizado_desde == date(2025, 11, 17)
    q = interpretar("imóveis inscritos desde 2024 em Macapá", MUNICIPIOS, HOJE)
    assert q.filtros.criado_desde == date(2024, 1, 1)


def test_tipos_assentamento_e_pct():
    p = interpretar("Imóveis de assentamento em Porto Grande", MUNICIPIOS, HOJE)
    assert p.filtros.tipo == ["AST"]
    q = interpretar("territórios quilombolas no Amapá", MUNICIPIOS, HOJE)
    assert q.filtros.tipo == ["PCT"]


def test_pergunta_sem_filtro_nao_executa():
    p = interpretar("qual a capital da França?", MUNICIPIOS, HOJE)
    assert p.acao == "nada" and p.avisos


def test_tentativa_de_injecao_vira_texto_inofensivo():
    p = interpretar("Macapá'; DROP TABLE car.imovel; --", MUNICIPIOS, HOJE)
    # Só sobra o filtro de município; nenhum texto livre chega ao SQL.
    assert p.filtros.model_dump(exclude_none=True) == {"cod_municipio": 1600303}


def test_url_equivalente_listar():
    p = interpretar("imóveis suspensos em Santana", MUNICIPIOS, HOJE)
    assert p.url_api().startswith("/v1/imoveis?cod_municipio=1600600&status=SU")


def test_explicacao_resumo():
    p = interpretar("quantos imóveis há em Santana?", MUNICIPIOS, HOJE)
    texto = explicar(
        p,
        {"imoveis": 1091, "area_total_ha": 12345.6, "area_mediana_ha": 20.5,
         "por_status": {"Ativo": 1000, "Pendente": 91}, "por_porte": {"pequeno (até 4 módulos fiscais)": 1000}},
        ["AP", "DF"],
    )
    assert "1.091 imóveis" in texto and "12.345,6 ha" in texto


def test_formatacao_numeros():
    assert fmt_num(1234567.891) == "1.234.567,9"
    assert fmt_num(10, 0) == "10"
