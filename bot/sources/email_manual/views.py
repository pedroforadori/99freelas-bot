"""
Mensagens do Telegram do e-mail manual (recrutador achado fora do bot, ex: LinkedIn).
Tudo que o usuário digitou é escapado — nome/assunto podem ter "<" ou "&".
"""
import os

from bot import telegram_api
from bot.telegram_api import esc

TAG = "<b>[E-mail]</b>"
_PREFIX = f"{TAG} "

FORMATO = "Nome, Assunto, email@empresa.com"


def approval_text(project: dict, email: dict) -> str:
    """Prévia do e-mail antes do envio (email = EmailManualSource._build_email)."""
    info = (
        f"{_PREFIX}📧 <b>E-mail pra enviar</b>\n"
        f"<b>Para:</b> {esc(email['email_to'])}"
        + (" · editado por você" if email.get("email_editado") else "")
        + f"\n<b>Recrutador(a):</b> {esc(project.get('recrutador', ''))}\n"
        f"<b>Assunto:</b> {esc(email['assunto'])}\n"
    )
    anexo = email.get("anexo")
    if anexo:
        existe = os.path.isfile(anexo)
        info += f"<b>Anexo:</b> {esc(os.path.basename(anexo))}" + ("" if existe else " ⚠️ <b>arquivo não encontrado</b>") + "\n"
    else:
        info += "<b>Anexo:</b> nenhum (apinfo_jobs.email.anexo vazio)\n"
    return f"{info}\n<b>E-mail:</b>\n{esc(email['texto'])}"


def approval_keyboard(item_id: str, edit_buttons: list[dict]) -> dict:
    return {
        "inline_keyboard": [
            edit_buttons,
            [
                {"text": "✅ Enviar", "callback_data": f"approve:{item_id}"},
                {"text": "❌ Cancelar", "callback_data": f"reject:{item_id}"},
            ],
        ]
    }


def notify_result(item_id: str, email: dict, success: bool, detail: str) -> None:
    """Resultado do envio. Falha ganha "🔄 Tentar de novo" (reenvia o MESMO e-mail)."""
    titulo = "✅ E-mail enviado" if success else "⚠️ Falha ao enviar e-mail"
    linhas = [
        f"{_PREFIX}{titulo}",
        f"<b>Para:</b> {esc(email.get('email_to', ''))}",
        f"<b>Assunto:</b> {esc(email.get('assunto', ''))}",
        f"<b>Detalhe:</b> {esc(detail)}",
    ]
    reply_markup = None
    if not success:
        reply_markup = telegram_api.single_button_keyboard("🔄 Tentar de novo", f"retry:{item_id}")
    telegram_api.send_message("\n".join(linhas), reply_markup)


def notify_formato_invalido() -> None:
    telegram_api.send_message(
        f"{_PREFIX}⚠️ Não entendi. Pra mandar o currículo, escreva:\n<code>{esc(FORMATO)}</code>\n"
        "ex: <code>Roberta, Vaga Front End Sr., roberta@empresa.com.br</code>\n"
        "ou, sem vírgula (nome = primeira palavra): <code>Roberta Vaga Front End Sr. roberta@empresa.com.br</code>"
    )

