import math
import random

from bot import ai_writer
from bot.logger_setup import get_logger
from bot.utils import format_currency_br

log = get_logger(__name__)

# Usado só se o texto final (de QUALQUER origem — IA ou template fixo) violar
# check_text_safety mesmo assim. Sem placeholders, sem risco nenhum.
_SAFE_FALLBACK_TEXTO = "Olá! Tenho interesse nesse projeto e gostaria de conversar sobre os detalhes. Fico à disposição."

# Rótulos exibidos no Telegram (ver bot/sources/freelas99/views.py) pra
# deixar claro de onde veio o valor/prazo de cada proposta — a IA só é chamada pra sugerir
# preço quando não há nem orçamento do cliente nem média de propostas concorrentes (ver
# build_proposal abaixo), então o usuário precisa saber qual base foi usada antes de aprovar.
ORIGEM_LABELS = {
    "ia": "🤖 Sugerido pela IA (sem orçamento do cliente nem propostas concorrentes)",
    "menor_proposta": "📊 Baseado na média das propostas concorrentes",  # propostas antigas
    "orcamento_cliente": "💰 Baseado no orçamento do cliente",
    "fixo": "📌 Valor fixo configurado",
    "media_arredondada": "📊 Média das propostas concorrentes, arredondada pra baixo",
}

