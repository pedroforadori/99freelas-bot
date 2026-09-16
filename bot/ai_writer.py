"""
Gera o texto da proposta via IA (Anthropic Claude ou Google Gemini, configurável),
baseado na descrição completa do projeto — usado quando config.yaml tem
proposal.texto_modo == "ia".

Regras de segurança são reforçadas em dois níveis: instrução explícita no prompt E um
filtro automático que rejeita e cai pro template fixo (config.yaml) se o texto gerado
mesmo assim contiver e-mail, link, telefone ou valor/prazo — nunca confiamos só na
instrução do modelo pra isso.
"""
import os
import re

from bot.logger_setup import get_logger

log = get_logger(__name__)

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
_PHONE_PATTERN = re.compile(r"(\(?\d{2}\)?\s?)?9?\d{4}[-.\s]?\d{4}")
_MAX_CHARS = 2900  # abaixo do maxlength=3000 do campo #proposta, com margem

_SYSTEM_PROMPT = (
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
    "- Tom profissional e direto, focado em como você resolveria o problema descrito pelo cliente.\n"
    "- No máximo 1500 caracteres. Poucos parágrafos, sem saudação genérica excessiva nem "
    "fechamento floreado.\n"
    "- Responda APENAS com o texto da proposta, sem comentários extras."
)


def _safety_violation(text: str) -> str | None:
    """Retorna o motivo se o texto violar alguma regra de segurança, ou None se estiver OK."""
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


def _generate_with_anthropic(user_prompt: str, model: str) -> str | None:
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
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text").strip()
    except Exception as e:
        log.warning("Falha ao gerar proposta via Anthropic: %s", e)
        return None


def _generate_with_gemini(user_prompt: str, model: str) -> str | None:
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

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                max_output_tokens=1200,
                # Sem isso, modelos "thinking" (ex: gemini-3.6-flash) consomem o
                # max_output_tokens inteiro com raciocínio interno e cortam o texto
                # final na metade — confirmado testando (thoughts_token_count > 0).
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return (response.text or "").strip()
    except Exception as e:
        log.warning("Falha ao gerar proposta via Gemini: %s", e)
        return None


_PROVIDERS = {
    "anthropic": (_generate_with_anthropic, "claude-sonnet-5"),
    "gemini": (_generate_with_gemini, "gemini-3.6-flash"),
}


def generate_proposal_text(project: dict, full_description: str, config: dict) -> str | None:
    """
    Retorna o texto gerado, ou None se a API falhar ou o texto violar as regras de
    segurança — nesses casos o chamador (bot/proposal.py) deve cair pro template fixo.
    """
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

    text = generate_fn(user_prompt, model)
    if not text:
        log.warning("IA (%s) retornou texto vazio ou falhou.", provider_name)
        return None

    violation = _safety_violation(text)
    if violation:
        log.warning("Texto gerado por IA rejeitado (%s) — caindo pro template fixo.", violation)
        return None

    return text
