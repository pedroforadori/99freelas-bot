import re

from playwright.sync_api import Page

from bot import site_selectors as sel
from bot.logger_setup import get_logger
from bot.utils import minutes_since_epoch_ms, parse_relative_time_minutes

log = get_logger(__name__)


def _extract_project_id(url: str) -> str | None:
    match = re.search(sel.PROJECT_ID_URL_REGEX, url)
    return match.group(1) if match else None


def _extract_posted_minutes_ago(posted_el) -> float | None:
    if not posted_el:
        return None
    epoch_attr = posted_el.get_attribute("cp-datetime")
    if epoch_attr:
        return minutes_since_epoch_ms(epoch_attr)
    return parse_relative_time_minutes(posted_el.inner_text())


def _extract_category(info_el) -> str:
    """Categoria vem como o primeiro trecho de texto antes do '|' em PROJECT_CARD_INFO."""
    if not info_el:
        return ""
    first_segment = info_el.inner_text().split("|")[0]
    return first_segment.strip()


def fetch_open_projects(page: Page, max_items: int = 30) -> list[dict]:
    """
    Abre a listagem de projetos recentes e retorna uma lista de dicts:
    {id, title, url, category, budget, description, posted_minutes_ago}

    NOTA: "budget" sempre vem None — a listagem não mostra orçamento, só a página do
    projeto/proposta (ver PROJECT_CARD_BUDGET ausente em site_selectors.py).
    """
    # "networkidle" quase nunca é atingido nessa página (mesmo motivo já corrigido em
    # connections.py/messages.py pro /dashboard, commit c411488) — algum script de
    # analytics/chat mantém requisição em aberto, estourando os 30s default e derrubando
    # o ciclo inteiro (confirmado: 227 ocorrências consecutivas no log de produção).
    page.goto(sel.PROJECTS_LIST_URL, wait_until="load")
    cards = page.query_selector_all(sel.PROJECT_CARD)
    log.info("Encontrados %d cards de projeto na listagem", len(cards))

    projects = []
    for card in cards[:max_items]:
        try:
            title_el = card.query_selector(sel.PROJECT_CARD_TITLE)
            link_el = card.query_selector(sel.PROJECT_CARD_LINK)
            info_el = card.query_selector(sel.PROJECT_CARD_INFO)
            desc_el = card.query_selector(sel.PROJECT_CARD_DESCRIPTION)
            posted_el = card.query_selector(sel.PROJECT_CARD_POSTED_AT)

            if not title_el or not link_el:
                continue

            url = link_el.get_attribute("href") or ""
            if url.startswith("/"):
                url = "https://www.99freelas.com.br" + url

            project_id = card.get_attribute(sel.PROJECT_CARD_ID_ATTR) or _extract_project_id(url)
            if not project_id:
                log.warning("Não consegui extrair ID do projeto a partir da URL: %s", url)
                continue

            projects.append({
                "id": project_id,
                "title": title_el.inner_text().strip(),
                "url": url,
                "category": _extract_category(info_el),
                "budget": None,  # não disponível na listagem, ver nota em site_selectors.py
                "description": desc_el.inner_text().strip() if desc_el else "",
                "posted_minutes_ago": _extract_posted_minutes_ago(posted_el),
            })
        except Exception as e:
            log.warning("Erro ao processar um card de projeto, pulando: %s", e)
            continue

    return projects