# Estilo do texto (ver ai_writer._TEXT_VARIANTS), exibido no Telegram e gravado em
# applied_jobs.json pra comparar os estilos depois (ver bot/report.py).
TEXTO_VARIANTE_LABELS = {
    "padrao": "padrão (atual)",
    "pergunta": "curto + pergunta ao cliente",
    "plano": "etapas numeradas",
    "minimo": "mínimo (2-3 frases)",
    "resultado": "foco no resultado",
    "diagnostico": "diagnóstico (aponta um cuidado)",
    "opcoes": "dois caminhos pro cliente escolher",
    "conversa": "conversa informal",
    "template": "template fixo do config.yaml",
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


def _prazo_abaixo_da_media(media_prazo: int, proposal_cfg: dict) -> int:
    """
    Prazo um pouco abaixo da "Duração média estimada" das propostas concorrentes (pedido do
    usuário, 2026-09-25): desconta `prazo_desconto_percent`% (default 20 — ex: média 10 dias
    -> 8 dias), nunca abaixo de 1 dia.
    """
    desconto = proposal_cfg.get("prazo_desconto_percent", 20)
    return max(1, round(media_prazo * (1 - desconto / 100)))


def _build_texto(
    project: dict, config: dict, oferta: float, prazo_dias: int, full_description: str | None
) -> tuple[str, bool, str]:
    """
    Retorna (texto, texto_ia_falhou, texto_variante) — texto_variante é o estilo sorteado
    (ver _sortear_variante_texto), ou "template" quando o texto veio do template fixo. texto_ia_falhou só é True quando texto_modo == "ia",
    havia full_description pra tentar (isto é, a IA de fato foi chamada) e a geração
    falhou/foi rejeitada — é esse caso específico que notifier.py sinaliza na mensagem de
    aprovação com um botão "🔄 Tentar gerar via IA novamente" (ver
    Freelas99Source._on_retry_ia_text), já que só faz sentido reoferecer a tentativa quando a
    causa foi uma falha da IA (ex: API fora do ar), não a ausência da própria descrição.
    """
    proposal_cfg = config.get("proposal", {})
    modo = proposal_cfg.get("texto_modo", "fixo")

    texto_ia_falhou = False
    if modo == "ia" and full_description:
        variante = _sortear_variante_texto(proposal_cfg)
        gerado = ai_writer.generate_proposal_text(project, full_description, config, variante=variante)
        if gerado:
            return gerado, False, variante
        log.warning("Geração via IA indisponível/rejeitada para '%s' — usando template fixo.", project.get("title"))
        texto_ia_falhou = True

    template = proposal_cfg.get("texto", "Olá! Tenho interesse em {titulo}.")
    texto = template.format(
        titulo=project.get("title", ""),
        oferta=format_currency_br(oferta),
        prazo_dias=prazo_dias,
    )
    return texto, texto_ia_falhou, "template"


def _finalizar_texto(texto: str, project: dict, proposal_cfg: dict) -> tuple[str, bool]:
    """
    Anexa a nota_extra e roda a checagem final de segurança. Retorna (texto, substituido)
    — substituido=True quando o texto violou alguma regra e virou _SAFE_FALLBACK_TEXTO.
    """
    # Nota fixa opcional (ex: "estou começando no site, mas tenho portfólio") anexada
    # SEMPRE por fora do texto gerado — decisão explícita do usuário de deixar isso fixo em
    # vez de pedir pra IA reformular a cada vez, pra não arriscar ela variar/errar a
    # redação de uma alegação factual (quantos projetos, onde conferir etc.). Entra ANTES
    # de check_text_safety pra continuar coberta pela mesma rede de segurança.
    nota_extra = (proposal_cfg.get("nota_extra") or "").strip()
    if nota_extra:
        texto = f"{texto}\n\n{nota_extra}"

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
        return _SAFE_FALLBACK_TEXTO, True
    return texto, False


def build_proposal(
    project: dict,
    config: dict,
    lowest_bid: float | None = None,
    full_description: str | None = None,
    media_prazo: int | None = None,
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

    media_prazo: "Duração média estimada" das propostas concorrentes (mesmo bloco da média
    de valor). Quando presente, o prazo vira um pouco abaixo dela (_prazo_abaixo_da_media)
    em vez do prazo_dias fixo do config.yaml / da sugestão da IA.
    """
    proposal_cfg = config.get("proposal", {})
    estrategia = proposal_cfg.get("oferta_estrategia", "orcamento_cliente")
    budget = project.get("budget")
    orcamento_cliente = budget or proposal_cfg.get("oferta_fixa") or 0
    prazo_dias = proposal_cfg.get("prazo_dias", 7)

    if estrategia == "menor_proposta" and lowest_bid:
        # Média das propostas concorrentes arredondada pra baixo (1230 -> 1200) — decisão do
        # usuário (2026-09-25), no lugar do antigo undercut_percent% abaixo da média.
        oferta = _arredondar_para_baixo(lowest_bid)
        origem = "media_arredondada"
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

    if media_prazo:
        prazo_dias = _prazo_abaixo_da_media(media_prazo, proposal_cfg)

    texto, texto_ia_falhou, texto_variante = _build_texto(project, config, oferta, prazo_dias, full_description)

    texto, substituido = _finalizar_texto(texto, project, proposal_cfg)
    if substituido:
        texto_variante = "template"

    resultado = {
        "oferta": oferta,
        "prazo_dias": prazo_dias,
        "texto": texto,
        "origem_valor": origem,
        "texto_ia_falhou": texto_ia_falhou,
        # Metadados pra exibição no Telegram e registro em applied_jobs.json (não mudam o
        # cálculo acima) — base pra comparar os estilos de texto depois (bot/report.py).
        "media_concorrentes": lowest_bid,
        "media_prazo": media_prazo,
        "texto_variante": texto_variante,
    }
    if origem == "ia" and oferta_sugerida_ia != oferta:
        # Guarda o valor bruto sugerido pela IA (antes do desconto competitivo) pra
        # notifier.py mostrar os dois lado a lado no Telegram — ajuda a validar/lapidar
        # ia_desconto_milhar/ia_preco_minimo em config.yaml com dados reais ao longo do tempo.
        resultado["oferta_sugerida_ia"] = oferta_sugerida_ia

    log.info(
        "Proposta montada para '%s': oferta=R$%s, prazo=%sd, origem=%s, texto=%s%s",
        project.get("title"), oferta, prazo_dias, origem, texto_variante,
        f" (IA sugeriu R${oferta_sugerida_ia})" if "oferta_sugerida_ia" in resultado else "",
    )

    return resultado


# --- Estilo do texto sorteado por proposta ---
#
# 36 propostas no estilo "padrao" renderam 2 respostas e nenhum fechamento. Pra descobrir
# qual estilo converte melhor, cada proposta sorteia um estilo (pesos em
# proposal.texto_variantes do config.yaml), gravado em applied_jobs.json junto com o
# resultado marcado pelo usuário no Telegram (cola o link do projeto no chat do bot e
# clica "💬 Respondeu"/"🏆 Fechou" — ver Freelas99Source._on_link_action). `python bot/report.py` mostra as taxas por estilo.


def _arredondar_para_baixo(valor: float) -> float:
    """Piso na centena (1230 -> 1200, 770 -> 700); abaixo de R$100, piso na dezena (85 -> 80)."""
    passo = 100 if valor >= 100 else 10
    return float(math.floor(valor / passo) * passo)


def _sortear_variante_texto(proposal_cfg: dict) -> str:
    """Sorteia o estilo do texto pelos pesos de proposal.texto_variantes (default: todos iguais)."""
    pesos = proposal_cfg.get("texto_variantes") or {v: 1 for v in ai_writer.TEXT_VARIANTS}
    validos = {v: p for v, p in pesos.items() if v in ai_writer.TEXT_VARIANTS and p and p > 0}
    ignorados = set(pesos) - set(validos)
    if ignorados:
        log.warning("texto_variantes: ignorando estilo(s) desconhecido(s)/sem peso: %s", ", ".join(sorted(ignorados)))
    if not validos:
        return "padrao"
    return random.choices(list(validos), weights=list(validos.values()))[0]
