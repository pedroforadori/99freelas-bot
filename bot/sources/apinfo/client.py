"""
Cliente do APinfo (https://www.apinfo.com): busca de vagas, filtro local, candidatura
(CPF/senha no "Envie seu currículo" → o site mostra o e-mail e o assunto da vaga) e envio
do e-mail com o currículo. Portado do bot standalone C:\\www\\bot-apinfo (bot.py) — a
lógica de busca/parse/filtro/extração é a mesma; o que mudou é que o HTTP NUNCA dorme
esperando a pausa anti-bloqueio: quem espera é o loop do bot, tick a tick (ver
ApinfoSource.tick e Apinfo.ready), porque um sleep de minutos aqui travaria o polling
de aprovações do Telegram das outras fontes (processo único).

HTTP direto (requests + BeautifulSoup), sem Playwright. O site é ISO-8859-1 e limita
consultas por IP ("limite de consultas" → RateLimited).

Registro próprio em data/apinfo_jobs.json, no MESMO formato do applied.json do bot
standalone (chave = código da vaga; status "enviado" | "capturado" | "falhou") — a
migração é copiar o arquivo. Separado de applied_jobs.json de propósito: lá um "sent"
soma na cota diária de conexões do 99Freelas.
"""
import json
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from bot import email_sender
from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE = "https://www.apinfo.com/apinfo/inc/"
SEARCH_URL = urljoin(BASE, "list4.cfm")
APPLY_URL = urljoin(BASE, "enviecv.cfm")
ENCODING = "iso-8859-1"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_LOCK = threading.Lock()
DATA_PATH = os.path.join(BASE_DIR, "data", "apinfo_jobs.json")
DEBUG_DIR = os.path.join(BASE_DIR, "data", "apinfo_debug")

# Pisos anti-bloqueio: valores menores no config.yaml são ignorados.
PAUSA_MIN_SEGURA = 6           # s entre requisições ao APinfo
PAUSA_VAGA_MIN_SEGURA = 25     # s entre uma candidatura e outra ("tempo de leitura")
INTERVALO_MIN_SEGURO = 30      # min entre buscas
BACKOFF_MAX = 8                # após bloqueios seguidos, espera até 8x o intervalo

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
IGNORED_EMAIL_DOMAINS = ("apinfo.com", "apinfo2.com")


def now() -> float:
    """Relógio de todas as pausas (monotônico). Função própria pra os testes controlarem o tempo."""
    return time.monotonic()


class RateLimited(Exception):
    """O APinfo bloqueou temporariamente as consultas."""


class ApplyError(Exception):
    """Falha ao se candidatar a uma vaga específica."""


@dataclass
class Vaga:
    codigo: str
    cargo: str
    empresa: str
    local: str
    descricao: str
    link_envio: str
    publicada: str = ""  # dd/mm/aa, como o site mostra


# --- HTTP ------------------------------------------------------------------------------------


def faixa_segura(nome: str, lo: float, hi: float, piso: float) -> tuple[float, float]:
    """Garante lo >= piso e hi >= lo, avisando quando o config.yaml foi corrigido."""
    lo2 = max(lo, piso)
    hi2 = max(hi, lo2 * 1.5)
    if (lo2, hi2) != (lo, hi):
        log.info("apinfo_jobs: %s %s-%ss muito baixo; usando %g-%gs", nome, lo, hi, lo2, hi2)
    return lo2, hi2


class Apinfo:
    """
    Sessão HTTP com o APinfo. As pausas sorteadas entre requisições (`pausa`) e entre
    candidaturas (`pausa_vaga`) não são esperadas aqui dentro: quem chama checa ready()
    antes de dar o próximo passo. A única espera bloqueante é _wait, que só age entre as
    duas requisições seguidas de apply() (e em chamadas fora do fluxo de ticks).
    """

    def __init__(self, pausa_min: float, pausa_max: float, pausa_vaga: tuple[float, float] = (40, 90)):
        self.pausa_min, self.pausa_max = faixa_segura("pausa", pausa_min, pausa_max, PAUSA_MIN_SEGURA)
        self.pausa_vaga = faixa_segura("pausa_vaga", *pausa_vaga, PAUSA_VAGA_MIN_SEGURA)
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9",
        })
        self._next_req_at = 0.0
        self._next_vaga_at = 0.0

    def ready(self, kind: str = "req") -> bool:
        """Já passou a pausa desde a última requisição ("req") / candidatura ("vaga")?"""
        if now() < self._next_req_at:
            return False
        return kind != "vaga" or now() >= self._next_vaga_at

    def marcar_vaga(self) -> None:
        """Chamado depois de consultar uma vaga: a próxima só depois de pausa_vaga ("lendo" a anterior)."""
        self._next_vaga_at = now() + random.uniform(*self.pausa_vaga)

    def _wait(self) -> None:
        restante = self._next_req_at - now()
        if restante > 0:
            time.sleep(restante)

    def _request(self, method: str, url: str, data: list[tuple[str, str]] | None = None,
                 referer: str | None = None) -> str:
        self._wait()
        headers = {"Referer": referer} if referer else {}
        body = None
        if data is not None:
            # O site é ISO-8859-1: acentos nas palavras-chave precisam ir nesse encoding
            body = urlencode(data, encoding=ENCODING)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            r = self.s.request(method, url, data=body, headers=headers, timeout=30)
        finally:
            self._next_req_at = now() + random.uniform(self.pausa_min, self.pausa_max)
        r.raise_for_status()
        r.encoding = ENCODING
        html = r.text
        if "limite de consultas" in html.lower():
            raise RateLimited("O APinfo informou que o limite de consultas está esgotado. Tente mais tarde.")
        return html

    def get(self, url: str, referer: str | None = None) -> str:
        return self._request("GET", url, referer=referer)

    def post(self, url: str, data: list[tuple[str, str]], referer: str | None = None) -> str:
        return self._request("POST", url, data=data, referer=referer)


