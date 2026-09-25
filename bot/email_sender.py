"""
Envio de e-mail por SMTP (candidatura a vagas do GitHub, ver github_jobs.py) — só chamado
por main.process_pending_approvals depois da aprovação no Telegram.

Config no .env: SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = SSL), SMTP_USER,
SMTP_PASSWORD, EMAIL_FROM (default SMTP_USER) e EMAIL_FROM_NAME (opcional). No Gmail,
SMTP_PASSWORD precisa ser uma "senha de app" (conta com verificação em 2 etapas), não a
senha normal da conta.
"""
import html
import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from bot.logger_setup import get_logger

log = get_logger(__name__)


_LINK_REGEX = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def render_links(text: str) -> tuple[str, str]:
    """
    Converte links no formato [texto](url) do template (github_jobs.email.texto) em
    (texto_puro, html): no texto puro vira "texto (url)"; no HTML, <a href> com o resto do
    texto escapado e quebras de linha em <br>. O e-mail vai com as duas versões — o
    cliente de e-mail mostra o HTML (link na palavra) e cai pro texto puro se não suportar.
    """
    plain = _LINK_REGEX.sub(r"\1 (\2)", text)
    escaped = html.escape(text, quote=False)
    body = _LINK_REGEX.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', escaped)
    body_html = f'<div style="font-family: Arial, sans-serif; font-size: 14px;">{body.replace(chr(10), "<br>")}</div>'
    return plain, body_html


def send(
    to: str, subject: str, body: str, attachment_path: str | None = None, body_html: str | None = None
) -> tuple[bool, str]:
    """Retorna (sucesso, detalhe). Nunca levanta exceção. `body_html` vira a alternativa HTML."""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    if not host or not user or not password:
        return False, "SMTP_HOST/SMTP_USER/SMTP_PASSWORD não configurados no .env"
    port = int(os.environ.get("SMTP_PORT", 587))
    from_addr = os.environ.get("EMAIL_FROM") or user
    from_name = os.environ.get("EMAIL_FROM_NAME") or ""

    if attachment_path and not os.path.isfile(attachment_path):
        return False, f"anexo não encontrado: {attachment_path}"

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    msg.set_content(body)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    if attachment_path:
        ctype, _ = mimetypes.guess_type(attachment_path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(attachment_path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(attachment_path))

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(msg)
    except Exception as e:
        log.warning("Falha ao enviar e-mail pra %s: %s", to, e)
        return False, f"erro SMTP: {e}"
    return True, f"e-mail enviado pra {to}"
