"""
Contrato das fontes de vagas: registro, escolha da fonte por item, e — com uma fonte
fictícia — que uma fonte nova se pluga no núcleo (dispatcher, aprovação, resolução) sem
nenhuma mudança fora dela. Se este arquivo precisar de if por fonte, o design regrediu.
"""
import pytest

from bot import approvals, main, notifier
from bot.sources import registry
from bot.sources.base import CallbackRoute, EditableField, JobSource
from bot.telegram_dispatcher import TelegramDispatcher
from tests.conftest import FREELAS99, GITHUB


def test_toda_fonte_registrada_e_completa():
    nomes = [s.name for s in registry.ALL_SOURCES]
    assert nomes == ["99freelas", "github"]
    for source in registry.ALL_SOURCES:
        assert source.tag.startswith("<b>[") and source.tag.endswith("]</b>")
        codes = [f.code for f in source.editable_fields]
        assert len(codes) == len(set(codes)) and all(len(c) == 1 for c in codes)


def test_source_of():
    assert registry.source_of({"source": "github"}) is GITHUB
    assert registry.source_of({"source": "99freelas"}) is FREELAS99
    assert registry.source_of({}) is FREELAS99  # entradas antigas, sem "source"
    assert registry.source_of({"source": "desconhecida"}) is FREELAS99


def test_source_for_id():
    assert registry.source_for_id("gh-frontendbr/vagas#1") is GITHUB
    assert registry.source_for_id("785400") is FREELAS99
    assert registry.source_for_id("qualquer-coisa") is FREELAS99


def test_enabled_sources():
    assert registry.enabled_sources({}) == [FREELAS99]
    assert registry.enabled_sources({"github_jobs": {"enabled": False}}) == [FREELAS99]
    assert registry.enabled_sources({"github_jobs": {"enabled": True}}) == [FREELAS99, GITHUB]


def test_prefixo_de_callback_duplicado_quebra_na_inicializacao():
    class Invasora(_FonteFake):
        def callback_routes(self):
            return {"approve": CallbackRoute(1, lambda cb: None)}

    with pytest.raises(ValueError, match="approve"):
        TelegramDispatcher([Invasora()], {})


def test_nao_da_pra_instanciar_fonte_incompleta():
    class Incompleta(JobSource):
        name, tag = "x", "<b>[X]</b>"

    with pytest.raises(TypeError):
        Incompleta()


# --- Uma fonte nova, fictícia, plugada no núcleo ----------------------------------------------


class _FonteFake(JobSource):
    name = "fake"
    tag = "<b>[Fake]</b>"
    editable_fields = (
        EditableField(
            code="v", button="✏️ Editar valor", prompt="Novo valor:", invalid_msg="Valor inválido.",
            parse=lambda raw: raw.strip() or None,
            apply=lambda proposal, valor: proposal.update(valor=valor) or f"Valor: {valor}",
        ),
    )

    def __init__(self):
        self.entregues, self.rejeitadas, self.cliques = [], [], []

    def owns_id(self, project_id):
        return project_id.startswith("fk-")

    def run_cycle(self, config):
        self.queue_for_approval({"id": "fk-1", "title": "Item fake"}, {"valor": "a"})

    def render_approval(self, project, proposal):
        keyboard = {"inline_keyboard": [
            self.edit_buttons(project["id"]),
            [{"text": "✅", "callback_data": f"approve:{project['id']}"}],
        ]}
        return f"{self.tag} {project['title']}: {proposal['valor']}", keyboard

    def deliver(self, entry):
        return True, "entregue"

    def on_delivered(self, entry, success, detail):
        self.entregues.append((entry["project_id"], entry["proposal"]["valor"], success))

    def on_rejected(self, entry):
        self.rejeitadas.append(entry["project_id"])

    def callback_routes(self):
        return {"fk": CallbackRoute(1, lambda cb: self.cliques.append(cb.args))}


