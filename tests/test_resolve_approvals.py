"""
Resolução de decisões já gravadas (main.process_pending_approvals antes do refactor): é
aqui que o envio de verdade acontece — proposta no 99Freelas (Playwright, mockado) ou
e-mail do GitHub (SMTP, mockado).
"""
import pytest

from bot import approvals, storage
from tests.conftest import github_email, github_job, project_99, proposal_99

CONFIG = {"proposal": {}}
PID = "785400"
GH_ID = "gh-frontendbr/vagas#123"


def _markups(telegram) -> list[str]:
    return [p["reply_markup"]["inline_keyboard"][0][0]["text"] for p in telegram.of("editMessageReplyMarkup")]


@pytest.fixture
def envios(api):
    """Mocka o envio real das duas fontes e grava o que seria enviado."""
    log = {"99": [], "email": [], "result_99": (True, "proposta enviada"), "result_email": (True, "e-mail enviado pra x")}

    def finalize(page, project, proposal, dry_run=False):
        assert page is api.page
        log["99"].append((project["id"], proposal))
        return log["result_99"]

    def send(to, subject, body, attachment=None, body_html=None):
        log["email"].append((to, subject, body, attachment, body_html))
        return log["result_email"]

    api.patch_submitter("finalize_submission", finalize)
    api.patch_email_send(send)
    return log


# --- 99Freelas ------------------------------------------------------------------------------


def test_99_aprovado_enviado(api, telegram, envios):
    proposal = proposal_99(ajustado_manualmente=True)
    approvals.add_pending(project_99(), proposal, 555)
    approvals.record_decision(PID, "approved")
    api.process_approvals(CONFIG)

    assert envios["99"] == [(PID, proposal)]
    assert approvals.get_pending(PID) is None
    rec = storage.get_application(PID)
    assert rec["status"] == "sent"
    assert rec["detail"] == "proposta enviada"
    assert {k: rec[k] for k in ("oferta", "prazo_dias", "origem_valor", "texto_variante", "media_concorrentes", "media_prazo", "ajustado_manualmente")} == {
        "oferta": 1200.0, "prazo_dias": 8, "origem_valor": "media_arredondada", "texto_variante": "pergunta",
        "media_concorrentes": 1234.5, "media_prazo": 10, "ajustado_manualmente": True,
    }
    assert "texto" not in rec
    assert storage.proposals_sent_today() == 1
    assert _markups(telegram) == ["✅ Aprovada e enviada"]
    assert telegram.last("editMessageReplyMarkup")["message_id"] == 555
    assert any("🚀 Enviando proposta aprovada" in p["text"] for p in telegram.of("sendMessage"))


def test_99_aprovado_falhou(api, telegram, envios):
    envios["result_99"] = (False, "projeto foi fechado")
    approvals.add_pending(project_99(), proposal_99(), 555)
    approvals.record_decision(PID, "approved")
    api.process_approvals(CONFIG)

    assert approvals.get_pending(PID)["decision"] == "failed"  # guardado pro "Tentar de novo"
    assert storage.get_application(PID)["status"] == "failed"
    # Comportamento atual: falha de envio aparece com o rótulo de "Rejeitada" + motivo.
    assert _markups(telegram) == ["❌ Rejeitada — projeto foi fechado"]


def test_99_rejeitado(api, telegram, envios):
    approvals.add_pending(project_99(), proposal_99(), 555)
    approvals.record_decision(PID, "rejected")
    api.process_approvals(CONFIG)

    assert envios["99"] == []
    assert approvals.get_pending(PID) is None
    assert storage.get_application(PID)["status"] == "rejected_by_user"
    assert _markups(telegram) == ["❌ Rejeitada — rejeitada por você via Telegram"]
    assert any("❌ Rejeitada por você" in p["text"] and p["text"].startswith("<b>[99Freelas]</b> ") for p in telegram.of("sendMessage"))