# --- Busca -----------------------------------------------------------------------------------


def build_search_form(cfg: dict, keyword: str) -> list[tuple[str, str]]:
    form = [
        ("tcv", "1"),
        ("pag", "1"),
        ("ddmmaa1", cfg.get("data_de") or ""),
        ("ddmmaa2", cfg.get("data_ate") or ""),
        ("keyw", keyword),
        ("onde", str(cfg.get("onde", 1))),
        ("andor", str(cfg.get("andor", 1))),
    ]
    form += [("estado[]", str(v)) for v in cfg.get("estados") or []]
    form += [("cod_cidade[]", str(v)) for v in cfg.get("cidades") or []]
    form += [("cargo2cod[]", str(v)) for v in cfg.get("cargos") or []]
    return form


def parse_vagas(html: str) -> list[Vaga]:
    soup = BeautifulSoup(html, "html.parser")
    vagas = []
    for box in soup.select("div.box-vagas"):
        link = box.select_one("a.btn3[href*='enviecv.cfm']")
        if not link:
            continue
        href = urljoin(BASE, link["href"])
        m = re.search(r"codvaga=(\d+)", href)
        if not m:
            continue

        info = box.select_one(".info-data")
        cargo = box.select_one(".cargo")
        texto = box.select_one(".texto p")
        # "Cidade - UF - dd/mm/aa" -> "Cidade - UF"
        local = info.get_text(" ", strip=True) if info else ""
        data = re.search(r"\s*-\s*(\d{2}/\d{2}/\d{2})\s*$", local)
        if data:
            local = local[:data.start()]

        full_text = box.get_text(" ", strip=True)
        emp = re.search(r"Empresa\s*\.*:\s*(.+?)\s*C[óo]digo", full_text)

        vagas.append(Vaga(
            codigo=m.group(1),
            # sem separador: o destaque do termo pesquisado quebra palavras em <span>s
            cargo=" ".join(cargo.get_text().split()) if cargo else "",
            empresa=emp.group(1).strip() if emp else "",
            local=local,
            descricao=texto.get_text("\n", strip=True) if texto else "",
            link_envio=href,
            publicada=data.group(1) if data else "",
        ))
    return vagas


def next_page_form(html: str, page: int) -> list[tuple[str, str]] | None:
    """Monta o POST de paginação a partir do formulário 'Pular para a página'."""
    soup = BeautifulSoup(html, "html.parser")
    m = re.search(r"P[áa]gina\s+\d+\s+de\s+(\d+)", soup.get_text(" "))
    if not m or page > int(m.group(1)):
        return None
    for form in soup.find_all("form"):
        if form.find("input", attrs={"name": "pag", "type": "text"}):
            data = [(i["name"], i.get("value", "")) for i in form.find_all("input")
                    if i.get("name") and i.get("type") in ("hidden", "text")]
            return [(k, str(page) if k == "pag" else v) for k, v in data]
    return None


