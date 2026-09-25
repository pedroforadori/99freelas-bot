"""
Ponta a ponta do polling do Telegram (cliques e respostas de texto) com updates falsos.
Confere o estado gravado (approvals, manual_queue, storage) e, via snapshot, exatamente o
que foi mandado de volta pro Telegram.
"""
import pytest

from bot import approvals, manual_queue, storage
from tests.conftest import github_email, github_job, project_99, proposal_99

CONFIG = {"proposal": {"ia_provider": "gemini", "ia_model": "x"}}
PID = "785400"
GH_ID = "gh-frontendbr/vagas#123"
LINK = "https://www.99freelas.com.br/project/site-institucional-785400"


def _calls(telegram) -> list:
    """Tudo que o bot mandou pro Telegram, exceto o próprio getUpdates, sem o chat_id."""
    return [
        [m, {k: v for k, v in p.items() if k != "chat_id"}]
        for m, p in telegram.calls
        if m != "getUpdates"
    ]


@pytest.fixture
def pending_99():
    approvals.add_pending(project_99(), proposal_99(), 555)


@pytest.fixture
def pending_gh():
    approvals.add_pending(github_job(), github_email(), 556)


# --- Aprovar / rejeitar -------------------------------------------------------------------


@pytest.mark.parametrize("acao,decisao", [("approve", "approved"), ("reject", "rejected")])
def test_decisao_gravada(api, telegram, snapshot, pending_99, acao, decisao):
    telegram.push_callback(f"{acao}:{PID}", message_id=555)
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["decision"] == decisao
    snapshot(f"flow_decisao_{acao}", _calls(telegram))


def test_decisao_nao_reprocessa_update(api, telegram, pending_99):
    telegram.push_callback(f"approve:{PID}")
    api.poll(CONFIG)
    telegram.clear()
    api.poll(CONFIG)  # offset persistido: o mesmo update não volta
    assert telegram.methods() == ["getUpdates"]
    assert telegram.last("getUpdates")["offset"] == 2


def test_decisao_id_desconhecido(api, telegram):
    telegram.push_callback("approve:999")
    api.poll(CONFIG)
    assert telegram.last("answerCallbackQuery")["text"] == "Não encontrado (já processado ou expirado)"


def test_decisao_github(api, telegram, pending_gh):
    telegram.push_callback(f"approve:{GH_ID}", message_id=556)
    api.poll(CONFIG)
    assert approvals.get_pending(GH_ID)["decision"] == "approved"


@pytest.mark.parametrize("data", ["noop", "algo:estranho:mesmo:x", "desconhecido:1"])
def test_callback_sem_acao(api, telegram, data):
    telegram.push_callback(data)
    api.poll(CONFIG)
    assert _calls(telegram) == [["answerCallbackQuery", {"callback_query_id": "cb1"}]]


# --- Edição por texto livre ---------------------------------------------------------------


def _prompt_id(telegram) -> int:
    return 1000 + len(telegram.of("sendMessage"))  # message_id do último sendMessage


@pytest.mark.parametrize("field,resposta,campo,esperado", [
    ("o", "R$ 1.500,50", "oferta", 1500.5),
    ("p", "10 dias", "prazo_dias", 10),
])
def test_edicao_99(api, telegram, snapshot, pending_99, field, resposta, campo, esperado):
    telegram.push_callback(f"editf:{field}:{PID}", message_id=555)
    api.poll(CONFIG)
    prompt_id = _prompt_id(telegram)
    assert approvals.get_pending(PID)["pending_edit"] == {"field": field, "prompt_message_id": prompt_id}

    telegram.push_message(resposta, reply_to=prompt_id)
    api.poll(CONFIG)
    entry = approvals.get_pending(PID)
    assert entry["proposal"][campo] == esperado
    assert entry["proposal"]["ajustado_manualmente"] is True
    assert entry["pending_edit"] is None
    snapshot(f"flow_edicao_99_{field}", _calls(telegram))


@pytest.mark.parametrize("field,resposta", [("o", "sei lá"), ("p", "amanhã")])
def test_edicao_99_valor_invalido(api, telegram, pending_99, field, resposta):
    telegram.push_callback(f"editf:{field}:{PID}")
    api.poll(CONFIG)
    prompt_id = _prompt_id(telegram)
    telegram.push_message(resposta, reply_to=prompt_id)
    api.poll(CONFIG)
    entry = approvals.get_pending(PID)
    assert entry["proposal"] == proposal_99()
    assert entry["pending_edit"]["prompt_message_id"] == prompt_id  # pode tentar de novo
    assert telegram.last("sendMessage")["text"].startswith("Não entendi")


