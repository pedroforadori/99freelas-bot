"""
Fonte GitHub: vagas publicadas como issues (github_jobs no config.yaml), candidatura por
e-mail (SMTP, email_sender.py) depois da aprovação no Telegram. Não usa o Playwright.
Sem e-mail no corpo da issue → só avisa com o link, uma vez.
"""
import re

from bot import email_sender
from bot.logger_setup import get_logger
from bot.sources.base import EditableField, JobSource
from bot.sources.github import client, views
from bot.telegram_api import esc

log = get_logger(__name__)

_EMAIL_REGEX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _parse_email(raw: str) -> str | None:
    novo = raw.strip()
    return novo if _EMAIL_REGEX.match(novo) else None


def _apply_email(email: dict, novo: str) -> str:
    email["email_to"] = novo
    email["email_editado"] = True
    return f"Destinatário atualizado: {esc(novo)}"


DESTINATARIO = EditableField(
    code="e",
    button="✏️ Editar destinatário",
    prompt="Digite o e-mail de destino (ex: vagas@empresa.com):",
    invalid_msg="Não entendi o e-mail. Responda de novo à mensagem anterior com o endereço de destino.",
    parse=_parse_email,
    apply=_apply_email,
)


class GitHubSource(JobSource):
    name = "github"
    tag = views.TAG
    editable_fields = (DESTINATARIO,)
    # Telegram fora: sem mensagem não há botão pra aprovar — a vaga não é registrada e
    # volta a ser "nova" no próximo ciclo.
    require_message_id = True

    def is_enabled(self, config: dict) -> bool:
        return bool((config.get("github_jobs") or {}).get("enabled"))

    def owns_id(self, project_id: str) -> bool:
        return project_id.startswith("gh-")

    def run_cycle(self, config: dict) -> None:
        queued = 0
        for job in client.check_new_issues(config):
            if not job["email_to"]:
                log.info("Vaga do GitHub sem e-mail: '%s'", job["title"])
                views.notify_no_email(job)
                client.register(job["id"], job["title"], "no_email", "sem e-mail no corpo da issue", {"url": job["url"]})
                continue
            if queued >= self.MAX_QUEUED_PER_CYCLE:
                break  # não registra — fica pro próximo ciclo
            email = client.build_email(job, config)
            if not self.queue_for_approval(job, email):
                log.warning("Pedido de aprovação da vaga '%s' não chegou ao Telegram — tenta de novo no próximo ciclo.", job["title"])
                continue
            client.register(
                job["id"], job["title"], "pending_approval", "aguardando aprovação no Telegram",
                {"url": job["url"], "email_to": email["email_to"]},
            )
            log.info("Vaga do GitHub aguardando aprovação: '%s' → %s", job["title"], email["email_to"])
            queued += 1

    def render_approval(self, project: dict, proposal: dict) -> tuple[str, dict]:
        return views.approval_text(project, proposal), views.approval_keyboard(project["id"], self.edit_buttons(project["id"]))

    def already_delivered(self, entry: dict) -> bool:
        rec = client.get_record(entry["project_id"])
        return bool(rec and rec.get("status") == "email_sent")

    def deliver(self, entry: dict) -> tuple[bool, str]:
        email = entry["proposal"]
        return email_sender.send(email["email_to"], email["assunto"], email["texto"], email.get("anexo"), email.get("texto_html"))

    def on_delivered(self, entry: dict, success: bool, detail: str) -> None:
        job, email = entry["project"], entry["proposal"]
        log.info("%s (vaga GitHub): '%s' — %s", "E-MAIL ENVIADO" if success else "FALHOU", job["title"], detail)
        client.register(
            entry["project_id"], job["title"], "email_sent" if success else "failed", detail,
            {"url": job["url"], "email_to": email["email_to"]},
        )
        views.notify_email_result(job, email, success, detail)

    def on_rejected(self, entry: dict) -> None:
        job = entry["project"]
        log.info("Vaga do GitHub rejeitada pelo usuário: '%s'", job["title"])
        client.register(entry["project_id"], job["title"], "rejected_by_user", "rejeitada via Telegram", {"url": job["url"]})
