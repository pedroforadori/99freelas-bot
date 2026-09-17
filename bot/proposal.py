from bot import ai_writer
from bot.logger_setup import get_logger
from bot.utils import format_currency_br

log = get_logger(__name__)

# Usado só se o texto final (de QUALQUER origem — IA ou template fixo) violar
# check_text_safety mesmo assim. Sem placeholders, sem risco nenhum.
_SAFE_FALLBACK_TEXTO = "Olá! Tenho interesse nesse projeto e gostaria de conversar sobre os detalhes. Fico à disposição."

# Rótulos exibidos no Telegram (ver notifier._approval_text/notify_proposal_result) pra
# deixar claro de onde veio o valor/prazo de cada proposta — a IA só é chamada pra sugerir
# preço quando não há nem orçamento do cliente nem média de propostas concorrentes (ver
# build_proposal abaixo), então o usuário precisa saber qual base foi usada antes de aprovar.
ORIGEM_LABELS = {
    "ia": "🤖 Sugerido pela IA (sem orçamento do cliente nem propostas concorrentes)",
    "menor_proposta": "📊 Baseado na média das propostas concorrentes",
    "orcamento_cliente": "💰 Baseado no orçamento do cliente",
    "fixo": "📌 Valor fixo configurado",
}


def _aplicar_desconto_competitivo(oferta_sugerida: float, proposal_cfg: dict) -> float:
    """
    Desconto paliativo sobre a sugestão de preço da IA (ver build_proposal/
    ai_writer.suggest_price_and_deadline): por experiência do usuário no 99Freelas, a IA
    tende a sugerir um preço de mercado "real", mas o cliente geralmente fecha com o
    freelancer mais barato — arredonda a sugestão pra baixo no milhar mais próximo e
    desconta mais `ia_desconto_milhar` reais (ex: R$2400 -> R$1000, R$3300 -> R$2000).
    Só se aplica a sugestões >= R$1000 (abaixo disso já é considerado baixo o suficiente);
    nunca deixa o resultado abaixo de `ia_preco_minimo`. Ainda não validado com dados reais
    — os dois parâmetros existem em config.yaml especificamente pra serem ajustados
    conforme o resultado das próximas propostas.
    """
    if oferta_sugerida < 1000:
        return oferta_sugerida
    desconto = proposal_cfg.get("ia_desconto_milhar", 1000)
    piso = proposal_cfg.get("ia_preco_minimo", 150)
    ajustada = (oferta_sugerida // 1000) * 1000 - desconto
    return max(ajustada, piso)


def _build_texto(project: dict, config: dict, oferta: float, prazo_dias: int, full_description: str | None) -> str:
    proposal_cfg = config.get("proposal", {})
    modo = proposal_cfg.get("texto_modo", "fixo")

    if modo == "ia" and full_description:
        gerado = ai_writer.generate_proposal_text(project, full_description, config)
        if gerado:
            return gerado
        log.warning("Geração via IA indisponível/rejeitada para '%s' — usando template fixo.", project.get("title"))

    template = proposal_cfg.get("texto", "Olá! Tenho interesse em {titulo}.")
    return template.format(
        titulo=project.get("title", ""),
        oferta=format_currency_br(oferta),
        prazo_dias=prazo_dias,
    )


def build_proposal(
    project: dict,
    config: dict,
    lowest_bid: float | None = None,
    full_description: str | None = None,
) -> dict | None:
    """
    Monta oferta, prazo e texto da proposta. Retorna dict pronto pra ser usado pelo
    submitter: {"oferta", "prazo_dias", "texto"} — ou None no único caso em que não há
    NENHUM dado pra basear um preço (ver ramo "menor_proposta" abaixo) e a IA também não
    conseguiu sugerir um valor; nesse caso não faz sentido montar uma proposta com
    oferta=0, e o chamador (submitter.prepare_proposal) deve tratar como falha de preparo.

    lowest_bid: valor médio das propostas concorrentes já enviadas nesse projeto (lido da
    página de envio de proposta), usado quando oferta_estrategia == "menor_proposta". None
    se o projeto ainda não tem propostas suficientes acumuladas pra calcular a média —
    comum em projetos recém-publicados, o alvo do filtro de idade deste bot.

    full_description: descrição completa do projeto (lida da página do projeto, sem o
    truncamento da listagem). Usada tanto pra gerar o texto via IA (proposal.texto_modo
    == "ia") quanto, quando lowest_bid e o orçamento do cliente estão ausentes, pra pedir
    à IA uma sugestão de valor/prazo (ver bot/ai_writer.suggest_price_and_deadline) —
    sem essa segunda IA-sugestão, o comportamento seria cair direto pra oferta_fixa ou 0.
    """
    proposal_cfg = config.get("proposal", {})
    estrategia = proposal_cfg.get("oferta_estrategia", "orcamento_cliente")
    budget = project.get("budget")
    orcamento_cliente = budget or proposal_cfg.get("oferta_fixa") or 0
    prazo_dias = proposal_cfg.get("prazo_dias", 7)

    if estrategia == "menor_proposta" and lowest_bid:
        undercut_percent = proposal_cfg.get("undercut_percent", 5)
        oferta = round(lowest_bid * (1 - undercut_percent / 100), 2)
        origem = "menor_proposta"
    elif estrategia == "menor_proposta" and not lowest_bid and not budget and full_description:
        # Nem "menor proposta" (média concorrente) nem orçamento do cliente disponíveis —
        # em vez de cair direto pra oferta_fixa/0, pede à IA uma sugestão coerente com o
        # escopo descrito, buscando um valor baixo pra maximizar aceitação (decisão
        # explícita do usuário: sem piso fixo configurado, a IA julga o valor por projeto).
        sugestao = ai_writer.suggest_price_and_deadline(project, full_description, config)
        if sugestao is None:
            log.warning(
                "Sem menor proposta, sem orçamento do cliente, e a IA não conseguiu sugerir "
                "preço/prazo pra '%s' — proposta não pode ser montada.",
                project.get("title"),
            )
            return None
        oferta_sugerida_ia, prazo_dias = sugestao
        oferta = _aplicar_desconto_competitivo(oferta_sugerida_ia, proposal_cfg)
        origem = "ia"
    elif estrategia == "fixo" and proposal_cfg.get("oferta_fixa") is not None:
        oferta = proposal_cfg["oferta_fixa"]
        origem = "fixo"
    else:
        # sem proposta concorrente pra usar de base (ou estratégia orcamento_cliente):
        # usa o orçamento anunciado pelo cliente; se não houver, cai pra oferta_fixa ou 0
        oferta = orcamento_cliente
        origem = "orcamento_cliente"

    texto = _build_texto(project, config, oferta, prazo_dias, full_description)

    # Checagem final, INDEPENDENTE da origem do texto (IA já é checada dentro de
    # generate_proposal_text, mas o template fixo de config.yaml nunca passava por isso —
    # essa é a rede de segurança que garante que nem um template mal configurado consiga
    # colocar contato/valor/prazo dentro do texto da proposta).
    violation = ai_writer.check_text_safety(texto)
    if violation:
        log.warning(
            "Texto final da proposta pra '%s' violou regra de segurança (%s) — usando "
            "fallback mínimo genérico em vez disso. Confira o template 'texto' em config.yaml.",
            project.get("title"),
            violation,
        )
        texto = _SAFE_FALLBACK_TEXTO

    resultado = {"oferta": oferta, "prazo_dias": prazo_dias, "texto": texto, "origem_valor": origem}
    if origem == "ia" and oferta_sugerida_ia != oferta:
        # Guarda o valor bruto sugerido pela IA (antes do desconto competitivo) pra
        # notifier.py mostrar os dois lado a lado no Telegram — ajuda a validar/lapidar
        # ia_desconto_milhar/ia_preco_minimo em config.yaml com dados reais ao longo do tempo.
        resultado["oferta_sugerida_ia"] = oferta_sugerida_ia

    log.info(
        "Proposta montada para '%s': oferta=R$%s, prazo=%sd, origem=%s%s",
        project.get("title"), oferta, prazo_dias, origem,
        f" (IA sugeriu R${oferta_sugerida_ia})" if "oferta_sugerida_ia" in resultado else "",
    )

    return resultado
