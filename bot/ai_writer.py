"""
Gera o texto da proposta via IA (Anthropic Claude ou Google Gemini, configurável),
baseado na descrição completa do projeto — usado quando config.yaml tem
proposal.texto_modo == "ia".

Regras de segurança são reforçadas em dois níveis: instrução explícita no prompt E um
filtro automático que rejeita e cai pro template fixo (config.yaml) se o texto gerado
mesmo assim contiver e-mail, link, telefone ou valor/prazo — nunca confiamos só na
instrução do modelo pra isso.
"""
import json
import os
import re
import time

from bot.logger_setup import get_logger

log = get_logger(__name__)

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(\(?\d{2}\)?\s?)?9?\d{4}[-.\s]?\d{4}")
_MAX_CHARS = 2900  # abaixo do maxlength=3000 do campo #proposta, com margem

# Regras de segurança/conteúdo comuns a TODOS os estilos de texto (ver _TEXT_VARIANTS).
_SYSTEM_PROMPT_BASE = (
    "Você escreve propostas de freelancer para projetos no 99Freelas, em português do "
    "Brasil. Regras OBRIGATÓRIAS, sem exceção:\n"
    "- NUNCA inclua e-mail, telefone, WhatsApp, redes sociais, links ou qualquer forma de contato.\n"
    "- NUNCA mencione valor, preço, orçamento ou R$ — isso é preenchido em outro campo do formulário.\n"
    "- NUNCA mencione prazo ou número de dias — isso também é preenchido em outro campo.\n"
    "- Baseie-se apenas nas habilidades informadas; não invente experiências, ferramentas "
    "ou projetos anteriores não mencionados.\n"
    "- NÃO mencione tecnologias, frameworks, linguagens de programação, siglas técnicas ou "
    "termos de arquitetura (ex: React, Node.js, SOLID, DDD, API, JWT, GraphQL) — o cliente "
    "geralmente é leigo em tecnologia. Fale do problema dele e de como você resolve, em "
    "linguagem simples e acessível, não da stack técnica.\n"
)

# Estilo do texto — sorteado por proposta (ver proposal._sortear_variante_texto) pra
# descobrir qual estilo converte melhor. "padrao" é o estilo original (grupo de controle):
# _SYSTEM_PROMPT continua idêntico ao de antes do teste.
_TEXT_VARIANTS = {
    "padrao": (
        "- Tom profissional e direto, focado em como você resolveria o problema descrito pelo cliente.\n"
        "- No máximo 1500 caracteres. Poucos parágrafos, sem saudação genérica excessiva nem "
        "fechamento floreado.\n"
    ),
    "pergunta": (
        "- Texto CURTO: no máximo 600 caracteres, 2 ou 3 parágrafos curtos.\n"
        "- A primeira frase já fala do problema específico do cliente, usando os termos da "
        "descrição dele — nada de 'Olá, tenho interesse no seu projeto' nem apresentação genérica.\n"
        "- Em 1 ou 2 frases, diga de forma concreta como você resolveria.\n"
        "- Termine com UMA pergunta específica sobre o projeto (um detalhe que realmente "
        "faltou na descrição), que convide o cliente a responder.\n"
    ),
    "plano": (
        "- Primeira frase: resuma em uma linha o que o cliente precisa, com os termos da "
        "descrição dele — sem saudação genérica.\n"
        "- Em seguida, liste de 3 a 4 etapas numeradas do que você vai fazer, cada uma numa "
        "linha curta e em linguagem simples.\n"
        "- Feche com uma frase dizendo o que o cliente terá em mãos ao final.\n"
        "- No máximo 1000 caracteres.\n"
    ),
    "minimo": (
        "- Texto MUITO curto: 2 ou 3 frases, no máximo 300 caracteres.\n"
        "- Primeira frase: o problema específico do cliente, com os termos da descrição dele. "
        "Segunda: como você resolve, de forma concreta. Se couber, uma terceira bem curta "
        "mostrando disponibilidade pra começar.\n"
        "- Sem saudação e sem despedida.\n"
    ),
    "resultado": (
        "- Foque no RESULTADO pro cliente, não nas tarefas: descreva como vai ficar a situação "
        "dele depois do trabalho pronto (o que ele passa a conseguir fazer, o que deixa de ser "
        "problema).\n"
        "- Use exemplos concretos tirados da descrição dele.\n"
        "- Sem saudação genérica. No máximo 900 caracteres.\n"
    ),
    "diagnostico": (
        "- Comece mostrando em uma frase que entendeu o pedido, com os termos do cliente.\n"
        "- Aponte UM cuidado ou risco importante que esse tipo de projeto costuma ter e que o "
        "cliente talvez não tenha mencionado, e diga como você vai tratar isso — em linguagem "
        "simples.\n"
        "- Não critique o cliente nem a descrição dele.\n"
        "- No máximo 900 caracteres.\n"
    ),
    "opcoes": (
        "- Resuma em uma frase o que o cliente precisa.\n"
        "- Ofereça dois caminhos: um mais enxuto e um mais completo, cada um em uma ou duas "
        "frases dizendo o que inclui — SEM falar de valor ou prazo de nenhum dos dois.\n"
        "- Termine perguntando qual dos dois caminhos faz mais sentido pra ele.\n"
        "- No máximo 1000 caracteres.\n"
    ),
    "conversa": (
        "- Tom informal e próximo, em primeira pessoa, como uma mensagem de conversa — sem "
        "cara de proposta formal e sem listas.\n"
        "- Mostre que entendeu o que o cliente quer, com os termos dele, e diga com "
        "naturalidade como você faria.\n"
        "- Continue respeitoso: sem gírias e sem emojis.\n"
        "- No máximo 800 caracteres.\n"
    ),
}
TEXT_VARIANTS = tuple(_TEXT_VARIANTS)