@pytest.fixture
def fake_source(monkeypatch):
    fake = _FonteFake()
    monkeypatch.setattr(registry, "ALL_SOURCES", registry.ALL_SOURCES + [fake])
    monkeypatch.setattr(registry, "_BY_NAME", {**registry._BY_NAME, fake.name: fake})
    return fake


def test_fonte_nova_de_ponta_a_ponta(telegram, fake_source):
    dispatcher = TelegramDispatcher(registry.ALL_SOURCES, {})

    fake_source.run_cycle({})
    entry = approvals.get_pending("fk-1")
    assert entry["project"]["source"] == "fake"
    assert telegram.last("sendMessage")["text"] == "<b>[Fake]</b> Item fake: a"

    # edição genérica, com o campo declarado pela fonte
    telegram.push_callback("editf:v:fk-1", message_id=entry["telegram_message_id"])
    dispatcher.poll()
    prompt = telegram.last("sendMessage")
    assert prompt["text"].startswith("<b>[Fake]</b> Novo valor:")
    telegram.push_message("b", reply_to=1000 + len(telegram.of("sendMessage")))
    dispatcher.poll()
    assert approvals.get_pending("fk-1")["proposal"]["valor"] == "b"
    assert telegram.last("editMessageText")["text"] == "<b>[Fake]</b> Item fake: b"
    assert telegram.last("sendMessage")["text"] == "<b>[Fake]</b> Valor: b"

    # botão próprio da fonte
    telegram.push_callback("fk:123")
    dispatcher.poll()
    assert fake_source.cliques == [("123",)]

    # aprovação → resolução pela própria fonte
    telegram.push_callback("approve:fk-1")
    dispatcher.poll()
    main.process_pending_approvals()
    assert fake_source.entregues == [("fk-1", "b", True)]
    assert approvals.get_pending("fk-1") is None


def test_fonte_nova_rejeitada(telegram, fake_source):
    fake_source.run_cycle({})
    approvals.record_decision("fk-1", "rejected")
    main.process_pending_approvals()
    assert fake_source.rejeitadas == ["fk-1"]
    assert fake_source.entregues == []
    assert approvals.get_pending("fk-1") is None


def test_retry_sem_preparo_usa_resposta_padrao(telegram, fake_source):
    telegram.push_callback("retry:fk-9")
    TelegramDispatcher(registry.ALL_SOURCES, {}).poll()
    assert telegram.last("answerCallbackQuery")["text"].startswith("Nada pra tentar de novo")


# --- Guarda de ciclo por fonte -----------------------------------------------------------------


class _Instavel(_FonteFake):
    def __init__(self, falhas):
        super().__init__()
        self.falhas = falhas

    def run_cycle(self, config):
        if self.falhas:
            self.falhas -= 1
            raise RuntimeError("API <fora>")


def test_cycle_guard_notifica_so_no_inicio_e_na_recuperacao(telegram):
    guard = main._CycleGuard()
    fonte = _Instavel(falhas=3)
    for _ in range(5):
        guard.run(fonte, {})
    textos = [p["text"] for p in telegram.of("sendMessage")]
    assert len(textos) == 2
    assert "Problema num ciclo" in textos[0] and "<b>[Fake]</b> API &lt;fora&gt;" in textos[0]
    assert "Ciclo voltou ao normal" in textos[1] and "após 3 ciclo(s)" in textos[1]


def test_cycle_guard_conta_por_fonte(telegram):
    guard = main._CycleGuard()
    a, b = _Instavel(falhas=1), _Instavel(falhas=1)
    b.name = "fake2"
    guard.run(a, {})
    guard.run(b, {})  # falha de outra fonte também notifica (contador separado)
    assert len(telegram.of("sendMessage")) == 2


def test_notify_activity_com_e_sem_tag(telegram):
    notifier.notify_activity("oi", tag="<b>[X]</b>")
    notifier.notify_activity("sem tag")
    assert [p["text"] for p in telegram.of("sendMessage")] == ["<b>[X]</b> oi", "sem tag"]
