"""
Lê o saldo REAL de conexões da conta na página /dashboard — mais confiável que estimar a
cota localmente (MONTHLY_PROPOSAL_QUOTA / dias do mês corrente), porque a renovação do
plano não necessariamente cai no dia 1 (ex: renovação real confirmada em 17/10/2026 numa
conta real) e porque pode haver conexões não-expiráveis somadas ao saldo total.

Cacheado em data/connections.json e atualizado uma vez por ciclo (run_cycle em main.py,
antes do scraping da listagem) — não a cada proposta, pra não multiplicar navegações.
notifier.py lê esse cache (sem precisar de acesso à Page) e soma localmente as propostas
reais enviadas depois do último refresh, pra manter o contador correto entre atualizações.
"""
import json
import os
import re
import threading
from datetime import datetime

from bot import site_selectors as sel
from bot import storage
from bot.logger_setup import get_logger

log = get_logger(__name__)

_LOCK = threading.Lock()
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "connections.json")

# Ex real (HTML da conta do usuário): "240 conexões restantes de um total de 240 ...
# referentes ao seu plano (Premium)."
_PLANO_PATTERN = re.compile(r"(\d+)\s*conex[õo]es?\s*restantes de um total de\s*(\d+)", re.IGNORECASE)
_DISPONIVEIS_PATTERN = re.compile(r"Conex[õo]es dispon[íi]veis:?\s*(\d+)", re.IGNORECASE)
_NAO_EXPIRAVEIS_PATTERN = re.compile(r"(\d+)\s*conex[õo]es?\s*n[ãa]o\s*expir[áa]veis", re.IGNORECASE)
_RENOVACAO_PATTERN = re.compile(r"renovadas no dia\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)


def _load_cache() -> dict | None:
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_cache(data: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CACHE_PATH)  # escrita atômica, evita corromper o arquivo


def read_cached() -> dict | None:
    with _LOCK:
        return _load_cache()


def refresh(page) -> dict | None:
    """
    Navega pra /dashboard, extrai o saldo real de conexões do texto da página e salva no
    cache. Em caso de falha (layout mudou, elemento ausente, erro de rede), loga warning e
    devolve o último cache válido em vez de derrubar o ciclo — mesma filosofia de
    notifier.py: isso nunca pode quebrar o bot.
    """
    try:
        # "load" em vez de "networkidle": confirmado em produção (2026-09-18) que
        # /dashboard mantém alguma requisição de fundo aberta que às vezes nunca
        # "acalma" dentro do timeout padrão de 30s (mesmo padrão já visto na página de
        # envio de proposta, ver CLAUDE.md), causando timeout mesmo com o conteúdo já
        # pronto pra ler. O texto extraído abaixo não depende de rede ociosa, só do DOM.
        page.goto(sel.DASHBOARD_URL, wait_until="load")
        texto = page.inner_text("body")

        plano_match = _PLANO_PATTERN.search(texto)
        if not plano_match:
            log.warning("Não foi possível ler o saldo de conexões em %s (layout mudou?).", sel.DASHBOARD_URL)
            return read_cached()

        disponiveis_match = _DISPONIVEIS_PATTERN.search(texto)
        nao_expiraveis_match = _NAO_EXPIRAVEIS_PATTERN.search(texto)
        renovacao_match = _RENOVACAO_PATTERN.search(texto)

        data = {
            "plano_restantes": int(plano_match.group(1)),
            "plano_total": int(plano_match.group(2)),
            "disponiveis": int(disponiveis_match.group(1)) if disponiveis_match else None,
            "nao_expiraveis": int(nao_expiraveis_match.group(1)) if nao_expiraveis_match else None,
            "renovacao": renovacao_match.group(1) if renovacao_match else None,
            # Snapshot de quantas propostas reais já tinham sido enviadas hoje no momento
            # deste refresh — notifier.py soma em cima disso o que foi enviado DEPOIS
            # deste refresh, sem precisar visitar o dashboard a cada proposta.
            "baseline_sent_today": storage.proposals_sent_today(),
            "fetched_at": datetime.utcnow().isoformat(),
        }
        with _LOCK:
            _save_cache(data)
        return data
    except Exception as e:
        log.warning("Erro ao atualizar saldo de conexões: %s", e)
        return read_cached()
