"""
Vagas publicadas como issues no GitHub (ex: frontendbr/vagas) — candidatura por e-mail
com aprovação no Telegram, mesmo portão das propostas do 99Freelas.

Fluxo: main.py chama `check_new_issues(config)` uma vez por ciclo de varredura → busca as
issues abertas com as labels configuradas (API pública do GitHub, sem Playwright) → pra
cada issue nova, extrai o e-mail de candidatura do corpo e monta o e-mail (assunto = título
da issue, texto fixo do config.yaml, anexo) → notifier.send_github_approval_request +
approvals.add_pending (com project["source"] = "github"). O envio de verdade só acontece
em main.process_pending_approvals, depois do clique em "✅ Aprovar" (email_sender.send).

Registro próprio em data/github_jobs.json (mesmo padrão atômico de storage.py), separado
de applied_jobs.json de propósito: lá o status "sent" soma na contagem diária de conexões
do 99Freelas e entra no relatório de estilos (bot/report.py) — e-mail não gasta conexão.
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

from bot import email_sender
from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
_LOCK = threading.Lock()
DATA_PATH = os.path.join(BASE_DIR, "data", "github_jobs.json")

_API_URL = "https://api.github.com/repos/{repo}/issues"
MAX_QUEUED_PER_CYCLE = 10  # mesma trava de main.MAX_QUEUED_PER_CYCLE, contra rajada

_EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# E-mails que aparecem em corpo de issue mas nunca são de candidatura.
_EMAIL_IGNORADOS = ("noreply", "no-reply", "example.com", "exemplo.com", "users.noreply.github.com")
# Seção do template do frontendbr/vagas (e da maioria dos repos de vagas no mesmo molde).
_SECAO_CANDIDATURA = re.compile(r"^#+\s*.*candidat.*$", re.I | re.M)


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


def already_seen(job_id: str) -> bool:
    with _LOCK:
        return job_id in _load()


def get_record(job_id: str) -> dict | None:
    with _LOCK:
        return _load().get(job_id)


def register(job_id: str, title: str, status: str, detail: str = "", extra: dict | None = None) -> None:
    """
    status: "pending_approval" | "email_sent" | "failed" | "rejected_by_user" |
    "no_email" (issue sem e-mail no corpo — candidatura só pelo link, avisada uma vez).
    """
    with _LOCK:
        data = _load()
        data[job_id] = {
            "title": title,
            "status": status,
            "detail": detail,
            "timestamp": datetime.utcnow().isoformat(),
            **(extra or {}),
        }
        _save(data)


def last_email_sent_to(address: str) -> dict | None:
    """Último e-mail já enviado pra esse endereço (aviso na aprovação — ex: recrutador com várias vagas)."""
    address = address.lower()
    with _LOCK:
        enviados = [
            rec for rec in _load().values()
            if rec.get("status") == "email_sent" and (rec.get("email_to") or "").lower() == address
        ]
    return max(enviados, key=lambda r: r["timestamp"]) if enviados else None


def job_id_for(repo: str, number: int) -> str:
    """
    Id usado em approvals.py e no callback_data do Telegram (limite de 64 bytes, com
    prefixos de até 8 chars tipo "editf:e:"). Legível quando cabe; senão, hash do repo.
    Nunca contém ":" (é o separador do callback_data).
    """
    legivel = f"gh-{repo}#{number}"
    if len(legivel.encode()) <= 50:
        return legivel
    return f"gh-{hashlib.sha1(repo.encode()).hexdigest()[:10]}#{number}"


def extract_email(body: str) -> tuple[str | None, bool]:
    """
    Retorna (e-mail, veio_da_secao_candidatura). Prefere um e-mail dentro da seção
    "Como se candidatar"; senão, o primeiro e-mail em qualquer lugar do corpo — que pode
    ser só um contato de feedback (ex: vagas que pedem candidatura pelo site), por isso a
    mensagem de aprovação avisa quando é esse o caso.
    """
    body = body or ""

    def _primeiro(texto: str) -> str | None:
        for email in _EMAIL_REGEX.findall(texto):
            email = email.rstrip(".")
            if not any(ign in email.lower() for ign in _EMAIL_IGNORADOS):
                return email
        return None

    secao = _SECAO_CANDIDATURA.search(body)
    if secao:
        resto = body[secao.end():]
        proxima = re.search(r"^#+\s", resto, re.M)
        encontrado = _primeiro(resto[: proxima.start()] if proxima else resto)
        if encontrado:
            return encontrado, True
    return _primeiro(body), False


def fetch_issues(repo: str, labels: list[str]) -> list[dict]:
    """
    Issues abertas com TODAS as labels (a API do GitHub faz AND com a lista separada por
    vírgula), mais novas primeiro. Sem GITHUB_TOKEN o limite é 60 req/h por IP — sobra pra
    alguns repos no ritmo de CHECK_INTERVAL_*; com token, 5000/h.
    """
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"state": "open", "sort": "created", "direction": "desc", "per_page": 50}
    if labels:
        params["labels"] = ",".join(labels)
    resp = requests.get(_API_URL.format(repo=repo), headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    # A API de issues também devolve pull requests — não são vagas.
    return [i for i in resp.json() if "pull_request" not in i]


def published_label(created_at: str | None) -> str | None:
    """"25/09/2026 (há 3 dias)" a partir do created_at da issue (ISO UTC), no fuso local."""
    if not created_at:
        return None
    criada = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    dias = (datetime.now(timezone.utc) - criada).days
    quando = "hoje" if dias == 0 else "ontem" if dias == 1 else f"há {dias} dias"
    return f"{criada.astimezone().strftime('%d/%m/%Y')} ({quando})"


def _attachment_path(config_email: dict) -> str | None:
    anexo = config_email.get("anexo")
    if not anexo:
        return None
    return anexo if os.path.isabs(anexo) else os.path.join(BASE_DIR, anexo)


def build_email(job: dict, config: dict) -> dict:
    """Monta o "proposal" de uma vaga do GitHub: destinatário, assunto, texto fixo e anexo."""
    cfg_email = config.get("github_jobs", {}).get("email", {})
    template = cfg_email.get("texto") or ""
    try:
        texto = template.format(titulo=job["title"], url=job["url"], repo=job["repo"])
    except (KeyError, IndexError, ValueError):
        # Chave desconhecida/chaves soltas no template — manda o texto como está.
        texto = template
    # Links [texto](url) viram link clicável no HTML e "texto (url)" no texto puro
    # (que é também o que aparece na mensagem de aprovação do Telegram).
    texto_puro, texto_html = email_sender.render_links(texto.strip())
    return {
        "email_to": job["email_to"],
        "email_da_secao_candidatura": job["email_da_secao_candidatura"],
        "assunto": job["title"],
        "texto": texto_puro,
        "texto_html": texto_html,
        "anexo": _attachment_path(cfg_email),
    }


def check_new_issues(config: dict) -> list[dict]:
    """
    Busca issues ainda não vistas em todos os repos configurados e devolve os jobs (dict
    no mesmo molde de `project` do 99Freelas, com source="github"). `email_to` vem None
    quando o corpo não tem e-mail — quem chama (main.run_github_cycle) decide o que fazer.
    Issues mais antigas que max_issue_age_days são registradas como "skipped_old" aqui
    mesmo. Nunca levanta exceção por causa de um repo: loga e segue pro próximo.
    """
    cfg = config.get("github_jobs") or {}
    max_age = cfg.get("max_issue_age_days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age) if max_age else None

    novas: list[dict] = []
    for entrada in cfg.get("repos") or []:
        repo, labels = entrada.get("repo"), entrada.get("labels") or []
        if not repo:
            continue
        try:
            issues = fetch_issues(repo, labels)
        except Exception as e:
            log.warning("Falha ao buscar issues de %s: %s", repo, e)
            continue

        for issue in issues:
            job_id = job_id_for(repo, issue["number"])
            if already_seen(job_id):
                continue
            criada = datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
            if cutoff and criada < cutoff:
                # Marca como vista pra não reavaliar a cada ciclo.
                register(job_id, issue["title"], "skipped_old", f"criada em {criada.date().isoformat()}")
                continue
            email, da_secao = extract_email(issue.get("body") or "")
            novas.append({
                "id": job_id,
                "source": "github",
                "repo": repo,
                "number": issue["number"],
                "title": issue["title"],
                "url": issue["html_url"],
                "created_at": issue["created_at"],
                "labels": [lbl["name"] for lbl in issue.get("labels", [])],
                "description": issue.get("body") or "",
                "email_to": email,
                "email_da_secao_candidatura": da_secao,
            })
    return novas
