"""
Mensagens do Telegram das vagas do GitHub. Tudo que vem da issue é escapado — o corpo é
markdown livre, pode ter "<" que quebraria o parse_mode HTML.
"""
import os

from bot import telegram_api
from bot.sources.github import client
from bot.telegram_api import esc

TAG = "<b>[GitHub]</b>"
_PREFIX = f"{TAG} "


def approval_text(job: dict, email: dict) -> str:
    """Pedido de aprovação de uma candidatura por e-mail (email = client.build_email)."""
    cabecalho = (
        f"{_PREFIX}📧 <b>Vaga pra aprovar (e-mail)</b>\n"
        f"<b>Vaga:</b> {esc(job.get('title', ''))}\n"
        f"<b>Link:</b> {esc(job.get('url', ''))}\n"
        f"<b>Repo:</b> {esc(job.get('repo', ''))}"
        + (f" · {esc(', '.join(job['labels']))}" if job.get("labels") else "")
        + "\n\n"
    )
    info = f"<b>Para:</b> {esc(email['email_to'])}"
    if email.get("email_editado"):
        info += " · editado por você"
    info += "\n"
    if not email.get("email_da_secao_candidatura") and not email.get("email_editado"):
        info += (
            "⚠️ Esse e-mail não está na seção \"Como se candidatar\" — pode ser só contato de "
            "feedback. Confira no link antes de aprovar.\n"
        )
    anterior = client.last_email_sent_to(email["email_to"])
    if anterior:
        info += f"⚠️ Você já mandou e-mail pra esse endereço ({esc(anterior['title'])}, {anterior['timestamp'][:10]}).\n"
    info += f"<b>Assunto:</b> {esc(email['assunto'])}\n"
    anexo = email.get("anexo")
    if anexo:
        existe = os.path.isfile(anexo)
        info += f"<b>Anexo:</b> {esc(os.path.basename(anexo))}" + ("" if existe else " ⚠️ <b>arquivo não encontrado</b>") + "\n"
    else:
        info += "<b>Anexo:</b> nenhum (github_jobs.email.anexo vazio)\n"

    texto_email = esc(email["texto"])
    descricao = esc(job.get("description") or "")
    moldura = "\n<b>Descrição da vaga:</b>\n\n\n<b>E-mail:</b>\n"
    overhead = len(cabecalho) + len(info) + len(moldura) + len(texto_email) + 50
    max_desc_chars = max(telegram_api.MSG_LIMIT - overhead, 200)
    descricao = telegram_api.truncate_escaped(descricao, max_desc_chars)

    return f"{cabecalho}{info}\n<b>Descrição da vaga:</b>\n{descricao}\n\n<b>E-mail:</b>\n{texto_email}"


def approval_keyboard(job_id: str, edit_buttons: list[dict]) -> dict:
    return {
        "inline_keyboard": [
            edit_buttons,
            [
                {"text": "✅ Aprovar e enviar", "callback_data": f"approve:{job_id}"},
                {"text": "❌ Rejeitar", "callback_data": f"reject:{job_id}"},
            ],
        ]
    }


def notify_no_email(job: dict) -> None:
    """Vaga nova sem e-mail no corpo da issue — só avisa (candidatura pelo link), sem botões."""
    telegram_api.send_message(
        f"{_PREFIX}🔗 <b>Vaga nova sem e-mail</b> — candidatura pelo link\n"
        f"<b>Vaga:</b> {esc(job.get('title', ''))}\n"
        f"<b>Link:</b> {esc(job.get('url', ''))}"
    )


def notify_email_result(job: dict, email: dict, success: bool, detail: str) -> None:
    """Resultado do envio de um e-mail aprovado. Falha ganha "🔄 Tentar de novo"."""
    titulo = "✅ E-mail enviado" if success else "⚠️ Falha ao enviar e-mail"
    linhas = [
        f"{_PREFIX}{titulo}",
        f"<b>Vaga:</b> {esc(job.get('title', ''))}",
        f"<b>Link:</b> {esc(job.get('url', ''))}",
        f"<b>Para:</b> {esc(email.get('email_to', ''))}",
        f"<b>Detalhe:</b> {esc(detail)}",
    ]
    reply_markup = None
    if not success:
        reply_markup = telegram_api.single_button_keyboard("🔄 Tentar de novo", f"retry:{job['id']}")
    telegram_api.send_message("\n".join(linhas), reply_markup)