def test_sem_decisao_nao_faz_nada(api, telegram, envios):
    approvals.add_pending(project_99(), proposal_99(), 555)
    api.process_approvals(CONFIG)
    assert envios["99"] == []
    assert approvals.get_pending(PID)["decision"] is None
    assert telegram.calls == []


def test_entrada_failed_nao_e_reprocessada(api, envios):
    approvals.add_pending(project_99(), proposal_99(), 555)
    approvals.record_decision(PID, "approved")
    approvals.mark_failed(PID)
    api.process_approvals(CONFIG)
    assert envios["99"] == []


def test_entrada_antiga_sem_source_e_99(api, envios):
    project = project_99()
    project.pop("source", None)
    approvals.add_pending(project, proposal_99(), 555)
    approvals.record_decision(PID, "approved")
    api.process_approvals(CONFIG)
    assert [pid for pid, _ in envios["99"]] == [PID]
    assert envios["email"] == []


# --- GitHub -----------------------------------------------------------------------------------


def test_github_aprovado_enviado(api, telegram, envios):
    email = github_email(anexo="/tmp/cv.pdf")
    approvals.add_pending(github_job(), email, 556)
    approvals.record_decision(GH_ID, "approved")
    api.process_approvals(CONFIG)

    assert envios["99"] == []
    assert envios["email"] == [("vagas@acme.com", email["assunto"], email["texto"], "/tmp/cv.pdf", email["texto_html"])]
    assert approvals.get_pending(GH_ID) is None
    rec = api.github.get_record(GH_ID)
    assert (rec["status"], rec["email_to"], rec["url"]) == ("email_sent", "vagas@acme.com", github_job()["url"])
    # e-mail NUNCA entra em applied_jobs.json (somaria na cota de conexões do 99Freelas)
    assert storage.get_application(GH_ID) is None
    assert storage.proposals_sent_today() == 0
    assert _markups(telegram) == ["✅ Aprovada e enviada"]
    resultado = telegram.last("sendMessage")
    assert resultado["text"].startswith("<b>[GitHub]</b> ✅ E-mail enviado")
    assert "reply_markup" not in resultado


def test_github_aprovado_falhou(api, telegram, envios):
    envios["result_email"] = (False, "erro SMTP: boom")
    approvals.add_pending(github_job(), github_email(), 556)
    approvals.record_decision(GH_ID, "approved")
    api.process_approvals(CONFIG)

    assert approvals.get_pending(GH_ID)["decision"] == "failed"
    assert api.github.get_record(GH_ID)["status"] == "failed"
    resultado = telegram.last("sendMessage")
    assert resultado["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"retry:{GH_ID}"


def test_github_rejeitado(api, telegram, envios):
    approvals.add_pending(github_job(), github_email(), 556)
    approvals.record_decision(GH_ID, "rejected")
    api.process_approvals(CONFIG)

    assert envios["email"] == []
    assert approvals.get_pending(GH_ID) is None
    assert api.github.get_record(GH_ID)["status"] == "rejected_by_user"
    assert storage.get_application(GH_ID) is None
    assert _markups(telegram) == ["❌ Rejeitada — rejeitada por você via Telegram"]


def test_github_ja_enviado_nao_reenvia(api, telegram, envios):
    """Crash entre o SMTP e o approvals.resolve: o registro já diz email_sent."""
    approvals.add_pending(github_job(), github_email(), 556)
    approvals.record_decision(GH_ID, "approved")
    api.github.register(GH_ID, "Vaga", "email_sent", "ok", {"email_to": "vagas@acme.com"})
    api.process_approvals(CONFIG)

    assert envios["email"] == []
    assert approvals.get_pending(GH_ID) is None


def test_mistura_de_fontes(api, envios):
    approvals.add_pending(project_99(), proposal_99(), 555)
    approvals.add_pending(github_job(), github_email(), 556)
    approvals.record_decision(PID, "approved")
    approvals.record_decision(GH_ID, "approved")
    api.process_approvals(CONFIG)
    assert [pid for pid, _ in envios["99"]] == [PID]
    assert [e[0] for e in envios["email"]] == ["vagas@acme.com"]
