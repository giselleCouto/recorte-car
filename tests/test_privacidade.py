from app.privacidade import coluna_pessoal, filtrar_publico, mascarar_texto, pseudonimizar_ip


def test_mascara_cpf_cnpj_email_telefone():
    t = mascarar_texto("Fulano CPF 123.456.789-09, empresa 12.345.678/0001-95, fulano@exemplo.com, (61) 99876-5432")
    assert "123.456.789-09" not in t
    assert "12.345.678/0001-95" not in t
    assert "fulano@exemplo.com" not in t
    assert "99876-5432" not in t
    assert "***.***.***-**" in t and "**.***.***/****-**" in t


def test_texto_sem_dado_pessoal_fica_igual():
    assert mascarar_texto("Analisado, em conformidade com a Lei nº 12.651/2012") == (
        "Analisado, em conformidade com a Lei nº 12.651/2012"
    )
    assert mascarar_texto(None) is None


def test_colunas_pessoais_sao_reconhecidas():
    for c in ("NOM_PROPRIETARIO", "cpf_cnpj", "email", "telefone", "nome_declarante"):
        assert coluna_pessoal(c), c
    for c in ("cod_imovel", "num_area", "condicao", "municipio"):
        assert not coluna_pessoal(c), c


def test_filtrar_publico():
    assert filtrar_publico({"cod_imovel": "X", "cpf": "1", "area_ha": 2}) == {"cod_imovel": "X", "area_ha": 2}


def test_ip_pseudonimizado():
    a = pseudonimizar_ip("200.1.2.3")
    assert a == pseudonimizar_ip("200.1.2.3")
    assert a != pseudonimizar_ip("200.1.2.4")
    assert "200" not in a or len(a) == 16
