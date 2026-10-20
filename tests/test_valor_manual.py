"""
proposal.valor_modo "manual" (2026-10-02): o projeto chega no Telegram sem valor/prazo e o
usuário responde (reply) à mensagem com "valor prazo" — isso já aprova e o próximo tick envia.
"""
import pytest

from bot import ai_writer, approvals, proposal as proposal_mod, storage, submitter
from bot.sources.freelas99.source import _parse_valor_prazo
from tests.conftest import project_99, proposal_99

CONFIG = {"proposal": {"ia_provider": "gemini", "ia_model": "x"}}
PID = "785400"
MSG_ID = 555


def _manual(**over) -> dict:
    return proposal_99(origem_valor="manual", oferta=None, prazo_dias=None, **over)


@pytest.fixture
def pending_manual():
    approvals.add_pending({**project_99(), "source": "99freelas"}, _manual(), MSG_ID)


@pytest.fixture
def envios(api):
    enviados = []

    def finalize(page, project, proposal, dry_run=False):
        enviados.append((project["id"], proposal))
        return True, "proposta enviada"

    api.patch_submitter("finalize_submission", finalize)
    return enviados


# --- parser ---------------------------------------------------------------------------------


@pytest.mark.parametrize("texto,esperado", [
    ("1200 10", (1200.0, 10)),
    ("1200, 10", (1200.0, 10)),
    ("R$ 1.200,50 10 dias", (1200.5, 10)),
    ("1200 / 10d", (1200.0, 10)),
    ("150,00 3", (150.0, 3)),
])
def test_parse_valor_prazo(texto, esperado):
    assert _parse_valor_prazo(texto) == esperado


@pytest.mark.parametrize("texto", ["trezentos", "1200", "0 5", "1200 0", "", "dez dias", "99,99 5", "80 3"])
def test_parse_valor_prazo_invalido(texto):
    assert _parse_valor_prazo(texto) is None


# --- reply no Telegram ------------------------------------------------------------------------


def test_reply_com_valor_aprova_e_envia(api, telegram, pending_manual, envios):
    telegram.push_message("R$ 1.500 7 dias", reply_to=MSG_ID)
    api.poll(CONFIG)

    entry = approvals.get_pending(PID)
    assert entry["decision"] == "approved"
    assert (entry["proposal"]["oferta"], entry["proposal"]["prazo_dias"]) == (1500.0, 7)
    editado = telegram.last("editMessageText")
    assert editado["message_id"] == MSG_ID
    assert "<b>Oferta:</b> R$ 1500,00" in editado["text"]
    assert "Definido por você" in editado["text"]
    assert editado["reply_markup"]["inline_keyboard"][0][0]["text"] == "⏳ Processando..."

    api.process_approvals(CONFIG)
    assert [(pid, p["oferta"], p["prazo_dias"]) for pid, p in envios] == [(PID, 1500.0, 7)]
    rec = storage.get_application(PID)
    assert (rec["status"], rec["oferta"], rec["prazo_dias"], rec["origem_valor"]) == ("sent", 1500.0, 7, "manual")


def test_reply_invalido_avisa_e_nao_muda_nada(api, telegram, pending_manual):
    telegram.push_message("trezentos", reply_to=MSG_ID)
    api.poll(CONFIG)
    entry = approvals.get_pending(PID)
    assert entry["decision"] is None
    assert entry["proposal"]["oferta"] is None
    assert "Não entendi" in telegram.last("sendMessage")["text"]
    assert not telegram.of("editMessageText")


def test_reply_depois_de_rejeitada(api, telegram, pending_manual):
    approvals.record_decision(PID, "rejected")
    telegram.push_message("1200 10", reply_to=MSG_ID)
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["proposal"]["oferta"] is None
    assert "já foi decidida" in telegram.last("sendMessage")["text"]


def test_reply_de_outro_chat_e_ignorado(api, telegram, pending_manual):
    telegram.push_message("1200 10", reply_to=MSG_ID, chat_id="999")
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["decision"] is None


def test_reply_a_proposta_automatica_nao_aprova(api, telegram):
    approvals.add_pending(project_99(), proposal_99(), MSG_ID)
    telegram.push_message("1200 10", reply_to=MSG_ID)
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["decision"] is None