def keyword_regex(kw: str) -> re.Pattern:
    """Termo como palavra inteira; espaço/hífen opcionais ("front-end" casa "Frontend")."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", kw.strip()) if p]
    return re.compile(r"(?<!\w)" + r"[\s\-]*".join(parts) + r"(?!\w)", re.I)


def search_steps(api: Apinfo, cfg: dict):
    """
    Gerador da busca: cada next() faz NO MÁXIMO uma requisição (uma palavra-chave ou uma
    página) e para — ApinfoSource.tick só chama o próximo quando api.ready(). No fim, o
    StopIteration carrega (em .value) a lista de vagas únicas encontradas.
    """
    found: dict[str, Vaga] = {}
    for kw in cfg.get("palavras_chave") or [""]:
        log.info("APinfo: buscando '%s'", kw)
        # O site casa pedaços de palavra ("ios" em "Negócios"); na busca por título, refiltra
        pattern = keyword_regex(kw) if kw and cfg.get("onde", 1) == 1 else None
        html = api.post(SEARCH_URL, build_search_form(cfg, kw), referer=SEARCH_URL)
        page = 1
        while True:
            vagas = parse_vagas(html)
            if pattern:
                vagas = [v for v in vagas if pattern.search(v.cargo)]
            log.info("APinfo: '%s' página %d: %d vaga(s)", kw, page, len(vagas))
            for v in vagas:
                found.setdefault(v.codigo, v)
            page += 1
            if page > cfg.get("max_paginas", 1):
                break
            form = next_page_form(html, page)
            if not form:
                break
            yield
            html = api.post(SEARCH_URL, form, referer=SEARCH_URL)
        yield
    return list(found.values())


def _has_any(text: str, terms: list[str]) -> bool:
    text = text.lower()
    return any(t.lower() in text for t in terms)


ENGLISH_MENTION_RE = re.compile(r"ingl[eê]s|english", re.I)
# Qualquer nível acima de básico / técnico para leitura
ENGLISH_LEVEL_RE = re.compile(
    r"intermedi|avan[çc]ad|fluen|conversa|proficien|nativ|bil[ií]ngu|"
    r"advanced|fluent|upper|business|full professional|\b[BC][12]\b",
    re.I,
)
ENGLISH_STOPWORDS = {
    "the", "and", "with", "you", "will", "for", "our", "are", "we", "of", "to",
    "in", "is", "experience", "skills", "team", "work", "knowledge", "strong",
}


def english_block_reason(desc: str) -> str | None:
    """Motivo para descartar a vaga pelo nível de inglês exigido, ou None."""
    for m in ENGLISH_MENTION_RE.finditer(desc):
        window = desc[max(0, m.start() - 40): m.end() + 60]
        if ENGLISH_LEVEL_RE.search(window):
            return f"inglês: '{' '.join(window.split())}'"

    # Descrição escrita em inglês = vaga que exige inglês
    words = re.findall(r"[a-zA-Z]+", desc.lower())
    if len(words) >= 20:
        ratio = sum(w in ENGLISH_STOPWORDS for w in words) / len(words)
        if ratio > 0.12:
            return "descrição em inglês"
    return None


def local_filter(vagas: list[Vaga], cfg: dict) -> list[Vaga]:
    out = []
    for v in vagas:
        motivo = None
        if _has_any(v.cargo, cfg.get("excluir_titulo") or []):
            motivo = "título excluído"
        elif cfg.get("exigir_titulo") and not _has_any(v.cargo, cfg["exigir_titulo"]):
            motivo = "título sem termo exigido"
        elif _has_any(v.descricao, cfg.get("excluir_descricao") or []):
            motivo = "descrição excluída"
        elif cfg.get("locais") and not _has_any(v.local, cfg["locais"]):
            motivo = "local"
        elif cfg.get("bloquear_ingles"):
            motivo = english_block_reason(v.descricao)

        if motivo:
            log.info("APinfo: descartada %s %s -> %s", v.codigo, v.cargo, motivo)
        else:
            out.append(v)
    return out


# --- Candidatura -----------------------------------------------------------------------------


def save_debug(name: str, html: str) -> str:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, f"{datetime.now():%Y%m%d-%H%M%S}-{name}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def extract_contact(html: str) -> tuple[str | None, str | None]:
    """
    Extrai e-mail e assunto da página exibida após enviar CPF/senha. Formato esperado:
        Email : fulano@empresa.com.br
        Assunto a ser colocado no email : apinfo - 85138 - Twig Symfony
    """
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href^='mailto:']"):
        a.replace_with(a["href"][7:].split("?")[0])
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)

    m = re.search(r"E-?mail\s*:\s*(" + EMAIL_RE.pattern + ")", text, re.I)
    if m:
        email = m.group(1)
    else:
        candidates = [e for e in EMAIL_RE.findall(text) if not e.lower().endswith(IGNORED_EMAIL_DOMAINS)]
        email = candidates[0] if candidates else None

    subject = None
    m = re.search(r"Assunto[^:\n]*:\s*(.+)", text, re.I)
    if m:
        subject = m.group(1).strip()

    return email, subject


def apply(api: Apinfo, vaga: Vaga, cpf: str, senha: str) -> tuple[str, str]:
    form_html = api.get(vaga.link_envio, referer=SEARCH_URL)
    soup = BeautifulSoup(form_html, "html.parser")
    form = soup.find("form", id="form-incluir-cv")
    if form:
        data = [(i["name"], i.get("value", "")) for i in form.find_all("input", type="hidden") if i.get("name")]
        data += [("cpf2", cpf), ("chave3", senha), ("subx", "Enviar")]
        result = api.post(APPLY_URL, data, referer=vaga.link_envio)
    else:
        # Já logado na sessão: o site mostra os dados de contato direto, sem pedir CPF/senha
        result = form_html
    email, subject = extract_contact(result)

    if not email:
        path = save_debug(f"{vaga.codigo}-resposta", result)
        raise ApplyError(f"e-mail não encontrado na resposta (HTML salvo em {path})")

    # Às vezes o site mostra o assunto sem o título ("apinfo - 86056 -")
    if not subject or subject.rstrip().endswith("-"):
        subject = f"apinfo - {vaga.codigo} - {vaga.cargo}"
    return email, subject


# --- E-mail ----------------------------------------------------------------------------------


def _attachment_path(anexo: str | None) -> str | None:
    if not anexo:
        return None
    return anexo if os.path.isabs(anexo) else os.path.join(BASE_DIR, anexo)


def send_email(to: str, subject: str, vaga: Vaga, email_cfg: dict, modo: str) -> tuple[bool, str]:
    """
    Manda o e-mail de candidatura (email_sender.send). modo "teste": vai pro próprio
    SMTP_USER com "[TESTE -> destino]" no assunto, em vez da empresa.
    """
    template = email_cfg.get("texto") or ""
    try:
        texto = template.strip().format(
            cargo=vaga.cargo, codigo=vaga.codigo, empresa=vaga.empresa, local=vaga.local,
            nome=os.environ.get("EMAIL_FROM_NAME", ""), publicada=vaga.publicada,
        )
    except (KeyError, IndexError, ValueError):
        # Chave desconhecida/chaves soltas no template — manda o texto como está.
        texto = template.strip()
    plain, html = email_sender.render_links(texto)
    # Só manda a versão HTML se o template usa links [texto](url) — senão fica só texto
    # puro, igual ao bot standalone.
    body_html = html if plain != texto else None

    user = os.environ.get("SMTP_USER", "")
    bcc = None
    if modo == "teste":
        to, subject = user, f"[TESTE -> {to}] {subject}"
    elif email_cfg.get("copia_para_mim"):
        bcc = user
    return email_sender.send(to, subject, plain, _attachment_path(email_cfg.get("anexo")), body_html, bcc=bcc)


# --- Registro --------------------------------------------------------------------------------


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


def get_record(codigo: str) -> dict | None:
    with _LOCK:
        return _load().get(codigo)


def save_record(codigo: str, rec: dict) -> None:
    with _LOCK:
        data = _load()
        data[codigo] = rec
        _save(data)


def already_done(codigo: str, modo: str, retentar_falhas: bool = False) -> bool:
    status = (get_record(codigo) or {}).get("status")
    if status == "capturado":  # candidatura feita, e-mail (real) ainda não enviado
        return modo != "real"
    if status == "falhou":
        return not retentar_falhas
    return status in ("enviado", "rejeitado")


def candidatar(api: Apinfo, v: Vaga, cpf: str, senha: str, email_cfg: dict, modo: str) -> dict:
    """
    Candidata-se à vaga e envia o e-mail. Grava e devolve o registro. Propaga
    RateLimited sem gravar nada (a vaga continua "nova" pra próxima busca).
    """
    prev = get_record(v.codigo) or {}
    rec = {k: val for k, val in prev.items() if k not in ("status", "erro")}
    rec.update(asdict(v), data=datetime.now().isoformat(timespec="seconds"))
    rec.pop("descricao")
    try:
        if prev.get("email"):
            # candidatura já feita antes: reaproveita e-mail/assunto sem consultar o site
            email, subject = prev["email"], prev["assunto"]
        else:
            email, subject = apply(api, v, cpf, senha)
    except (ApplyError, requests.RequestException) as e:
        rec.update(status="falhou", erro=str(e))
        log.warning("APinfo: falha na candidatura %s (%s): %s", v.codigo, v.cargo, e)
        save_record(v.codigo, rec)
        return rec

    rec.update(email=email, assunto=subject)
    if modo == "sem_email":
        rec["status"] = "capturado"
    else:
        ok, detail = send_email(email, subject, v, email_cfg, modo)
        if not ok:
            # candidatura feita, só o e-mail falhou: não refaz a candidatura depois
            rec.update(status="capturado", erro=detail)
        else:
            rec["status"] = "capturado" if modo == "teste" else "enviado"
    log.info("APinfo: %s %s → %s (%s)", v.codigo, v.cargo, email, rec["status"])
    save_record(v.codigo, rec)
    return rec
