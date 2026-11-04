"""
App de celular (iOS/Android) nunca sai abaixo de R$ 1.200 (regra do usuário, 2026-10-28 —
caso real: app de despertador com desafios matinais saiu por R$ 150).
"""
import pytest

from bot import ai_writer, approvals, submitter
from bot import proposal as proposal_mod
from bot.sources.freelas99 import views
from bot.sources.freelas99.source import _apply_oferta
from tests.conftest import project_99, proposal_99

PID = "785400"
DESPERTADOR = (
    "Quero um aplicativo de despertador com desafios para acordar: o usuário cria conta, "
    "configura alarmes e acompanha a evolução diária. Precisa mandar alertas no celular."
)


@pytest.mark.parametrize("texto", [
    DESPERTADOR,
    "App para Android e iOS de delivery",
    "Publicar meu app na Play Store",
    "Aplicativo em Flutter para clínica",
    "Preciso de um app mobile para minha academia",
    "aplicativo para celular de controle de gastos",
])
def test_reconhece_app_de_celular(texto):
    assert proposal_mod.app_de_celular(texto)


@pytest.mark.parametrize("texto", [
    "Site institucional para clínica, que funcione bem no celular",
    "Sistema web de agendamento",
    "Aplicativo web (PWA) para controle de estoque, acessado pelo celular",
    "Integração com o app do WhatsApp no meu site",
    "Landing page responsiva",
])
def test_site_e_sistema_web_nao_contam(texto):
    assert not proposal_mod.app_de_celular(texto)


@pytest.fixture
def ia_barata(monkeypatch):
    monkeypatch.setattr(ai_writer, "suggest_price_and_deadline", lambda *a, **k: (150.0, 15))
    monkeypatch.setattr(ai_writer, "generate_proposal_text", lambda *a, **k: "Olá, tudo bem?")


def _config(**extra):
    return {"proposal": {"ia_provider": "gemini", "ia_model": "x", "valor_modo": "automatico",
                         "oferta_estrategia": "ia", "texto_modo": "ia", **extra}}


def test_caso_real_sobe_pra_1200(ia_barata):
    p = proposal_mod.build_proposal(project_99(title="App despertador"), _config(), full_description=DESPERTADOR)
    assert p["oferta"] == 1200.0 and p["app_celular"] and p["oferta_minima"] == 1200.0
    linha = views._origem_line(p)
    assert "App de celular: mínimo de R$ 1200,00" in linha
    assert "IA sugeriu R$ 150,00 — subiu pro mínimo de app" in linha


def test_minimo_configuravel(ia_barata):
    p = proposal_mod.build_proposal(project_99(), _config(oferta_minima_app=2000), full_description=DESPERTADOR)
    assert p["oferta"] == 2000.0


def test_site_continua_com_o_minimo_normal(ia_barata):
    p = proposal_mod.build_proposal(project_99(), _config(), full_description="Site institucional com 5 páginas.")
    assert p["oferta"] == 150.0 and "app_celular" not in p
    assert p["oferta_minima"] == proposal_mod.OFERTA_MINIMA


def test_valor_manual_guarda_o_piso():
    config = {"proposal": {"valor_modo": "manual"}}
    p = proposal_mod.build_proposal(project_99(), config, full_description=DESPERTADOR)
    assert p["oferta"] is None and p["oferta_minima"] == 1200.0
    assert "App de celular" in views._origem_line(p)


def test_envio_recusa_abaixo_do_piso(telegram):
    proposal = proposal_99(oferta=150.0, oferta_minima=1200.0, app_celular=True)
    ok, detail = submitter.finalize_submission(object(), project_99(), proposal)
    assert not ok and "abaixo do mínimo de R$ 1200,00 (app de celular)" in detail


def test_editar_oferta_sobe_pro_piso():
    proposal = proposal_99(oferta_minima=1200.0, app_celular=True)
    ack = _apply_oferta(proposal, 500.0)
    assert proposal["oferta"] == 1200.0 and "mínimo pra app de celular" in ack
    normal = proposal_99()
    _apply_oferta(normal, 500.0)
    assert normal["oferta"] == 500.0


def test_reply_valor_manual_abaixo_do_piso_e_recusado(api, telegram):
    approvals.add_pending(
        {**project_99(), "source": "99freelas"},
        proposal_99(origem_valor="manual", oferta=None, prazo_dias=None, oferta_minima=1200.0, app_celular=True),
        555,
    )
    config = {"proposal": {"ia_provider": "gemini", "ia_model": "x"}}
    telegram.push_message("500 10", reply_to=555)
    api.poll(config)
    assert approvals.get_pending(PID)["decision"] is None
    assert "mínimo pra app de celular" in telegram.last("sendMessage")["text"]
    telegram.push_message("1300 10", reply_to=555)
    api.poll(config)
    assert approvals.get_pending(PID)["decision"] == "approved"
