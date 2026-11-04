"""
Material de experiência pra proposta, quando o cliente pede "experiência com projetos
semelhantes" (pedido do usuário, 2026-10-28): o texto do site de portfólio
(proposal.portfolio.url) + os trabalhos aprovados em data/portfolio/<Nome>/texto.txt (ver
portfolio.py). A IA resume o que for parecido com o projeto, sem citar empresa — os nomes
de nomes_a_omitir são barrados por ai_writer.check_text_safety.

Nunca levanta exceção: sem site e sem trabalhos locais, devolve None e a IA só aponta o
portfólio do perfil.
"""
import json
import os
import time

from bot import attachments
from bot.logger_setup import get_logger

log = get_logger(__name__)

_DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CACHE_PATH = os.path.join(_DATA, "portfolio_cache.json")
PORTFOLIO_DIR = os.path.join(_DATA, "portfolio")
CACHE_SEGUNDOS = 24 * 3600
MAX_CHARS = 4000


def _cfg(config: dict) -> dict:
    return (config.get("proposal") or {}).get("portfolio") or {}


def _ler_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _gravar_cache(dados: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        log.info("Não consegui gravar o cache do portfólio: %s", e)


def _texto_do_site(url: str) -> str | None:
    cache = _ler_cache()
    if cache.get("url") == url and time.time() - cache.get("lido_em", 0) < CACHE_SEGUNDOS:
        return cache.get("texto")
    texto, erro = attachments.read_link(url, None)
    if texto:
        _gravar_cache({"url": url, "lido_em": time.time(), "texto": texto})
        return texto
    log.info("Não consegui ler o site de portfólio (%s) — usando o cache antigo, se houver.", erro)
    return cache.get("texto") if cache.get("url") == url else None


def _trabalhos_locais() -> list[tuple[str, str]]:
    """(nome da pasta, texto.txt) dos trabalhos aprovados em data/portfolio/ (sem .pendentes)."""
    trabalhos = []
    try:
        pastas = sorted(os.listdir(PORTFOLIO_DIR))
    except OSError:
        return []
    for pasta in pastas:
        caminho = os.path.join(PORTFOLIO_DIR, pasta, "texto.txt")
        if pasta.startswith(".") or not os.path.isfile(caminho):
            continue
        try:
            with open(caminho, encoding="utf-8") as f:
                trabalhos.append((pasta, f.read().strip()))
        except OSError:
            continue
    return trabalhos


def material_portfolio(config: dict) -> str | None:
    partes = []
    url = _cfg(config).get("url")
    if url:
        site = _texto_do_site(url)
        if site:
            partes.append(f"Site de portfólio:\n{site}")
    trabalhos = [texto for _, texto in _trabalhos_locais() if texto]
    if trabalhos:
        partes.append("Trabalhos do portfólio:\n" + "\n\n".join(trabalhos))
    return attachments.clip("\n\n".join(partes), MAX_CHARS) if partes else None


def nomes_a_omitir(config: dict) -> list[str]:
    """
    Empresas/clientes que nunca aparecem no texto (proposal.portfolio.nomes_a_omitir). Os
    nomes das pastas de data/portfolio/ NÃO entram sozinhos: muitos são nome de produto
    ("Portal", "Mkt") e barrariam palavras comuns.
    """
    return [n.strip() for n in _cfg(config).get("nomes_a_omitir") or [] if n and len(n.strip()) >= 2]