def test_edicao_github_destinatario(api, telegram, snapshot, pending_gh):
    telegram.push_callback(f"editf:e:{GH_ID}", message_id=556)
    api.poll(CONFIG)
    prompt_id = _prompt_id(telegram)
    telegram.push_message("rh@acme.com", reply_to=prompt_id)
    api.poll(CONFIG)
    proposal = approvals.get_pending(GH_ID)["proposal"]
    assert proposal["email_to"] == "rh@acme.com"
    assert proposal["email_editado"] is True
    assert "ajustado_manualmente" not in proposal
    snapshot("flow_edicao_github_e", _calls(telegram))


def test_edicao_github_email_invalido(api, telegram, pending_gh):
    telegram.push_callback(f"editf:e:{GH_ID}")
    api.poll(CONFIG)
    telegram.push_message("não é e-mail", reply_to=_prompt_id(telegram))
    api.poll(CONFIG)
    assert approvals.get_pending(GH_ID)["proposal"]["email_to"] == "vagas@acme.com"
    assert telegram.last("sendMessage")["text"].startswith("Não entendi o e-mail")


def test_edicao_depois_da_decisao(api, telegram, pending_99):
    approvals.record_decision(PID, "approved")
    telegram.push_callback(f"editf:o:{PID}")
    api.poll(CONFIG)
    assert telegram.last("answerCallbackQuery")["text"] == "Já decidido ou expirado — não é possível editar."
    assert not telegram.of("sendMessage")


def test_resposta_decidida_no_meio_da_edicao(api, telegram, pending_99):
    telegram.push_callback(f"editf:o:{PID}")
    api.poll(CONFIG)
    prompt_id = _prompt_id(telegram)
    approvals.record_decision(PID, "rejected")
    telegram.push_message("100", reply_to=prompt_id)
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["proposal"]["oferta"] == 1200.0
    assert "já foi decidida" in telegram.last("sendMessage")["text"]


def test_reply_a_outra_mensagem_e_ignorado(api, telegram, pending_99):
    telegram.push_message("oi", reply_to=12345)
    api.poll(CONFIG)
    assert _calls(telegram) == []


def test_campo_de_edicao_desconhecido(api, telegram, pending_99):
    telegram.push_callback(f"editf:z:{PID}")
    api.poll(CONFIG)
    assert _calls(telegram) == [["answerCallbackQuery", {"callback_query_id": "cb1"}]]


# --- Tentar de novo -----------------------------------------------------------------------


def test_retry_falha_de_envio(api, telegram, snapshot, pending_99):
    approvals.record_decision(PID, "approved")
    approvals.mark_failed(PID)
    telegram.push_callback(f"retry:{PID}", message_id=777)
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["decision"] == "approved"
    snapshot("flow_retry_envio", _calls(telegram))


def test_retry_falha_de_envio_github(api, telegram, pending_gh):
    approvals.record_decision(GH_ID, "approved")
    approvals.mark_failed(GH_ID)
    telegram.push_callback(f"retry:{GH_ID}", message_id=777)
    api.poll(CONFIG)
    assert approvals.get_pending(GH_ID)["decision"] == "approved"


def test_retry_falha_de_preparo(api, telegram, snapshot):
    telegram.push_callback(f"retry:{PID}", message_id=778, text=f"⚠️ Falha\nLink: {LINK}\nDetalhe: x")
    api.poll(CONFIG)
    assert manual_queue.peek_all()[0]["url"] == LINK
    snapshot("flow_retry_preparo", _calls(telegram))


def test_retry_preparo_sem_link(api, telegram):
    telegram.push_callback(f"retry:{PID}", text="sem link aqui")
    api.poll(CONFIG)
    assert manual_queue.peek_all() == []
    assert "Não achei o link" in telegram.last("answerCallbackQuery")["text"]


def test_retry_preparo_github_sem_link(api, telegram):
    """Vaga do GitHub sem entrada "failed": não há o que preparar de novo."""
    telegram.push_callback(f"retry:{GH_ID}", text="⚠️ Falha ao enviar e-mail")
    api.poll(CONFIG)
    assert manual_queue.peek_all() == []
    assert telegram.of("answerCallbackQuery")


def test_retry_ja_aguardando_aprovacao(api, telegram, pending_99):
    telegram.push_callback(f"retry:{PID}", text=LINK)
    api.poll(CONFIG)
    assert telegram.last("answerCallbackQuery")["text"] == "Esse projeto já está aguardando sua aprovação."


def test_retry_ja_sendo_enviado(api, telegram, pending_99):
    approvals.record_decision(PID, "approved")
    telegram.push_callback(f"retry:{PID}", text=LINK)
    api.poll(CONFIG)
    assert telegram.last("answerCallbackQuery")["text"] == "Esse projeto já está sendo enviado."


def test_retry_ja_na_fila(api, telegram):
    manual_queue.add(PID, LINK)
    telegram.push_callback(f"retry:{PID}", text=LINK)
    api.poll(CONFIG)
    assert telegram.last("answerCallbackQuery")["text"] == "Esse projeto já está na fila, aguarde."


