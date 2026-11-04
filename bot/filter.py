"""
Decide se um projeto é "aderente ao perfil" com base no config.yaml.

O usuário ainda não definiu os critérios finais (categorias, palavras-chave,
faixa de orçamento) — este módulo já está pronto pra quando isso for
preenchido em config.yaml. Enquanto os campos estiverem vazios, o filtro é
permissivo (deixa passar quase tudo), o que é intencional pra facilitar teste
do resto do pipeline, mas NÃO deve ser usado assim em produção.
"""
import re

from bot.logger_setup import get_logger

log = get_logger(__name__)

# "Projeto" que é outro freelancer anunciando o próprio serviço (pedido do usuário,
# 2026-10-28 — caso real: "Criadora de Sites | Desenvolvedora Web", "Me chamo Nicole e
# trabalho com desenvolvimento web..."). Pontua sinais; 3 ou mais = anúncio. Os casos que
# passarem aqui podem ser conferidos pela IA (ai_writer.classify_project, no prepare_proposal).
_PROFISSAO = r"(?:criador(?:a)? de sites?|desenvolvedor(?:a)?|programador(?:a)?|web ?designer|designer|freelancer)"
_SINAIS_ANUNCIO = [
    (2, "apresentação pessoal", re.compile(
        r"\b(?:me chamo|meu nome (?:é|e)|sou (?:um |uma )?(?:desenvolvedor|desenvolvedora|programador|programadora"
        r"|designer|freelancer|profissional))\b")),
    (1, "oferece serviço", re.compile(
        r"\b(?:trabalho com|atuo (?:com|como|na|no)|ofereço|meus serviços|ajudo (?:empresas|profissionais|negócios|você)"
        r"|minha experiência|meu portf[óo]lio|me conte o que (?:você|vc) precisa|vamos conversar"
        r"|entre em contato comigo|formação (?:técnica|em))\b")),
]
_PEDIDO = re.compile(
    r"\b(?:preciso de|preciso que|procuro|busco|necessito|estou procurando|gostaria de contratar|quero (?:um|uma|que))\b"
)
_TITULO_PROFISSAO = re.compile(rf"(?:^|\|)\s*{_PROFISSAO}\b")
# "Desenvolvedor para app de delivery", "Procura-se programador": título de cliente, não de anúncio.
_TITULO_DE_PEDIDO = re.compile(r"\b(?:para|pra|procura|procuro|precisa|preciso|busco|contrata|vaga)")


def anuncio_de_freelancer(title: str, description: str) -> str | None:
    """Motivo (sinais achados) se o texto parece anúncio de freelancer, não pedido de cliente; senão None."""
    title = (title or "").lower()
    text = f"{title}\n{(description or '').lower()}"
    pontos, sinais = 0, []
    if _TITULO_PROFISSAO.search(title) and not _TITULO_DE_PEDIDO.search(title):
        pontos += 2
        sinais.append("título de profissão")
    for peso, nome, padrao in _SINAIS_ANUNCIO:
        achados = sorted(set(padrao.findall(text)))
        if achados:
            pontos += peso * len(achados)
            sinais.append(f"{nome} ({', '.join(achados)})")
    if _PEDIDO.search(text):
        pontos -= 3
    return "; ".join(sinais) if pontos >= 3 else None


def ignora_anuncios(config: dict) -> bool:
    return (config.get("anuncios_de_freelancer") or {}).get("ignorar", True) is not False


# Projeto fora da área — design gráfico, vetor, vídeo, protótipo sem programação (pedido do
# usuário, 2026-10-29 — caso real 794898: vetorizar PNG no Illustrator/Inkscape pra corte a
# laser, veio pela subcategoria "UX/UI e Web Design"). Conservador: só exclui com termo de
# design e NENHUM de desenvolvimento — "tenho o logotipo, quero o site" passa pra IA decidir
# (ai_writer.classify_project, no prepare_proposal).
_DESIGN = re.compile(
    r"\b(?:vetoriza\w*|vectoriza\w*|vetorizar|illustrator|ilustrator|inkscape|inscape|corel ?draw|photoshop"
    r"|ilustraç\w+|logotipos?|logomarcas?|identidade visual|corte a laser|plotter|mockups?"
    r"|edição de (?:vídeos?|videos?|fotos?)|editar (?:vídeos?|videos?)|protótipos?|prototipa\w*|figma)\b"
)
_DEV = re.compile(
    r"\b(?:sites?|website|sistemas?|aplicativos?|apps?|plataformas?|landing ?pages?|wordpress|programa\w*"
    r"|código|codigo|integra\w*|apis?|banco de dados|automa\w*|bots?|scripts?|front-?end|back-?end"
    r"|implementa\w*|desenvolv\w*|html|css|javascript|python|php)\b"
)


def fora_da_area(title: str, description: str) -> str | None:
    """Motivo (termos de design achados) se o projeto claramente não envolve programação; senão None."""
    text = f"{title or ''}\n{description or ''}".lower()
    design = sorted(set(_DESIGN.findall(text)))
    if not design or _DEV.search(text):
        return None
    return f"termos de design: {', '.join(design)}"


def ignora_fora_da_area(config: dict) -> bool:
    return (config.get("fora_da_area") or {}).get("ignorar", True) is not False


def excluded_keyword(text: str, config: dict) -> str | None:
    """
    Primeira palavra de keywords_exclude contida em `text` (case-insensitive), ou None.
    Também usada na descrição COMPLETA do projeto (submitter.prepare_proposal), já que a
    listagem só mostra um trecho dela.
    """
    text = (text or "").lower()
    for kw in config.get("keywords_exclude") or []:
        if kw.lower() in text:
            return kw.lower()
    return None


def is_match(project: dict, config: dict) -> tuple[bool, str]:
    """Retorna (aderente: bool, motivo: str) para fins de log."""
    title = (project.get("title") or "").lower()
    description = (project.get("description") or "").lower()
    category = (project.get("category") or "").lower()
    budget = project.get("budget")  # float ou None

    text = f"{title} {description}"

    max_age_minutes = config.get("max_project_age_minutes")
    posted_minutes_ago = project.get("posted_minutes_ago")
    if max_age_minutes is not None and posted_minutes_ago is not None and posted_minutes_ago > max_age_minutes:
        return False, f"publicado há {posted_minutes_ago:.0f}min, acima do limite de {max_age_minutes}min"

    kw = excluded_keyword(text, config)
    if kw:
        return False, f"contém palavra-chave excluída: '{kw}'"

    if ignora_anuncios(config):
        anuncio = anuncio_de_freelancer(project.get("title"), project.get("description"))
        if anuncio:
            return False, f"parece anúncio de freelancer: {anuncio}"

    if ignora_fora_da_area(config):
        fora = fora_da_area(project.get("title"), project.get("description"))
        if fora:
            return False, f"fora da área: {fora}"

    categories = [c.lower() for c in config.get("categories", [])]
    if categories and category not in categories:
        return False, f"categoria '{category}' fora da lista aceita"

    include = [k.lower() for k in config.get("keywords_include", [])]
    if include and not any(kw in text for kw in include):
        return False, "nenhuma palavra-chave incluída encontrada"

    budget_min = config.get("budget_min")
    budget_max = config.get("budget_max")
    if budget is not None:
        if budget_min is not None and budget < budget_min:
            return False, f"orçamento R${budget} abaixo do mínimo R${budget_min}"
        if budget_max is not None and budget > budget_max:
            return False, f"orçamento R${budget} acima do máximo R${budget_max}"

    return True, "aderente"
