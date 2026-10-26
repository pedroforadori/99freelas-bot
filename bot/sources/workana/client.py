"""
Workana sem Playwright: leitura do HTML (listagem, página do projeto, formulário de
proposta) e o registro próprio em data/workana_jobs.json. Quem navega é source.py; aqui
só entra o HTML já carregado — por isso dá pra testar com HTML de exemplo.

Seletores validados contra o HTML real, logado, em 2026-10-23. A listagem é lida do
HTML renderizado: o JSON que a página busca por XHR (X-Requested-With) leva 403 do
Cloudflare até no navegador que passou no desafio.
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime

from bs4 import BeautifulSoup

from bot import utils
from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE_URL = "https://www.workana.com"
PROJECT_URL = BASE_URL + "/job/{slug}"
BID_URL = BASE_URL + "/messages/bid/{slug}"

# --- seletores (listagem) ---------------------------------------------------------------
CARD = ".project-item"
CARD_TITLE_LINK = "h2.project-title a"
CARD_DATE = ".project-main-details .date"
CARD_BIDS = ".project-main-details .bids"
CARD_DESCRIPTION = ".html-desc"
CARD_SKILLS = ".skills h3"
CARD_BUDGET = "p.budget .values"

# --- seletores (página do projeto) -------------------------------------------------------
PROJECT_DESCRIPTION = ".block-detail .expander"
PROJECT_BUDGET = "h4.budget"
PROJECT_BID_BUTTON = "#bid_button"

# --- seletores (formulário /messages/bid/<slug>) -----------------------------------------
BID_AMOUNT = "#Amount"                # name="bid[amount]", type=number, min = lance mínimo do site
BID_CONTENT = "#BidContent"           # name="bid[content]"
BID_DELIVERY = "#BidDeliveryTime"     # name="bid[deliveryTime]", texto livre ("10 dias")
BID_ACKNOWLEDGED = "input[name='acknowledged']"  # aceite dos termos, já vem marcado
BID_SUBMIT = "input[type='submit'][value='Enviar orçamento']"

_LOCK = threading.Lock()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
DATA_PATH = os.path.join(BASE_DIR, "data", "workana_jobs.json")
DEBUG_DIR = os.path.join(BASE_DIR, "data", "workana_debug")

_MAX_ID_BYTES = 50  # callback_data tem limite de 64 bytes ("editf:o:" + id)


def project_id(slug: str) -> str:
    """Id estável do projeto ("wk-<slug>"); slug longo vira trecho + hash (nunca contém ":")."""
    pid = f"wk-{slug}"
    if len(pid.encode()) <= _MAX_ID_BYTES:
        return pid
    digest = hashlib.sha1(slug.encode()).hexdigest()[:10]
    return f"wk-{slug[:_MAX_ID_BYTES - 14]}-{digest}"


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def parse_budget(text: str) -> dict:
    """
    "R$ 2.500 - 5.000" → {"min": 2500, "max": 5000, "moeda": "R$", "por_hora": False}.
    Formatos vistos: "R$ 250 - 500", "Menos de R$ 250", "Menos de R$ 80 / hora". Logado
    em conta brasileira vem em R$; deslogado, em USD. Sem número → min/max None.
    """
    text = text or ""
    moeda = "R$" if "R$" in text else ("USD" if "USD" in text else "")
    valores = [v for v in (utils.parse_currency(n) for n in re.findall(r"\d[\d.]*(?:,\d+)?", text)) if v]
    minimo = min(valores) if valores else None
    maximo = max(valores) if valores else None
    if re.search(r"menos de", text, re.IGNORECASE):  # "Menos de R$ 250": só teto
        minimo = None
    elif re.search(r"mais de", text, re.IGNORECASE):  # "Mais de R$ 50.000": só piso
        maximo = None
    return {
        "min": minimo,
        "max": maximo,
        "moeda": moeda,
        "por_hora": "hora" in text.lower(),
    }


def parse_age_minutes(text: str) -> float | None:
    """'Publicado: há 20 horas', 'Há quase uma hora', 'há 2 dias' → minutos (None se não entender)."""
    t = (text or "").lower().replace("quase ", "").replace("cerca de ", "")
    t = re.sub(r"\b(uma|um)\b", "1", t)
    return utils.parse_relative_time_minutes(t)


def _slug_from_href(href: str) -> str | None:
    match = re.search(r"/job/([^/?#]+)", href or "")
    return match.group(1) if match else None


def parse_listing(html: str) -> list[dict]:
    """Cards da listagem → projetos no mesmo formato que filter.is_match entende."""
    soup = BeautifulSoup(html, "html.parser")
    projects = []
    for card in soup.select(CARD):
        link = card.select_one(CARD_TITLE_LINK)
        slug = _slug_from_href(link.get("href") if link else "")
        if not slug:
            continue
        titulo_completo = link.select_one("span[title]")
        title = titulo_completo["title"] if titulo_completo else _text(link)
        description = _text(card.select_one(CARD_DESCRIPTION)).replace("Ver mais detalhes", "").strip()
        budget_text = _text(card.select_one(CARD_BUDGET))
        budget = parse_budget(budget_text)
        bids = re.search(r"\d+", _text(card.select_one(CARD_BIDS)))
        projects.append({
            "id": project_id(slug),
            "slug": slug,
            "title": title,
            "url": PROJECT_URL.format(slug=slug),
            "category": "",
            "description": description,
            "budget": budget["min"] if budget["min"] is not None else budget["max"],  # filter.is_match
            "budget_text": budget_text,
            "por_hora": budget["por_hora"],
            "posted_minutes_ago": parse_age_minutes(_text(card.select_one(CARD_DATE))),
            "bids": int(bids.group()) if bids else None,
            "skills": [_text(s) for s in card.select(CARD_SKILLS)],
        })
    return projects


def parse_project_page(html: str) -> dict:
    """Página /job/<slug> logada: descrição completa, orçamento (R$) e se dá pra propor."""
    soup = BeautifulSoup(html, "html.parser")
    descricao = soup.select_one(PROJECT_DESCRIPTION)
    if descricao:
        for br in descricao.find_all("br"):
            br.replace_with("\n")
        full_description = re.sub(r"[ \t]+", " ", descricao.get_text()).strip()
    else:
        full_description = ""
    return {
        "full_description": full_description,
        "budget_text": _text(soup.select_one(PROJECT_BUDGET)),
        "pode_propor": soup.select_one(PROJECT_BID_BUTTON) is not None,
    }


def parse_bid_form(html: str) -> dict:
    """Formulário /messages/bid/<slug>: se existe e o lance mínimo do site (atributo min)."""
    soup = BeautifulSoup(html, "html.parser")
    amount = soup.select_one(BID_AMOUNT)
    minimo = None
    if amount and amount.get("min"):
        try:
            minimo = float(amount["min"])
        except ValueError:
            minimo = None
    return {"tem_formulario": amount is not None and soup.select_one(BID_CONTENT) is not None, "lance_minimo": minimo}


def format_amount(valor: float) -> str:
    """#Amount é type=number: ponto decimal, sem separador de milhar."""
    return str(int(valor)) if float(valor).is_integer() else f"{valor:.2f}"


# --- registro (data/workana_jobs.json) — NUNCA applied_jobs.json (cota/conexões do 99Freelas) ---


def _load() -> dict:
    if not os.path.exists(DATA_PATH):
        return {}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    tmp_path = DATA_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, DATA_PATH)


def get_record(pid: str) -> dict | None:
    with _LOCK:
        return _load().get(pid)


def register(pid: str, title: str, status: str, detail: str = "", extra: dict | None = None) -> None:
    """
    status: "skipped" (filtro) | "pending_approval" | "failed" | "sent" |
    "rejected_by_user".
    """
    with _LOCK:
        data = _load()
        data[pid] = {
            "title": title,
            "status": status,
            "detail": detail,
            "timestamp": datetime.utcnow().isoformat(),
            **(extra or {}),
        }
        _save(data)


def save_debug(nome: str, html: str) -> str | None:
    """Guarda o HTML de uma página inesperada em data/workana_debug/ pra ajustar seletores."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{datetime.now():%Y%m%d-%H%M%S}-{nome}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path
    except OSError as e:
        log.info("Não consegui salvar o HTML de debug do Workana: %s", e)
        return None
