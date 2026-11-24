"""Consulta em linguagem simples.

Transforma uma pergunta em português num *plano de consulta*: uma ação
(imóvel, ponto, listar ou resumo) mais um conjunto de filtros validados
pelo mesmo modelo `Filtros` da API. O plano nunca contém SQL. Se a pergunta
pedir algo fora do vocabulário, o plano diz o que não foi entendido, em
vez de inventar.

O interpretador é determinístico (regras e vocabulário controlado): roda
sem GPU, sem chave de API e sem enviar a pergunta a terceiros, e cada
decisão pode ser auditada. Um modelo de linguagem pode ser acoplado depois
para reescrever perguntas difíceis, mas a saída dele teria de passar pela
mesma validação.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlencode

from .consultas import CONDICOES, STATUS, TIPOS, Filtros

UFS = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
    "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
    "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará",
    "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
    "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins",
}

GLOSSARIO = {
    "AT": "Ativo: o cadastro está válido no Sicar.",
    "PE": "Pendente: o cadastro tem pendências apontadas na análise ou sobreposições a resolver.",
    "SU": "Suspenso: o cadastro foi suspenso por decisão administrativa ou judicial.",
    "CA": "Cancelado: o cadastro deixou de valer.",
    "IRU": "Imóvel rural: propriedade ou posse rural individual.",
    "AST": "Assentamento: projeto de reforma agrária.",
    "PCT": "Povos e comunidades tradicionais: território de uso coletivo.",
    "modulo_fiscal": "Módulo fiscal: unidade de área definida por município (5 a 110 ha). "
    "Até 4 módulos é pequena propriedade (Lei 8.629/1993).",
}


def _sem_acento(t: str) -> str:
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", _sem_acento(t).lower()).strip()


def fmt_num(v: float | int | None, casas: int = 1) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{casas}f}" if casas else f"{int(round(v)):,}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _numero(txt: str, mil: bool = False) -> float:
    txt = txt.strip()
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", txt):
        txt = txt.replace(".", "")
    v = float(txt)
    return v * 1000 if mil else v


@dataclass
class Plano:
    pergunta: str
    acao: str = "listar"  # imovel | ponto | listar | resumo | nada
    filtros: Filtros = field(default_factory=Filtros)
    ordenar: str = "cod"
    limite: int = 20
    entendido: list[str] = field(default_factory=list)
    termos: list[str] = field(default_factory=list)  # chaves do GLOSSARIO a explicar
    avisos: list[str] = field(default_factory=list)

    def url_api(self) -> str:
        f = self.filtros.model_dump(exclude_none=True)
        if self.acao == "imovel" and f.get("cod_imovel"):
            return f"/v1/imoveis/{f['cod_imovel'][0]}"
        if self.acao == "ponto":
            return "/v1/imoveis/ponto?" + urlencode({"lat": f["lat"], "lon": f["lon"]})
        params: list[tuple[str, str]] = []
        for k, v in f.items():
            if k in ("lat", "lon"):
                continue
            if isinstance(v, (list, tuple)):
                params.append((k, ",".join(str(x) for x in v)))
            else:
                params.append((k, str(v)))
        if self.acao == "resumo":
            return "/v1/resumo?" + urlencode(params)
        params += [("ordenar", self.ordenar), ("limite", str(self.limite))]
        return "/v1/imoveis?" + urlencode(params)


# ------------------------------------------------------------- regras

RE_COD = re.compile(r"\b([A-Za-z]{2}-\d{7}-[0-9A-Fa-f]{32})\b")
RE_COORD_PONTO = re.compile(r"(-?\d{1,2}\.\d+)\s*[,;\s]\s*(-?\d{1,2}\.\d+)")
RE_COORD_VIRGULA = re.compile(r"(-?\d{1,2},\d+)\s*(?:;|\s|,\s)\s*(-?\d{1,2},\d+)")
RE_NUM = r"(\d[\d.,]*)\s*(mil\s+)?"
RE_UNID = r"\s*(hectares?|ha\b|modulos?\s+fisca\w*|mf\b)"
RE_MIN = re.compile(
    r"(?:maior(?:es)?\s+(?:do\s+)?que|acima\s+de|mais\s+de|superior(?:es)?\s+a|a\s+partir\s+de|pelo\s+menos|no\s+minimo)\s+"
    + RE_NUM + RE_UNID
)
RE_MAX = re.compile(
    r"(?:menor(?:es)?\s+(?:do\s+)?que|abaixo\s+de|menos\s+de|ate|inferior(?:es)?\s+a|no\s+maximo)\s+" + RE_NUM + RE_UNID
)
RE_ENTRE = re.compile(r"entre\s+" + RE_NUM + r"\s*e\s+" + RE_NUM + RE_UNID)
RE_TOPN = re.compile(r"\b(\d{1,3})\s+(?:maiores|menores|primeiros|mais\s+recentes|imoveis)")


def _intervalo_data(t: str, hoje: date) -> tuple[date, str] | None:
    m = re.search(r"ultim[oa]s?\s+(\d+)\s+(dias?|semanas?|mes(?:es)?|anos?)", t)
    if m:
        n = int(m.group(1))
        unidade = m.group(2)
        dias = n * (1 if unidade.startswith("dia") else 7 if unidade.startswith("semana") else 30 if unidade.startswith("mes") else 365)
        return hoje - timedelta(days=dias), f"nos últimos {n} {unidade}"
    if re.search(r"ultimo\s+mes|mes\s+passado", t):
        return hoje - timedelta(days=30), "no último mês"
    if re.search(r"ultima\s+semana", t):
        return hoje - timedelta(days=7), "na última semana"
    if re.search(r"ultimo\s+ano|ultimos\s+12\s+meses", t):
        return hoje - timedelta(days=365), "no último ano"
    if re.search(r"(?:neste|este|esse|nesse)\s+ano", t):
        return date(hoje.year, 1, 1), f"em {hoje.year}"
    m = re.search(r"(?:desde|apos|depois\s+de|a\s+partir\s+de)\s+(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return d, f"desde {d.strftime('%d/%m/%Y')}"
    m = re.search(r"(?:desde|apos|depois\s+de|a\s+partir\s+de|em)\s+((?:19|20)\d{2})\b", t)
    if m:
        return date(int(m.group(1)), 1, 1), f"desde 1º/1/{m.group(1)}"
    return None


def interpretar(pergunta: str, municipios: list[dict], hoje: date | None = None) -> Plano:
    """Converte a pergunta em um Plano. `municipios` é a lista carregada no banco."""
    hoje = hoje or date.today()
    plano = Plano(pergunta=pergunta.strip()[:500])
    original = plano.pergunta
    t = _norm(original)
    f: dict = {}

    # 1. Código do imóvel: resposta direta.
    m = RE_COD.search(original)
    if m:
        f["cod_imovel"] = [m.group(1).upper()]
        plano.acao = "imovel"
        plano.entendido.append(f"o imóvel de código {m.group(1).upper()}")
        plano.filtros = Filtros(**f)
        return plano

    # 2. Coordenadas: quais imóveis contêm o ponto.
    m = RE_COORD_PONTO.search(original) or RE_COORD_VIRGULA.search(original)
    if m:
        a, b = (_numero(m.group(1).replace(",", ".")), _numero(m.group(2).replace(",", ".")))
        lat, lon = (a, b) if -35 <= a <= 6 and -75 <= b <= -28 else (b, a)
        if -35 <= lat <= 6 and -75 <= lon <= -28:
            f.update(lat=lat, lon=lon)
            plano.acao = "ponto"
            plano.entendido.append(f"o ponto de latitude {fmt_num(lat, 5)} e longitude {fmt_num(lon, 5)}")
            plano.filtros = Filtros(**f)
            return plano
        plano.avisos.append("Vi números que parecem coordenadas, mas estão fora do Brasil; ignorei.")

    # 3. Intenção: contar/resumir ou listar.
    if re.search(r"\b(quant[oa]s|quantidade|total|numero\s+de|resumo|estatistica|panorama|perfil)\b", t):
        plano.acao = "resumo"

    # 4. Lugar: município (mais longo primeiro) e UF.
    nome_mun = None
    for mun in sorted(municipios, key=lambda x: -len(x["municipio"])):
        nm = _norm(mun["municipio"])
        if not re.search(rf"\b{re.escape(nm)}\b", t):
            continue
        # "Amapá" é município e estado; só vale como município se a pergunta disser.
        if nm in {_norm(n) for n in UFS.values()} and not re.search(rf"(municipio|cidade)\s+(de|do|da)\s+{re.escape(nm)}", t):
            if not (nm == "brasilia"):
                continue
        f["cod_municipio"] = mun["cod_municipio_ibge"]
        nome_mun = f"{mun['municipio']} ({mun['uf']})"
        plano.entendido.append(f"no município de {nome_mun}")
        break

    if "cod_municipio" not in f:
        m = re.search(r"\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b", original)
        uf = m.group(1) if m else None
        if not uf:
            baixo = original.lower()
            for sigla, nome in sorted(UFS.items(), key=lambda x: -len(x[1])):
                n_acento = nome.lower()
                if sigla == "PA":
                    achou = re.search(r"\bpará\b", baixo) or re.search(r"estado\s+do\s+para\b", t)
                else:
                    achou = re.search(rf"\b{re.escape(n_acento)}\b", baixo) or re.search(rf"\b{re.escape(_norm(nome))}\b", t)
                if achou:
                    uf = sigla
                    break
        if uf:
            f["uf"] = uf
            plano.entendido.append(f"na UF {UFS[uf]} ({uf})")

    # 5. Situação do cadastro.
    status = []
    for padrao, cod in (
        (r"\bativ[oa]s?\b(?!\s+ambienta)", "AT"),
        (r"\bpendente?s?\b|\bpendencias?\b", "PE"),
        (r"\bsuspens[oa]s?\b", "SU"),
        (r"\bcancelad[oa]s?\b", "CA"),
    ):
        if re.search(padrao, t):
            status.append(cod)
    if status:
        f["status"] = status
        plano.entendido.append("com situação " + " ou ".join(STATUS[s].lower() for s in status))
        plano.termos += status

    # 6. Etapa da análise.
    condicoes = []
    if re.search(r"recompor|recuperar|passivo|deficit|regularizac|a\s+regularizar|irregular", t):
        condicoes.append("aguardando_regularizacao")
    if re.search(r"ativos?\s+ambienta|excedente|cota\s+de\s+reserva|\bcra\b", t):
        condicoes.append("conforme_com_ativos")
    elif re.search(r"em\s+conformidade|\bconformes?\b|\bregulares\b|regularizad", t):
        condicoes.append("conforme")
    if re.search(r"notifica", t):
        condicoes.append("aguardando_notificacao")
    if re.search(r"aguardando\s+analise|sem\s+analise|nao\s+(foram\s+)?analisad|ainda\s+nao\s+analisad", t):
        condicoes.append("aguardando_analise")
    elif re.search(r"\bem\s+analise\b|sendo\s+analisad", t):
        condicoes.append("em_analise")
    elif not condicoes and re.search(r"\b(ja\s+)?analisad[oa]s\b", t):
        condicoes.append("analisado")
    if condicoes:
        f["condicao"] = condicoes
        plano.entendido.append("na etapa: " + "; ou ".join(CONDICOES[c][0].lower() for c in condicoes))

    # 7. Tipo de imóvel.
    tipos = []
    if re.search(r"assentament", t):
        tipos.append("AST")
    if re.search(r"quilomb|indigen|povos\s+e\s+comunidades|comunidades?\s+tradiciona|territorios?\s+tradiciona", t):
        tipos.append("PCT")
    if tipos:
        f["tipo"] = tipos
        plano.entendido.append("do tipo " + " ou ".join(TIPOS[x].lower() for x in tipos))
        plano.termos += tipos

    # 8. Área e porte.
    def unidade_mf(u: str) -> bool:
        return u.startswith("modulo") or u == "mf"

    m = RE_ENTRE.search(t)
    if m:
        a, b = _numero(m.group(1), bool(m.group(2))), _numero(m.group(3), bool(m.group(4)))
        chave = "mf" if unidade_mf(m.group(5)) else "area"
        f[f"{chave}_min"], f[f"{chave}_max"] = min(a, b), max(a, b)
        rot = "módulos fiscais" if chave == "mf" else "ha"
        plano.entendido.append(f"com área entre {fmt_num(min(a, b), 0)} e {fmt_num(max(a, b), 0)} {rot}")
    else:
        for regex, sufixo, palavra in ((RE_MIN, "min", "acima de"), (RE_MAX, "max", "até")):
            m = regex.search(t)
            if m:
                v = _numero(m.group(1), bool(m.group(2)))
                chave = "mf" if unidade_mf(m.group(3)) else "area"
                f[f"{chave}_{sufixo}"] = v
                rot = "módulos fiscais" if chave == "mf" else "ha"
                plano.entendido.append(f"com área {palavra} {fmt_num(v, 0 if v == int(v) else 1)} {rot}")
                if chave == "mf":
                    plano.termos.append("modulo_fiscal")
    if "mf_min" not in f and "mf_max" not in f:
        if re.search(r"pequen[oa]s?\s+(propriedades?|imoveis|produtor)|agricultura\s+familiar|familiar(es)?\b", t):
            f["mf_max"] = 4
            plano.entendido.append("de pequeno porte (até 4 módulos fiscais)")
            plano.termos.append("modulo_fiscal")
        elif re.search(r"medi[oa]s\s+(propriedades?|imoveis)", t):
            f["mf_min"], f["mf_max"] = 4, 15
            plano.entendido.append("de médio porte (4 a 15 módulos fiscais)")
            plano.termos.append("modulo_fiscal")
        elif re.search(r"grandes\s+(propriedades?|imoveis)|latifundi", t):
            f["mf_min"] = 15
            plano.entendido.append("de grande porte (mais de 15 módulos fiscais)")
            plano.termos.append("modulo_fiscal")

    # 9. Datas.
    periodo = _intervalo_data(t, hoje)
    if periodo:
        d, rotulo = periodo
        if re.search(r"inscrit|cadastrad|criad|novos|registrad", t):
            f["criado_desde"] = d
            plano.entendido.append(f"inscritos {rotulo}")
        else:
            f["atualizado_desde"] = d
            plano.entendido.append(f"atualizados {rotulo}")

    # 10. Ordem e quantidade.
    if re.search(r"\bmaiores\b|\bmaior\s+(imovel|area)", t):
        plano.ordenar = "area_desc"
        plano.entendido.append("ordenados do maior para o menor")
    elif re.search(r"\bmenores\b", t):
        plano.ordenar = "area_asc"
        plano.entendido.append("ordenados do menor para o maior")
    elif re.search(r"mais\s+recentes|recem|ultimas?\s+atualizac", t):
        plano.ordenar = "atualizado_desc"
        plano.entendido.append("dos atualizados mais recentemente para os mais antigos")
    m = RE_TOPN.search(t)
    if m:
        plano.limite = max(1, min(int(m.group(1)), 500))
    elif re.search(r"\bo\s+maior\s+(imovel|propriedade)", t):
        plano.limite = 1

    if not f:
        plano.acao = "nada"
        plano.avisos.append(
            "Não encontrei na pergunta um lugar, código, coordenada ou filtro que eu saiba usar. "
            "Experimente: 'quantos imóveis ativos há em Macapá?', "
            "'imóveis com área a recompor no Amapá', ou cole uma coordenada como -15.96, -47.79."
        )
        return plano

    try:
        plano.filtros = Filtros(**f)
    except Exception as erro:  # validação recusou: explica em vez de executar
        plano.acao = "nada"
        plano.avisos.append(f"A pergunta gerou um filtro inválido e não foi executada: {erro}")
    return plano


def _lista_filtros(plano: Plano) -> str:
    return ", ".join(plano.entendido) if plano.entendido else "todos os imóveis carregados"


def explicar(plano: Plano, resultado: dict | None, ufs_carregadas: list[str]) -> str:
    """Resposta em linguagem simples para o resultado de um plano."""
    base = f"Entendi que você quer: imóveis {_lista_filtros(plano)}."
    if plano.acao == "imovel":
        if not resultado:
            return f"Não encontrei o imóvel {plano.filtros.cod_imovel[0]} nas UFs carregadas ({', '.join(ufs_carregadas)})."
        p = resultado["properties"]
        return (
            f"O imóvel {p['cod_imovel']} fica em {p['municipio']} ({p['uf']}), tem {fmt_num(p['area_ha'])} ha "
            f"({fmt_num(p['modulos_fiscais'], 2)} módulos fiscais) e está com situação "
            f"{STATUS.get(p['status'], p['status']).lower()}. Etapa da análise: {p['condicao']}. "
            f"Última atualização: {p['atualizado_em'] or 'sem data'}."
        )
    if plano.acao == "ponto":
        feats = (resultado or {}).get("features", [])
        if not feats:
            return (
                f"Nenhum imóvel do CAR contém esse ponto nas UFs carregadas ({', '.join(ufs_carregadas)}). "
                "Isso não prova que a área está livre de cadastro em outras bases."
            )
        partes = [
            f"{x['properties']['cod_imovel']} ({STATUS.get(x['properties']['status'], '').lower()}, "
            f"{fmt_num(x['properties']['area_ha'])} ha, {x['properties']['condicao']})"
            for x in feats[:5]
        ]
        sobre = " Há mais de um cadastro no mesmo ponto, o que indica sobreposição." if len(feats) > 1 else ""
        return f"O ponto cai dentro de {len(feats)} imóvel(is) do CAR: " + "; ".join(partes) + "." + sobre
    if plano.acao == "resumo":
        r = resultado or {}
        if not r.get("imoveis"):
            return base + " Não há imóveis com esses critérios nas UFs carregadas."
        status = ", ".join(f"{fmt_num(v, 0)} {k.lower()}" for k, v in r["por_status"].items())
        porte = ", ".join(f"{fmt_num(v, 0)} {k}" for k, v in r["por_porte"].items())
        return (
            base
            + f" São {fmt_num(r['imoveis'], 0)} imóveis, somando {fmt_num(r['area_total_ha'])} ha "
            f"(mediana de {fmt_num(r['area_mediana_ha'])} ha por imóvel). Por situação: {status}. Por porte: {porte}."
        )
    r = resultado or {}
    total = r.get("numberMatched", 0)
    if not total:
        return base + " Não há imóveis com esses critérios nas UFs carregadas."
    mostrados = r.get("numberReturned", 0)
    texto = base + f" Encontrei {fmt_num(total, 0)} imóveis, somando {fmt_num(r.get('area_total_ha'))} ha."
    if mostrados < total:
        texto += f" Mostrando {mostrados}; o link da API abaixo traz os demais por paginação."
    return texto
