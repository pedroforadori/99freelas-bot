"""
Envio automático (proposal.envio_automatico, 2026-10-14): projetos da varredura saem sem
aprovação, com valor/prazo elaborados pela IA (oferta_estrategia "ia"), e no Telegram só chega
o resultado (enviada ou falha). E-commerce fica de fora pelo keywords_exclude, também na
descrição completa.
"""
import pytest

from bot import ai_writer, approvals, connections, scraper, storage, submitter
from bot import proposal as proposal_mod
from bot.filter import excluded_keyword
from bot.sources.freelas99 import views
from tests.conftest import project_99, proposal_99

PID = "785400"
IA = {"ia_provider": "gemini", "ia_model": "x"}


def _config(**auto) -> dict:
    return {
        "proposal": {
            **IA,
            "valor_modo": "automatico",
            "oferta_estrategia": "ia",
            "envio_automatico": {"ativo": True, **auto},
        },
        "keywords_exclude": ["e-commerce", "loja virtual"],
    }


@pytest.fixture
def site(api, monkeypatch):
    state = {"projects": [project_99()], "prepare": lambda project: (proposal_99(), "ok"), "enviados": []}

    def prepare(page, project, config, require_average=False, check_exclude=False):
        assert check_exclude
        return state["prepare"](project)

    def finalize(page, project, proposal, dry_run=False):
        state["enviados"].append(project["id"])
        views.notify_proposal_result(project, proposal, "sent", "proposta enviada")  # como submitter._finish
        return True, "proposta enviada"

    monkeypatch.setattr(connections, "refresh", lambda page: None)
    monkeypatch.setattr(scraper, "fetch_open_projects", lambda page: [dict(p) for p in state["projects"]])
    api.patch_submitter("prepare_proposal", prepare)
    api.patch_submitter("finalize_submission", finalize)
    return state


def _textos(telegram):
    return [p["text"] for p in telegram.of("sendMessage")]


# --- envio -----------------------------------------------------------------------------------


def test_envia_sem_pedir_aprovacao(api, telegram, monkeypatch, site):
    monkeypatch.setenv("ACTIVITY_LOG_TELEGRAM", "false")
    api.run_cycle(_config())

    assert site["enviados"] == [PID]
    rec = storage.get_application(PID)
    assert rec["status"] == "sent"
    assert rec["oferta"] == 1200.0
    assert approvals.get_pending(PID) is None
    textos = _textos(telegram)
    assert len(textos) == 1 and "Proposta enviada" in textos[0]  # só o resultado


def test_desligado_continua_pedindo_aprovacao(api, telegram, site):
    config = _config()
    config["proposal"]["envio_automatico"]["ativo"] = False
    api.run_cycle(config)
    assert site["enviados"] == []
    assert storage.get_application(PID)["status"] == "pending_approval"
    assert any("Nova proposta pra aprovar" in t for t in _textos(telegram))


def test_nao_promove_sem_pedir(api, site):
    site["prepare"] = lambda project: (proposal_99(promovida=True), "ok")
    api.run_cycle(_config())
    assert not storage.get_application(PID).get("promovida")