def test_teclado_sem_aprovar_nem_editar(api):
    _, keyboard = api.render_approval(project_99(), _manual())
    callbacks = [b["callback_data"] for linha in keyboard["inline_keyboard"] for b in linha]
    assert callbacks == [f"promo:{PID}", f"reject:{PID}"]


# --- montagem e envio -------------------------------------------------------------------------


def test_build_proposal_manual_nao_chama_ia_de_preco(monkeypatch):
    def nao_chamar(*a, **k):
        raise AssertionError("IA de preço não deveria ser chamada no modo manual")

    monkeypatch.setattr(ai_writer, "suggest_price_and_deadline", nao_chamar)
    monkeypatch.setattr(ai_writer, "generate_proposal_text", lambda *a, **k: "Olá! Posso ajudar com esse projeto.")
    config = {"proposal": {"oferta_estrategia": "menor_proposta", "texto_modo": "ia"}}
    p = proposal_mod.build_proposal(project_99(), config, lowest_bid=None, full_description="Descrição", media_prazo=10)
    assert (p["oferta"], p["prazo_dias"], p["origem_valor"]) == (None, None, "manual")


def test_finalize_sem_valor_nunca_envia(telegram):
    # page=None: se tentasse navegar, quebraria — a trava vem antes de tocar o Playwright.
    ok, detail = submitter.finalize_submission(None, project_99(), _manual())
    assert not ok
    assert "valor/prazo não informados" in detail


# --- texto estruturado (cumprimento -> solucao -> proposta) -------------------------------


def test_sorteio_so_com_estruturado():
    cfg = {"texto_variantes": {"estruturado": 1, "padrao": 0, "pergunta": 0}}
    assert {proposal_mod._sortear_variante_texto(cfg) for _ in range(30)} == {"estruturado"}


def test_texto_estruturado_usa_prompt_proprio(monkeypatch):
    prompts = []

    def fake_gemini(user_prompt, model, system_prompt=None):
        prompts.append(system_prompt)
        return "Olá, tudo bem?\n\nEntendi o que você precisa.\n\nPosso começar logo."

    monkeypatch.setitem(ai_writer._PROVIDERS, "gemini", (fake_gemini, "x"))
    texto = ai_writer.generate_proposal_text(project_99(), "Descrição", CONFIG, variante="estruturado")
    assert texto.startswith("Olá, tudo bem?")
    assert prompts == [ai_writer._text_system_prompt("estruturado")]
    assert prompts[0].startswith(ai_writer._SYSTEM_PROMPT_BASE)
    assert "EXATAMENTE 3 partes" in prompts[0]


def test_valor_solto_sem_reply_explica(api, telegram, pending_manual):
    telegram.push_message("300 5")
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["decision"] is None
    assert "responda (reply)" in telegram.last("sendMessage")["text"]


def test_valor_solto_sem_proposta_aguardando_e_ignorado(api, telegram):
    telegram.push_message("300 5")
    api.poll(CONFIG)
    assert not telegram.of("sendMessage")


# --- valor mínimo (R$ 100,00) -------------------------------------------------------------------


@pytest.mark.parametrize("media,esperado", [(85.0, 100.0), (60.0, 100.0), (130.0, 100.0), (250.0, 200.0)])
def test_oferta_calculada_nunca_fica_abaixo_do_minimo(media, esperado):
    config = {"proposal": {"valor_modo": "automatico", "oferta_estrategia": "menor_proposta", "texto_modo": "fixo"}}
    resultado = proposal_mod.build_proposal(project_99(), config, lowest_bid=media)
    assert resultado["oferta"] == esperado


def test_finalize_nunca_envia_abaixo_do_minimo(api):
    class PaginaQueNaoPodeSerUsada:
        def __getattr__(self, nome):
            raise AssertionError(f"não devia tocar na página ({nome})")

    ok, detalhe = submitter.finalize_submission(
        PaginaQueNaoPodeSerUsada(), project_99(), proposal_99(oferta=99.0, prazo_dias=5)
    )
    assert not ok
    assert "abaixo do mínimo de R$ 100,00" in detalhe
