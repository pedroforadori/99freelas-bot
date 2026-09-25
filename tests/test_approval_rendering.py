"""
Snapshot dos textos/teclados mandados pro Telegram. Os snapshots em tests/snapshots/ foram
gerados pelo código ANTES do refactor de fontes — mudança aqui = mudança visível pro
usuário no Telegram, então só regrave (UPDATE_SNAPSHOTS=1) se for intencional.
"""
import json
import re

import pytest

from bot import connections, storage
from tests.conftest import github_email, github_job, project_99, proposal_99


def _sent(telegram) -> list[dict]:
    """Chamadas sendMessage sem o chat_id (constante) — é o que vai pro snapshot."""
    return [{k: v for k, v in p.items() if k != "chat_id"} for p in telegram.of("sendMessage")]


# --- 99Freelas ----------------------------------------------------------------------------

CASOS_99 = {
    "basico": (project_99(), proposal_99()),
    "ia_falhou": (project_99(), proposal_99(texto_ia_falhou=True, texto_variante="template")),
    "sugestao_ia_com_desconto": (
        project_99(),
        proposal_99(origem_valor="ia", oferta_sugerida_ia=2400.0, oferta=1000.0, media_concorrentes=None, media_prazo=None),
    ),
    "ajustado_manualmente": (project_99(), proposal_99(ajustado_manualmente=True, oferta=900.0)),
    "proposta_antiga_sem_campos": (
        project_99(),
        {"oferta": 500.0, "prazo_dias": 5, "texto": "Texto antigo", "full_description": None},
    ),
    "descricao_longa_truncada": (project_99(), proposal_99(full_description="x" * 5000)),
}


@pytest.mark.parametrize("caso", sorted(CASOS_99))
def test_render_99(api, snapshot, caso):
    project, proposal = CASOS_99[caso]
    text, keyboard = api.render_approval(project, proposal)
    snapshot(f"render_99_{caso}", {"text": text, "keyboard": keyboard})


def test_render_99_limite_diario_atingido(api, snapshot):
    for i in range(8):  # daily_quota(240) em setembro == 8
        storage.register_application(f"x{i}", "t", status="sent")
    text, _ = api.render_approval(project_99(), proposal_99())
    assert "Limite diário já atingido" in text
    assert "<b>Hoje:</b> 8/8" in text
    snapshot("render_99_limite_diario_atingido", text)


def test_send_approval_request_99(api, telegram, snapshot):
    message_id = api.send_approval_request(project_99(), proposal_99())
    assert message_id == 1001
    snapshot("send_approval_request_99", _sent(telegram))


def test_send_approval_request_telegram_fora(api, telegram):
    telegram.down = True
    assert api.send_approval_request(project_99(), proposal_99()) is None


# --- notify_proposal_result ---------------------------------------------------------------


def _com_conexoes():
    connections._save_cache({
        "plano_restantes": 230, "plano_total": 240, "nao_expiraveis": 6,
        "disponiveis": 236, "baseline_sent_today": 0,
    })


@pytest.mark.parametrize("status,simulated,with_proposal", [
    ("sent", False, True),
    ("failed", False, True),
    ("failed", False, False),
    ("sent", True, True),
    ("failed", True, True),
])
def test_notify_proposal_result(api, telegram, snapshot, status, simulated, with_proposal):
    _com_conexoes()
    storage.register_application("antes", "t", status="sent")
    proposal = proposal_99() if with_proposal else None
    api.notify_proposal_result(project_99(), proposal, status, "detalhe do resultado", simulated=simulated)
    snapshot(f"notify_proposal_result_{status}_sim{int(simulated)}_prop{int(with_proposal)}", _sent(telegram))


def test_notify_manual_project_failed(api, telegram, snapshot):
    api.notify_manual_project_failed("https://www.99freelas.com.br/project/site-legal-785500", "projeto foi fechado")
    api.notify_manual_project_failed(
        "https://www.99freelas.com.br/project/site-legal-785500", "já aguardando", retry=False
    )
    snapshot("notify_manual_project_failed", _sent(telegram))