def test_erro_no_envio_fica_pra_tentar_de_novo(api, telegram, site, monkeypatch):
    def finalize(page, project, proposal, dry_run=False):
        views.notify_proposal_result(project, proposal, "failed", "clique expirou")
        return False, "clique expirou"

    monkeypatch.setattr(submitter, "finalize_submission", finalize)
    api.run_cycle(_config())
    assert storage.get_application(PID)["status"] == "failed"
    assert approvals.get_pending(PID)["decision"] == "failed"  # "🔄 Tentar de novo" reenvia a mesma
    falha = telegram.last("sendMessage")
    assert "Falha ao enviar proposta" in falha["text"]
    assert falha["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"retry:{PID}"


def test_excecao_no_envio_nao_derruba_varredura(api, site, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("timeout de rede")

    monkeypatch.setattr(submitter, "finalize_submission", explode)
    api.run_cycle(_config())
    assert approvals.get_pending(PID)["decision"] == "approved"  # o próximo tick tenta de novo


@pytest.mark.parametrize("proposal,motivo", [
    (proposal_99(texto_ia_falhou=True, texto_variante="template"), "a IA não gerou o texto"),
    (proposal_99(oferta=5000.0), "acima do máximo"),
    (proposal_99(oferta=None), "sem valor/prazo"),
])
def test_bloqueado_vira_falha_sem_enviar(api, telegram, site, proposal, motivo):
    site["prepare"] = lambda project: (proposal, "ok")
    api.run_cycle(_config(oferta_maxima=3000))
    assert site["enviados"] == []
    rec = storage.get_application(PID)
    assert rec["status"] == "failed" and motivo in rec["detail"]
    falha = telegram.last("sendMessage")
    assert "Falha ao enviar proposta" in falha["text"] and motivo in falha["text"]


def test_limite_diario_para_a_varredura(api, site):
    site["projects"] = [project_99(id=str(i), title=f"P{i}") for i in range(12)]
    api.run_cycle(_config())
    # 240 / 30 dias (data congelada em setembro) = 8 por dia
    assert len(site["enviados"]) == 8
    assert not storage.already_applied("8")


def test_descricao_completa_com_ecommerce_e_ignorada(api, telegram, site):
    site["prepare"] = lambda project: (None, f"{submitter.EXCLUIDO_PELO_FILTRO}: 'loja virtual'")
    api.run_cycle(_config())
    rec = storage.get_application(PID)
    assert rec["status"] == "skipped_duplicate"
    assert site["enviados"] == []
    assert not any("Falha" in t for t in _textos(telegram))


def test_ecommerce_na_listagem_nem_abre_o_projeto(api, site):
    site["projects"] = [project_99(description="Preciso de um E-commerce de roupas")]
    site["prepare"] = lambda project: pytest.fail("não devia preparar")
    api.run_cycle(_config())
    assert storage.get_application(PID)["status"] == "skipped_duplicate"


def test_excluded_keyword():
    config = {"keywords_exclude": ["Loja Virtual"]}
    assert excluded_keyword("Quero uma LOJA VIRTUAL", config) == "loja virtual"
    assert excluded_keyword("Site institucional", config) is None
    assert excluded_keyword("x", {}) is None


# --- erros continuam chegando com o log de atividade desligado -------------------------------


def test_erro_encaminhado_mesmo_sem_log_de_atividade(telegram, monkeypatch):
    import logging

    from bot.notifier import TelegramErrorHandler

    monkeypatch.setenv("ACTIVITY_LOG_TELEGRAM", "false")
    TelegramErrorHandler().emit(logging.LogRecord("bot.x", logging.ERROR, "", 0, "deu ruim", None, None))
    assert "deu ruim" in telegram.last("sendMessage")["text"]


# --- preço pela IA ---------------------------------------------------------------------------


@pytest.fixture
def ia_preco(monkeypatch):
    chamadas = []

    def suggest(project, full_description, config, lowest_bid=None, media_prazo=None):
        chamadas.append((lowest_bid, media_prazo))
        return chamadas_resposta[0]

    chamadas_resposta = [(800.0, 6)]
    monkeypatch.setattr(ai_writer, "suggest_price_and_deadline", suggest)
    monkeypatch.setattr(ai_writer, "generate_proposal_text", lambda *a, **k: "Olá! Posso ajudar.")
    return chamadas, chamadas_resposta


def test_ia_elabora_sem_media(ia_preco):
    chamadas, _ = ia_preco
    p = proposal_mod.build_proposal(project_99(), _config(), full_description="desc")
    assert (p["oferta"], p["prazo_dias"], p["origem_valor"]) == (800.0, 6, "ia")
    assert chamadas == [(None, None)]


def test_ia_recebe_media_e_fica_abaixo_dela(ia_preco):
    chamadas, resposta = ia_preco
    resposta[0] = (1500.0, 12)
    p = proposal_mod.build_proposal(project_99(), _config(), lowest_bid=1234.5, full_description="desc", media_prazo=10)
    assert chamadas == [(1234.5, 10)]
    assert p["oferta"] == 1200.0  # teto: média arredondada pra baixo
    assert p["prazo_dias"] == 8  # teto: 20% abaixo da média de prazo
    assert p["origem_valor"] == "ia_com_media"
    assert p["oferta_sugerida_ia"] == 1500.0


def test_ia_abaixo_da_media_mantem_valor_da_ia(ia_preco):
    p = proposal_mod.build_proposal(project_99(), _config(), lowest_bid=1234.5, full_description="desc", media_prazo=10)
    assert (p["oferta"], p["prazo_dias"]) == (800.0, 6)
    assert "oferta_sugerida_ia" not in p


def test_ia_falhou_sem_proposta(ia_preco):
    _, resposta = ia_preco
    resposta[0] = None
    assert proposal_mod.build_proposal(project_99(), _config(), full_description="desc") is None


def test_prompt_de_preco_leva_a_media():
    prompt = ai_writer._build_price_user_prompt({"title": "Site"}, "desc", 1234.5, 10)
    assert "R$ 1234.50" in prompt and "10 dias" in prompt
    assert "Sem orçamento" in ai_writer._build_price_user_prompt({"title": "Site"}, "desc")
