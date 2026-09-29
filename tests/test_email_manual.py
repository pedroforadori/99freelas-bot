"""
E-mail manual pra recrutador: "Nome, Assunto, email" no chat → prévia com Enviar/Cancelar →
envio pelo SMTP com o currículo do APinfo → resultado no Telegram.
"""
import pytest

from bot import approvals
from bot.sources.email_manual import source as source_module
from bot.sources.email_manual.source import parse_request
from tests.conftest import CHAT_ID

CONFIG = {
    "email_manual": {"texto": "Olá, {recrutador}!\nVaga: {assunto}\n{nome}"},
    "apinfo_jobs": {"email": {"anexo": None, "copia_para_mim": True}},
}


@pytest.mark.parametrize("texto, esperado", [
    ("Roberta, Vaga Front End Sr., roberta@teste.com.br", ("Roberta", "Vaga Front End Sr.", "roberta@teste.com.br")),
    ("  Ana ,Dev React, Next e Node , ana@x.io ", ("Ana", "Dev React, Next e Node", "ana@x.io")),
    ("oi, tudo bem?", None),
    ("https://www.99freelas.com.br/project/x-123", None),
    ("Roberta, roberta@teste.com.br", False),
    (", Vaga, roberta@teste.com.br", False),
    ("Roberta, , roberta@teste.com.br", False),
    # sem vírgula: nome = primeira palavra, assunto = o resto
    ("Roberta Vaga Front End Sr. roberta@teste.com.br", ("Roberta", "Vaga Front End Sr.", "roberta@teste.com.br")),
    ("  Roberta   Vaga Front End   roberta@teste.com.br  ", ("Roberta", "Vaga Front End", "roberta@teste.com.br")),
    ("Roberta Silva, Vaga Front End Sr. roberta@teste.com.br", ("Roberta Silva", "Vaga Front End Sr.", "roberta@teste.com.br")),
    ("Roberta roberta@teste.com.br", False),
    ("roberta@teste.com.br", False),
    ("mande pra roberta@teste.com.br.", None),
])
def test_parse_request(texto, esperado):
    assert parse_request(texto) == esperado


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    # id = me-<epoch ms>: congelado pros snapshots (e pra testar a colisão de ids)
    monkeypatch.setattr(source_module.time, "time", lambda: 1790000000.0)


def _pedir(telegram, api, texto="Roberta, Vaga Front End Sr., roberta@teste.com.br", chat_id=CHAT_ID):
    telegram.push_message(texto, chat_id=chat_id)
    api.poll(CONFIG)
    return approvals.get_open()


@pytest.fixture
def smtp(api, monkeypatch):
    monkeypatch.setenv("SMTP_USER", "eu@gmail.com")
    monkeypatch.setenv("EMAIL_FROM_NAME", "Pedro")
    enviados, respostas = [], []

    def fake_send(to, subject, body, attachment_path=None, body_html=None, bcc=None):
        enviados.append({"to": to, "subject": subject, "body": body, "anexo": attachment_path, "bcc": bcc})
        return respostas.pop(0) if respostas else (True, f"e-mail enviado pra {to}")

    api.patch_email_send(fake_send)
    return enviados, respostas


def test_previa_enviar_e_resultado(telegram, api, smtp, snapshot):
    enviados, _ = smtp
    (entry,) = _pedir(telegram, api)
    previa = telegram.last("sendMessage")
    snapshot("email_manual_previa", {"text": previa["text"], "keyboard": previa["reply_markup"]})
    assert enviados == []  # nada sai antes do clique

    telegram.push_callback(f"approve:{entry['project_id']}", message_id=entry["telegram_message_id"])
    api.poll(CONFIG)
    api.process_approvals(CONFIG)

    assert enviados == [{
        "to": "roberta@teste.com.br", "subject": "Vaga Front End Sr.",
        "body": "Olá, Roberta!\nVaga: Vaga Front End Sr.\nPedro", "anexo": None, "bcc": "eu@gmail.com",
    }]
    snapshot("email_manual_enviado", telegram.last("sendMessage"))
    assert approvals.get_pending(entry["project_id"]) is None


def test_cancelar_nao_envia(telegram, api, smtp):
    enviados, _ = smtp
    (entry,) = _pedir(telegram, api)
    telegram.push_callback(f"reject:{entry['project_id']}", message_id=entry["telegram_message_id"])
    api.poll(CONFIG)
    api.process_approvals(CONFIG)
    assert enviados == []
    assert approvals.get_pending(entry["project_id"]) is None


def test_falha_smtp_e_tentar_de_novo(telegram, api, smtp, snapshot):
    enviados, respostas = smtp
    respostas.append((False, "erro SMTP: (535, 'senha errada')"))
    (entry,) = _pedir(telegram, api)
    item_id = entry["project_id"]

    telegram.push_callback(f"approve:{item_id}", message_id=entry["telegram_message_id"])
    api.poll(CONFIG)
    api.process_approvals(CONFIG)
    falha = telegram.last("sendMessage")
    snapshot("email_manual_falha", falha)
    assert falha["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"retry:{item_id}"

    telegram.push_callback(f"retry:{item_id}")
    api.poll(CONFIG)
    api.process_approvals(CONFIG)
    assert len(enviados) == 2
    assert telegram.last("sendMessage")["text"].startswith("<b>[E-mail]</b> ✅ E-mail enviado")


def test_editar_assunto_redesenha_previa(telegram, api, smtp):
    enviados, _ = smtp
    (entry,) = _pedir(telegram, api)
    item_id = entry["project_id"]

    telegram.push_callback(f"editf:a:{item_id}", message_id=entry["telegram_message_id"])
    api.poll(CONFIG)
    prompt_id = 1000 + len(telegram.of("sendMessage"))
    telegram.push_message("Frontend Sênior <React>", reply_to=prompt_id)
    api.poll(CONFIG)
    assert "<b>Assunto:</b> Frontend Sênior &lt;React&gt;" in telegram.last("editMessageText")["text"]

    telegram.push_callback(f"approve:{item_id}", message_id=entry["telegram_message_id"])
    api.poll(CONFIG)
    api.process_approvals(CONFIG)
    assert enviados[0]["subject"] == "Frontend Sênior <React>"


def test_formato_incompleto_avisa(telegram, api):
    assert _pedir(telegram, api, "Roberta, roberta@teste.com.br") == []
    assert "Não entendi" in telegram.last("sendMessage")["text"]


def test_ignora_outro_chat_e_mensagem_sem_email(telegram, api):
    assert _pedir(telegram, api, chat_id="999") == []
    assert _pedir(telegram, api, "bom dia") == []
    assert telegram.of("sendMessage") == []


def test_sem_template_no_config_usa_texto_padrao(telegram, api, smtp):
    telegram.push_message("Roberta, Vaga X, r@x.com")
    api.poll({})
    (entry,) = approvals.get_open()
    assert entry["proposal"]["texto"].startswith("Olá, Roberta, tudo bem?")
    assert entry["proposal"]["bcc"] is None


def test_duas_mensagens_no_mesmo_instante_tem_ids_diferentes(telegram, api):
    telegram.push_message("Roberta, Vaga A, r@x.com")
    telegram.push_message("Carla, Vaga B, c@x.com")
    api.poll(CONFIG)
    ids = sorted(e["project_id"] for e in approvals.get_open())
    assert ids == ["me-1790000000000", "me-1790000000001"]