_RESPONDA_APENAS_TEXTO = "- Responda APENAS com o texto da proposta, sem comentários extras."


def _text_system_prompt(variante: str) -> str:
    return _SYSTEM_PROMPT_BASE + _TEXT_VARIANTS[variante] + _RESPONDA_APENAS_TEXTO


_SYSTEM_PROMPT = _text_system_prompt("padrao")

_PRICE_SYSTEM_PROMPT = (
    "Você sugere valor e prazo de proposta para projetos de freelancer no 99Freelas, em "
    "português do Brasil. Contexto: não há dado de mercado disponível pra esse projeto "
    "(nem orçamento do cliente, nem propostas concorrentes já enviadas), então a sugestão "
    "precisa vir só da sua leitura da descrição do projeto.\n"
    "Regras OBRIGATÓRIAS:\n"
    "- O objetivo é MAXIMIZAR a chance de aceitação — sugira um valor BAIXO e competitivo "
    "pro escopo descrito, não um 'valor justo de mercado'. Prazo agressivo mas realista "
    "(rapidez também ajuda a converter).\n"
    "- Calibre pela complexidade real do escopo descrito — um projeto simples merece um "
    "valor claramente menor que um projeto grande, mesmo mirando baixo em ambos os casos.\n"
    "- Responda APENAS com um objeto JSON válido, sem markdown, sem explicação, no formato "
    'exato: {"oferta": <número>, "prazo_dias": <número inteiro>}. Nenhum texto antes ou depois.'
)


def check_text_safety(text: str) -> str | None:
    """
    Retorna o motivo se o texto violar alguma regra de segurança, ou None se estiver OK.
    Pública (sem `_`) porque proposal.py também usa isso como checagem final no texto que
    vai pro formulário, seja ele gerado por IA ou vindo do template fixo em config.yaml —
    nunca confiar só na instrução do prompt, nem só em "não esquecer" de configurar certo
    o template fixo.
    """
    if _EMAIL_PATTERN.search(text):
        return "contém um e-mail"
    if _URL_PATTERN.search(text):
        return "contém um link/URL"
    if _PHONE_PATTERN.search(text):
        return "contém um possível número de telefone"
    if "r$" in text.lower():
        return "menciona valor (R$)"
    if len(text) > _MAX_CHARS:
        return f"texto muito longo ({len(text)} caracteres)"
    return None


def _build_user_prompt(project: dict, full_description: str, skills: list) -> str:
    return (
        f"Projeto: {project.get('title', '')}\n\n"
        f"Descrição completa do projeto:\n{full_description}\n\n"
        f"Minhas habilidades/áreas de atuação: {', '.join(skills) if skills else '(não informado)'}\n\n"
        "Escreva a proposta seguindo todas as regras do system prompt."
    )


def _generate_with_anthropic(user_prompt: str, model: str, system_prompt: str = _SYSTEM_PROMPT) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY não configurada — não é possível gerar proposta via Anthropic.")
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        log.warning("Biblioteca 'anthropic' não instalada (ver requirements.txt).")
        return None

    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()
    except Exception as e:
        log.warning("Falha ao gerar proposta via Anthropic: %s", e)
        return None