def test_notify_new_messages(api, telegram, snapshot):
    api.notify_new_messages(3, 1)
    snapshot("notify_new_messages", _sent(telegram))


# --- GitHub ---------------------------------------------------------------------------------


def _anexo(tmp_path):
    path = tmp_path / "curriculo.pdf"
    path.write_bytes(b"%PDF")
    return str(path)


def test_render_github_basico(api, snapshot, isolated):
    text, keyboard = api.render_approval(github_job(), github_email(anexo=_anexo(isolated)))
    snapshot("render_github_basico", {"text": text.replace(str(isolated), "<tmp>"), "keyboard": keyboard})


CASOS_GH = {
    "sem_anexo": (github_job(), github_email()),
    "anexo_inexistente": (github_job(), github_email(anexo="/nao/existe/cv.pdf")),
    "email_fora_da_secao": (github_job(), github_email(email_da_secao_candidatura=False)),
    "email_editado": (github_job(), github_email(email_da_secao_candidatura=False, email_editado=True, email_to="rh@acme.com")),
    "sem_labels": (github_job(labels=[]), github_email()),
    "descricao_longa_com_html": (github_job(description="a & b < c " * 900), github_email(texto="Oi <b>&</b>")),
}


@pytest.mark.parametrize("caso", sorted(CASOS_GH))
def test_render_github(api, snapshot, caso):
    job, email = CASOS_GH[caso]
    text, keyboard = api.render_approval(job, email)
    snapshot(f"render_github_{caso}", {"text": text, "keyboard": keyboard})


def test_render_github_email_ja_enviado_antes(api, snapshot):
    api.github.register("gh-x#1", "Vaga anterior", "email_sent", "ok", {"email_to": "VAGAS@acme.com"})
    text, _ = api.render_approval(github_job(), github_email())
    assert "Você já mandou e-mail pra esse endereço" in text
    # A data do envio anterior vem do relógio real (register usa utcnow).
    snapshot("render_github_email_ja_enviado", re.sub(r"\d{4}-\d{2}-\d{2}", "<data>", text))


def test_notify_github(api, telegram, snapshot):
    api.notify_github_no_email(github_job(email_to=None))
    api.notify_github_email_result(github_job(), github_email(), True, "e-mail enviado pra vagas@acme.com")
    api.notify_github_email_result(github_job(), github_email(), False, "erro SMTP: boom <x>")
    snapshot("notify_github", _sent(telegram))


def test_render_99_escapa_conteudo_externo(api):
    """Título/descrição/texto com "<" ou "&" não podem quebrar o parse_mode HTML do Telegram."""
    project = project_99(title="App <React> & Node")
    proposal = proposal_99(texto="Uso <b> & afins", full_description="a < b & c > d")
    text, _ = api.render_approval(project, proposal)
    assert "App &lt;React&gt; &amp; Node" in text
    assert "Uso &lt;b&gt; &amp; afins" in text
    assert "a &lt; b &amp; c &gt; d" in text


def test_render_99_trunca_sem_partir_entidade(api):
    for n in range(4000, 4012):  # varia o ponto de corte em volta das entidades
        text, _ = api.render_approval(project_99(), proposal_99(full_description="&" * n))
        corpo = text.split("<b>Descrição do projeto:</b>\n")[1].split("… (veja mais no link)")[0]
        assert corpo == "&amp;" * (len(corpo) // 5), n
        assert len(text) <= 4096


def test_render_escolhe_por_origem(api):
    """Projeto sem "source" (formato antigo de pending_approvals.json) = 99Freelas."""
    text_99, kb_99 = api.render_approval(project_99(), proposal_99())
    text_gh, kb_gh = api.render_approval(github_job(), github_email())
    assert text_99.startswith("<b>[99Freelas]</b>")
    assert text_gh.startswith("<b>[GitHub]</b>")
    assert "editf:o:785400" in json.dumps(kb_99)
    assert "editf:e:gh-frontendbr/vagas#123" in json.dumps(kb_gh)
