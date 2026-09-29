"""
E-mail manual de candidatura: o usuário acha o e-mail de um recrutador fora do bot (ex:
LinkedIn) e escreve no chat do bot principal

    Roberta, Vaga Front End Sr., roberta@empresa.com.br
    Roberta Vaga Front End Sr. roberta@empresa.com.br   (sem vírgula: nome = 1ª palavra)

→ o bot manda uma prévia com "✅ Enviar"/"❌ Cancelar" e, no clique, envia o MESMO currículo
da fonte APinfo (apinfo_jobs.email.anexo, com a mesma cópia oculta) com o texto de
email_manual.texto do config.yaml. Sem histórico de envios (decisão do usuário).

Não é uma fonte de vagas de verdade (não varre nada): é uma "fonte" só pra reaproveitar o
portão de aprovação inteiro — fila em approvals.py, botões aprovar/rejeitar/editar e
"🔄 Tentar de novo" do telegram_dispatcher, decisão gravada antes do envio.
"""
import os
import re
import time

from bot import approvals, email_sender
from bot.logger_setup import get_logger
from bot.sources.apinfo.client import attachment_path
from bot.sources.base import EditableField, JobSource
from bot.sources.email_manual import views
from bot.sources.github.source import DESTINATARIO, parse_email
from bot.telegram_api import chat_id, esc

log = get_logger(__name__)

TEXTO_PADRAO = """Olá, {recrutador}, tudo bem?

Tenho interesse na vaga "{assunto}" e envio meu currículo em anexo.

Fico à disposição para uma conversa.

Atenciosamente,
{nome}"""


def _parse_assunto(raw: str) -> str | None:
    return raw.strip() or None


def _apply_assunto(email: dict, novo: str) -> str:
    email["assunto"] = novo
    return f"Assunto atualizado: {esc(novo)}"


ASSUNTO = EditableField(
    code="a",
    button="✏️ Editar assunto",
    prompt="Digite o assunto do e-mail:",
    invalid_msg="Assunto vazio. Responda de novo à mensagem anterior com o assunto.",
    parse=_parse_assunto,
    apply=_apply_assunto,
)


def parse_request(text: str) -> tuple[str, str, str] | None | bool:
    """
    "Nome, Assunto, email" ou "Nome Assunto email" → (nome, assunto, email). O último
    pedaço é sempre o e-mail. Com vírgula, ela separa nome e assunto (o assunto pode ter
    vírgula e o nome pode ter espaço); sem vírgula, o nome é a primeira palavra. None = não
    é um pedido de e-mail (não termina em e-mail); False = termina em e-mail, mas falta
    nome ou assunto.
    """
    texto = text.strip()
    m = re.match(r"^(.*?)[\s,]+([^\s,]+)$", texto, re.S)
    if m is None:
        return False if parse_email(texto) else None
    email = parse_email(m.group(2))
    if email is None:
        return None
    resto = m.group(1).strip().rstrip(",").strip()
    if "," in resto:
        nome, assunto = resto.split(",", 1)
    else:
        nome, _, assunto = resto.partition(" ")
    nome, assunto = nome.strip(), assunto.strip()
    if not nome or not assunto:
        return False
    return nome, assunto, email


class EmailManualSource(JobSource):
    name = "email_manual"
    tag = views.TAG
    editable_fields = (DESTINATARIO, ASSUNTO)
    # Sem a prévia no Telegram não há botão pra enviar — melhor não enfileirar.
    require_message_id = True

    def is_enabled(self, config: dict) -> bool:
        # Não há varredura pra rodar. handle_message e a resolução das aprovações já
        # percorrem registry.ALL_SOURCES (ligadas ou não), então funciona sem ligar nada.
        return False

    def owns_id(self, project_id: str) -> bool:
        return project_id.startswith("me-")

    def run_cycle(self, config: dict) -> None:
        pass

    def handle_message(self, message: dict, config: dict) -> bool:
        """Mensagem "Nome, Assunto, email" do próprio TELEGRAM_CHAT_ID → prévia pra aprovar."""
        meu_chat = chat_id()
        if not meu_chat or str(message.get("chat", {}).get("id")) != str(meu_chat):
            return False
        pedido = parse_request(message.get("text") or "")
        if pedido is None:
            return False
        if pedido is False:
            views.notify_formato_invalido()
            return True

        nome, assunto, email_to = pedido
        item = {"id": self._new_id(), "title": assunto, "recrutador": nome}
        if not self.queue_for_approval(item, self._build_email(nome, assunto, email_to, config)):
            log.warning("Prévia do e-mail manual pra %s não chegou ao Telegram.", email_to)
        else:
            log.info("E-mail manual aguardando aprovação: %s → %s", assunto, email_to)
        return True

    @staticmethod
    def _new_id() -> str:
        """me-<epoch ms>; duas mensagens no mesmo milissegundo não podem dividir o id."""
        n = int(time.time() * 1000)
        while approvals.get_pending(f"me-{n}") is not None:
            n += 1
        return f"me-{n}"

    def _build_email(self, nome: str, assunto: str, email_to: str, config: dict) -> dict:
        template = (config.get("email_manual") or {}).get("texto") or TEXTO_PADRAO
        try:
            texto = template.strip().format(recrutador=nome, assunto=assunto, nome=os.environ.get("EMAIL_FROM_NAME", ""))
        except (KeyError, IndexError, ValueError):
            # Chave desconhecida/chaves soltas no template — manda o texto como está.
            texto = template.strip()
        plain, html = email_sender.render_links(texto)
        apinfo_email = (config.get("apinfo_jobs") or {}).get("email") or {}
        return {
            "email_to": email_to,
            "assunto": assunto,
            "texto": plain,
            # Só manda HTML se o template usa links [texto](url) — igual ao APinfo.
            "texto_html": html if plain != texto else None,
            "anexo": attachment_path(apinfo_email.get("anexo")),
            "bcc": os.environ.get("SMTP_USER") if apinfo_email.get("copia_para_mim") else None,
        }

    def render_approval(self, project: dict, proposal: dict) -> tuple[str, dict]:
        return views.approval_text(project, proposal), views.approval_keyboard(project["id"], self.edit_buttons(project["id"]))

    def deliver(self, entry: dict) -> tuple[bool, str]:
        email = entry["proposal"]
        return email_sender.send(
            email["email_to"], email["assunto"], email["texto"], email.get("anexo"), email.get("texto_html"),
            bcc=email.get("bcc"),
        )

    def on_delivered(self, entry: dict, success: bool, detail: str) -> None:
        log.info("%s (e-mail manual): '%s' — %s", "E-MAIL ENVIADO" if success else "FALHOU", entry["project"]["title"], detail)
        views.notify_result(entry["project_id"], entry["proposal"], success, detail)

    def on_rejected(self, entry: dict) -> None:
        log.info("E-mail manual cancelado pelo usuário: '%s'", entry["project"]["title"])