def _generate_with_gemini(user_prompt: str, model: str, system_prompt: str = _SYSTEM_PROMPT) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY não configurada — não é possível gerar proposta via Gemini.")
        return None
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        log.warning("Biblioteca 'google-genai' não instalada (ver requirements.txt).")
        return None

    client = genai.Client(api_key=api_key)
    # 503 (modelo sobrecarregado) e 429 são transitórios e frequentes no Gemini — confirmado
    # em produção (2026-09-21) derrubando quase toda proposta; tenta de novo com backoff.
    esperas = [0, 3, 8, 15]
    for tentativa, espera in enumerate(esperas, start=1):
        if espera:
            time.sleep(espera)
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=1200,
                    # Sem isso, modelos "thinking" (ex: gemini-3.6-flash) consomem o
                    # max_output_tokens inteiro com raciocínio interno e cortam o texto
                    # final na metade — confirmado testando (thoughts_token_count > 0).
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            return (response.text or "").strip()
        except Exception as e:
            transitorio = any(c in str(e) for c in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
            log.warning("Falha ao gerar proposta via Gemini (tentativa %d/%d): %s", tentativa, len(esperas), e)
            if not transitorio:
                return None
    return None


_PROVIDERS = {
    "anthropic": (_generate_with_anthropic, "claude-sonnet-5"),
    "gemini": (_generate_with_gemini, "gemini-3.6-flash"),
}


def generate_proposal_text(
    project: dict, full_description: str, config: dict, variante: str = "padrao"
) -> str | None:
    """
    Retorna o texto gerado, ou None se a API falhar ou o texto violar as regras de
    segurança — nesses casos o chamador (bot/proposal.py) deve cair pro template fixo.
    `variante` escolhe o estilo do texto (ver _TEXT_VARIANTS); desconhecida → "padrao".
    """
    if variante not in _TEXT_VARIANTS:
        log.warning("Estilo de texto '%s' desconhecido — usando 'padrao'.", variante)
        variante = "padrao"
    proposal_cfg = config.get("proposal", {})
    provider_name = proposal_cfg.get("ia_provider", "anthropic")
    provider = _PROVIDERS.get(provider_name)
    if not provider:
        log.warning("ia_provider '%s' desconhecido (use 'anthropic' ou 'gemini').", provider_name)
        return None

    generate_fn, default_model = provider
    model = proposal_cfg.get("ia_model", default_model)
    skills = config.get("keywords_include", [])
    user_prompt = _build_user_prompt(project, full_description, skills)

    text = generate_fn(user_prompt, model, system_prompt=_text_system_prompt(variante))
    if not text:
        log.warning("IA (%s) retornou texto vazio ou falhou.", provider_name)
        return None

    violation = check_text_safety(text)
    if violation:
        log.warning("Texto gerado por IA rejeitado (%s) — caindo pro template fixo.", violation)
        return None

    return text


_PRICE_MIN_SANITY = 20.0
_PRICE_MAX_SANITY = 50000.0
_PRAZO_MIN_SANITY = 1
_PRAZO_MAX_SANITY = 90


def _build_price_user_prompt(project: dict, full_description: str) -> str:
    return (
        f"Projeto: {project.get('title', '')}\n\n"
        f"Descrição completa do projeto:\n{full_description}\n\n"
        "Sugira oferta (R$) e prazo_dias seguindo todas as regras do system prompt."
    )


def suggest_price_and_deadline(project: dict, full_description: str, config: dict) -> tuple[float, int] | None:
    """
    Sugere (oferta, prazo_dias) via IA quando não há orçamento do cliente nem "menor
    proposta"/média disponível pra basear o preço — ver proposal.build_proposal.

    Retorna None se a API falhar, a resposta não for um JSON válido no formato esperado,
    ou os valores caírem fora de uma faixa de sanidade ampla (não é um "piso de mercado",
    só uma proteção contra erro grosseiro de parsing/unidade — por decisão do usuário, não
    há piso mínimo configurado; ele quer que a IA julgue o valor coerente por projeto).
    Tudo ou nada: o chamador NÃO deve tentar usar só uma das duas partes se a outra falhar.
    """
    proposal_cfg = config.get("proposal", {})
    provider_name = proposal_cfg.get("ia_provider", "anthropic")
    provider = _PROVIDERS.get(provider_name)
    if not provider:
        log.warning("ia_provider '%s' desconhecido (use 'anthropic' ou 'gemini').", provider_name)
        return None

    generate_fn, default_model = provider
    model = proposal_cfg.get("ia_model", default_model)
    user_prompt = _build_price_user_prompt(project, full_description)

    raw = generate_fn(user_prompt, model, system_prompt=_PRICE_SYSTEM_PROMPT)
    if not raw:
        log.warning("IA (%s) não retornou sugestão de preço/prazo.", provider_name)
        return None

    # Alguns modelos ocasionalmente envolvem o JSON em ```json ... ``` mesmo pedindo pra não —
    # tira as cercas de código se existirem, sem tentar reparar nada além disso.
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    try:
        data = json.loads(cleaned)
        oferta = float(data["oferta"])
        prazo_dias = int(data["prazo_dias"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.warning("Resposta de preço/prazo da IA não é um JSON válido (%s): %r", e, raw[:200])
        return None

    if not (_PRICE_MIN_SANITY <= oferta <= _PRICE_MAX_SANITY):
        log.warning("Oferta sugerida pela IA fora da faixa de sanidade: R$%s", oferta)
        return None
    if not (_PRAZO_MIN_SANITY <= prazo_dias <= _PRAZO_MAX_SANITY):
        log.warning("Prazo sugerido pela IA fora da faixa de sanidade: %s dias", prazo_dias)
        return None

    return round(oferta, 2), prazo_dias


PORTFOLIO_TITULO_MAX = 50  # maxlength de #titulo no formulário de portfólio do 99Freelas
PORTFOLIO_DESCRICAO_MAX = 400  # maxlength de #descricao

_PORTFOLIO_SYSTEM_PROMPT = (
    "Você escreve o título e a descrição de um trabalho no portfólio de um freelancer de "
    "desenvolvimento, no 99Freelas, em português do Brasil. Recebe dados públicos do "
    "site/app (título, descrição, trecho do texto). Regras OBRIGATÓRIAS:\n"
    f"- Título com NO MÁXIMO {PORTFOLIO_TITULO_MAX} caracteres, descrição com NO MÁXIMO "
    f"{PORTFOLIO_DESCRICAO_MAX} caracteres.\n"
    "- Descreva o que é o projeto e o que ele entrega pro cliente final, em linguagem simples "
    "— quem lê é um possível cliente leigo. NÃO cite tecnologias, frameworks, linguagens ou siglas técnicas.\n"
    "- NUNCA inclua e-mail, telefone, WhatsApp, redes sociais, links/URLs ou preços.\n"
    "- Não invente funcionalidades, números, prêmios ou clientes que não estejam nos dados recebidos.\n"
    "- Tom profissional e direto, sem exageros de marketing.\n"
    "- Responda APENAS com um objeto JSON válido, sem markdown, no formato exato: "
    '{"titulo": "...", "descricao": "..."}.'
)


def _build_portfolio_user_prompt(context: dict) -> str:
    tipo = "app mobile" if context.get("kind") == "app" else "site"
    return (
        f"Tipo: {tipo}\n"
        f"Título encontrado: {context.get('title', '')}\n"
        f"Descrição encontrada: {context.get('meta_description', '')}\n"
        f"Trecho do conteúdo: {context.get('text', '')}\n\n"
        "Escreva título e descrição seguindo todas as regras do system prompt."
    )


def check_portfolio_text(titulo: str, descricao: str) -> str | None:
    """Motivo se título/descrição violarem limite ou regra de segurança, senão None."""
    if not titulo.strip():
        return "título vazio"
    if len(titulo) > PORTFOLIO_TITULO_MAX:
        return f"título com {len(titulo)} caracteres (máx {PORTFOLIO_TITULO_MAX})"
    if len(descricao) > PORTFOLIO_DESCRICAO_MAX:
        return f"descrição com {len(descricao)} caracteres (máx {PORTFOLIO_DESCRICAO_MAX})"
    return check_text_safety(f"{titulo}\n{descricao}")


def generate_portfolio_text(context: dict, config: dict) -> tuple[str, str] | None:
    """
    Gera (titulo, descricao) do item de portfólio a partir do contexto público capturado
    (ver portfolio_capture). Retorna None se a IA falhar, a resposta não for o JSON esperado
    ou violar limite/regra (check_portfolio_text) — o chamador cai pro texto do próprio
    site/app, marcado pra revisão. Mesma filosofia de generate_proposal_text: nunca confiar
    só na instrução do prompt.
    """
    proposal_cfg = config.get("proposal", {})
    provider_name = proposal_cfg.get("ia_provider", "anthropic")
    provider = _PROVIDERS.get(provider_name)
    if not provider:
        log.warning("ia_provider '%s' desconhecido (use 'anthropic' ou 'gemini').", provider_name)
        return None

    generate_fn, default_model = provider
    model = proposal_cfg.get("ia_model", default_model)
    raw = generate_fn(_build_portfolio_user_prompt(context), model, system_prompt=_PORTFOLIO_SYSTEM_PROMPT)
    if not raw:
        log.warning("IA (%s) não retornou texto de portfólio.", provider_name)
        return None

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
        titulo = str(data["titulo"]).strip()
        descricao = str(data["descricao"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        log.warning("Resposta de portfólio da IA não é um JSON válido (%s): %r", e, raw[:200])
        return None

    violation = check_portfolio_text(titulo, descricao)
    if violation:
        log.warning("Texto de portfólio da IA rejeitado (%s).", violation)
        return None
    return titulo, descricao
