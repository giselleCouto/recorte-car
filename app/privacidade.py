"""Proteção de dados pessoais (LGPD, Lei nº 13.709/2018).

Três camadas, nesta ordem:

1. Lista de campos permitidos: só atributos públicos do imóvel entram no
   banco. Qualquer coluna fora da lista (nome, CPF, telefone do declarante,
   se aparecerem em algum arquivo de origem) é descartada na carga.
2. Mascaramento de texto: os campos de texto livre que entram passam por um
   filtro que troca CPF, CNPJ, e-mail e telefone por máscaras.
3. Registros de acesso sem dado pessoal: o IP de quem consulta vira um hash
   com sal diário e nunca é gravado em claro.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date

# Campos que a API pode expor. Nenhum deles identifica pessoa natural.
CAMPOS_PUBLICOS: dict[str, str] = {
    "cod_imovel": "Número do recibo de inscrição do imóvel no CAR",
    "uf": "Unidade da Federação",
    "cod_municipio_ibge": "Código IBGE do município",
    "municipio": "Nome do município",
    "status": "Situação do cadastro (AT, PE, SU, CA)",
    "condicao": "Etapa em que a análise do cadastro se encontra",
    "tipo": "Tipo de imóvel (IRU, AST, PCT)",
    "area_ha": "Área declarada do imóvel, em hectares",
    "modulos_fiscais": "Área do imóvel em módulos fiscais",
    "criado_em": "Data de inscrição no CAR",
    "atualizado_em": "Data da última atualização do cadastro",
}

# Nomes de colunas que nunca devem ser carregadas, mesmo que apareçam na origem.
PADRAO_COLUNA_PESSOAL = re.compile(
    r"(cpf|cnpj|nome|nom_|propriet|possuid|declarant|telefone|fone|email|e_mail|endereco|rg_|nascimento)",
    re.IGNORECASE,
)

_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_TELEFONE = re.compile(r"(\(?\b\d{2}\)?\s?)?\b9?\d{4}-?\d{4}\b")


def coluna_pessoal(nome: str) -> bool:
    return bool(PADRAO_COLUNA_PESSOAL.search(nome))


def mascarar_texto(texto: str | None) -> str | None:
    """Troca identificadores pessoais por máscaras. A ordem importa: o CNPJ
    é testado antes do CPF, e o telefone por último."""
    if texto is None:
        return None
    t = _EMAIL.sub("[e-mail removido]", texto)
    t = _CNPJ.sub("**.***.***/****-**", t)
    t = _CPF.sub("***.***.***-**", t)
    t = _TELEFONE.sub("[telefone removido]", t)
    return t


def filtrar_publico(registro: dict) -> dict:
    """Mantém só os campos públicos de um registro."""
    return {k: v for k, v in registro.items() if k in CAMPOS_PUBLICOS}


_SAL = {"dia": None, "valor": b""}


def pseudonimizar_ip(ip: str | None) -> str:
    """Hash do IP com sal que muda todo dia. Permite limitar abuso sem
    guardar o IP, e não permite ligar acessos de dias diferentes."""
    hoje = date.today()
    if _SAL["dia"] != hoje:
        _SAL["dia"] = hoje
        _SAL["valor"] = secrets.token_bytes(16)
    return hashlib.sha256(_SAL["valor"] + (ip or "").encode()).hexdigest()[:16]