# --- Retry do texto via IA -------------------------------------------------------------------


def test_retry_ia_sucesso(api, telegram, snapshot):
    approvals.add_pending(project_99(), proposal_99(texto_ia_falhou=True, texto_variante="template"), 555)
    chamadas = []
    api.patch_ai_text(lambda project, desc, config, variante="padrao": chamadas.append(variante) or "Texto novo da IA")
    telegram.push_callback(f"retryia:{PID}", message_id=555)
    api.poll(CONFIG)
    proposal = approvals.get_pending(PID)["proposal"]
    # Comportamento atual: repassa "template" (ai_writer cai pra "padrao" com um warning).
    assert chamadas == ["template"]
    assert proposal["texto"] == "Texto novo da IA"
    assert proposal["texto_ia_falhou"] is False
    assert proposal["texto_variante"] == "padrao"
    snapshot("flow_retry_ia_sucesso", _calls(telegram))


def test_retry_ia_mantem_estilo(api, telegram):
    approvals.add_pending(project_99(), proposal_99(texto_ia_falhou=True, texto_variante="plano"), 555)
    chamadas = []
    api.patch_ai_text(lambda project, desc, config, variante="padrao": chamadas.append(variante) or "Novo")
    telegram.push_callback(f"retryia:{PID}")
    api.poll(CONFIG)
    assert chamadas == ["plano"]
    assert approvals.get_pending(PID)["proposal"]["texto_variante"] == "plano"


def test_retry_ia_falha(api, telegram):
    approvals.add_pending(project_99(), proposal_99(texto_ia_falhou=True), 555)
    api.patch_ai_text(lambda *a, **k: None)
    telegram.push_callback(f"retryia:{PID}")
    api.poll(CONFIG)
    assert approvals.get_pending(PID)["proposal"]["texto_ia_falhou"] is True
    assert telegram.last("answerCallbackQuery")["text"].startswith("IA falhou de novo")
    assert not telegram.of("editMessageText")


def test_retry_ia_sem_descricao(api, telegram):
    approvals.add_pending(project_99(), proposal_99(texto_ia_falhou=True, full_description=None), 555)
    telegram.push_callback(f"retryia:{PID}")
    api.poll(CONFIG)
    assert "Sem descrição completa" in telegram.last("answerCallbackQuery")["text"]


# --- Link colado no chat ------------------------------------------------------------------


def test_link_colado_projeto_novo(api, telegram, snapshot):
    telegram.push_message(f"olha esse {LINK}?ref=abc")
    api.poll(CONFIG)
    snapshot("flow_link_novo", _calls(telegram))


def test_link_colado_projeto_enviado(api, telegram, snapshot):
    storage.register_application(PID, "Site institucional", status="sent", extra={"texto_variante": "plano"})
    storage.record_outcome(PID, "respondeu")
    telegram.push_message("x", entities=[{"type": "text_link", "url": LINK.replace("/project/", "/project/bid/")}])
    api.poll(CONFIG)
    snapshot("flow_link_enviado", _calls(telegram))


def test_link_de_outro_chat_e_ignorado(api, telegram):
    telegram.push_message(LINK, chat_id="1")
    api.poll(CONFIG)
    assert _calls(telegram) == []


def test_mensagem_solta_sem_link_e_ignorada(api, telegram):
    telegram.push_message("bom dia")
    api.poll(CONFIG)
    assert _calls(telegram) == []


def test_link_preparar(api, telegram, snapshot):
    telegram.push_callback(f"link:p:{PID}", message_id=880, text=f"🔗 Projeto\n{LINK}")
    api.poll(CONFIG)
    assert manual_queue.peek_all()[0]["url"] == LINK
    snapshot("flow_link_preparar", _calls(telegram))


@pytest.mark.parametrize("kind,resultado", [("r", "respondeu"), ("f", "fechou")])
def test_link_resultado(api, telegram, snapshot, kind, resultado):
    storage.register_application(PID, "Site", status="sent")
    telegram.push_callback(f"link:{kind}:{PID}", message_id=881, text=LINK)
    api.poll(CONFIG)
    assert storage.get_application(PID)["resultado"] == resultado
    snapshot(f"flow_link_resultado_{kind}", _calls(telegram))


def test_link_resultado_projeto_nao_enviado(api, telegram):
    telegram.push_callback(f"link:r:{PID}", text=LINK)
    api.poll(CONFIG)
    assert telegram.last("answerCallbackQuery")["text"] == "Esse projeto não consta como proposta enviada — nada marcado."
    assert not telegram.of("editMessageReplyMarkup")


def test_link_preparar_ja_aguardando(api, telegram, pending_99):
    telegram.push_callback(f"link:p:{PID}", text=LINK)
    api.poll(CONFIG)
    assert manual_queue.peek_all() == []
    assert telegram.last("answerCallbackQuery")["text"] == "Esse projeto já está aguardando sua aprovação."
