"""
Fonte Workana: leitura do HTML (mesma estrutura do site real, logado, 2026-10-23), regra do
valor, e o fluxo inteiro com uma Page falsa — varredura → aprovação no Telegram → envio.
"""
import pytest

from bot import ai_writer, approvals
from bot.sources import registry
from bot.sources.workana import auth, client, source as wk_source
from bot.sources.workana.source import build_offer
from bot.telegram_dispatcher import TelegramDispatcher
from tests.conftest import CHAT_ID  # noqa: F401  (garante o fixture de ambiente carregado)

WORKANA = registry.source_of({"source": "workana"})

SLUG_APP = "desenvolvimento-de-aplicativo-de-agendamento"
SLUG_LOJA = "criacao-de-loja-virtual-completa"
SLUG_HORA = "desenvolvedor-full-stack-por-hora"


def _card(slug, titulo, budget, idade="há 1 hora", bids=5, desc="Preciso de um app de agendamento."):
    return f"""
    <div class="project-item js-project"><div class="project-header">
      <h2 class="h3 project-title"><span><a href="/job/{slug}"><span title="{titulo}">{titulo[:20]}...</span></a></span></h2></div>
      <div class="project-body"><div class="project-main-details hidden-xs small">
        <span class="date">Publicado: {idade}</span> <span class="bids">Propostas: {bids}</span></div>
        <div class="html-desc project-details"><div><p><span>{desc}</span> <a href="#">Ver mais detalhes</a></p></div></div>
        <div class="skills"><div><a class="skill"><h3>Flutter</h3></a><a class="skill"><h3>Node.js</h3></a></div></div></div>
      <div class="project-actions floating"><p class="budget h4"><span class="values"><span>{budget}</span></span></p></div>
    </div>"""


LISTING = (
    '<html><head><title>TI e Programação | Workana</title></head><body>'
    '<a href="https://www.workana.com/logout" class="submit-link">Sair</a><div id="projects">'
    + _card(SLUG_APP, "Desenvolvimento de Aplicativo de Agendamento", "R$ 2.500 - 5.000")
    + _card(SLUG_LOJA, "Criação de Loja Virtual Completa", "R$ 500 - 1.300", desc="Uma loja virtual com carrinho.")
    + _card(SLUG_HORA, "Desenvolvedor Full Stack por hora", "Menos de R$ 80 / hora")
    + "</div></body></html>"
)

PROJECT_PAGE = f"""<html><head><title>Projeto - Workana</title></head><body>
<a href="https://www.workana.com/logout">Sair</a>
<section class="block-detail"><h4 class="budget text-right"> R$ 2.500 - 5.000 </h4>
<div class="expander">Busca-se um desenvolvedor para um app de agendamento.<br> <br>- Login simples<br>- Agenda</div></section>
<a href="/messages/bid/{SLUG_APP}" id="bid_button">Fazer uma proposta</a></body></html>"""

BID_FORM = """<html><head><title>WORKANA</title></head><body>
<a href="https://www.workana.com/logout">Sair</a><form>
<input id="Amount" type="number" name="bid[amount]" min="1500" required>
<textarea name="bid[content]" id="BidContent"></textarea>
<input id="BidDeliveryTime" name="bid[deliveryTime]" type="text">
<input name="acknowledged" type="checkbox" checked value="1">
<input type="submit" class="btn btn-primary" value="Enviar orçamento"></form></body></html>"""


class _Checkbox:
    def is_checked(self):
        return True


class FakePage:
    """Page falsa: cada URL devolve um HTML; o clique em enviar leva pra `url_apos_enviar`."""

    def __init__(self):
        self.url = ""
        self.paginas = {
            auth.LISTING_URL: LISTING,
            client.PROJECT_URL.format(slug=SLUG_APP): PROJECT_PAGE,
            client.BID_URL.format(slug=SLUG_APP): BID_FORM,
        }
        self.preenchidos, self.cliques = {}, []
        self.url_apos_enviar = None
        self.logado = True

    def goto(self, url, **kw):
        self.url = url

    def content(self):
        return self.paginas.get(self.url, "<html></html>")

    def title(self):
        return "Workana"

    def wait_for_load_state(self, *a, **kw):
        pass

    def wait_for_selector(self, *a, **kw):
        pass

    def query_selector(self, sel):
        if sel == auth.LOGGED_IN_MARKER:
            return object() if self.logado else None
        if sel == client.BID_ACKNOWLEDGED:
            return _Checkbox()
        return None

    def fill(self, sel, valor):
        self.preenchidos[sel] = valor

    def click(self, sel):
        self.cliques.append(sel)
        if self.url_apos_enviar:
            self.url = self.url_apos_enviar


CONFIG = {
    "workana": {"enabled": True},
    "max_project_age_minutes": 600,
    "keywords_exclude": ["loja virtual"],
    "categories": ["Desenvolvimento Web"],  # nome do 99Freelas — NÃO vale pro Workana
    "proposal": {"texto_modo": "ia", "texto_variantes": {"estruturado": 1}},
}


@pytest.fixture
def workana(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(WORKANA, "page", page)
    monkeypatch.setattr(WORKANA, "_sem_sessao", False)
    monkeypatch.setattr(ai_writer, "generate_proposal_text", lambda *a, **kw: "Olá! Posso fazer o seu app de agendamento.")
    monkeypatch.setattr(ai_writer, "suggest_price_and_deadline", lambda *a, **kw: (3200.0, 20))
    return page


# --- leitura do HTML ----------------------------------------------------------------------------


def test_listagem():
    projetos = {p["slug"]: p for p in client.parse_listing(LISTING)}
    app = projetos[SLUG_APP]
    assert app["id"] == f"wk-{SLUG_APP}"
    assert app["title"] == "Desenvolvimento de Aplicativo de Agendamento"  # título completo, não o truncado
    assert app["url"] == f"https://www.workana.com/job/{SLUG_APP}"
    assert (app["budget"], app["posted_minutes_ago"], app["bids"]) == (2500.0, 60, 5)
    assert "Ver mais detalhes" not in app["description"]
    assert app["skills"] == ["Flutter", "Node.js"]
    assert projetos[SLUG_HORA]["por_hora"] is True


@pytest.mark.parametrize("texto, esperado", [
    ("R$ 2.500 - 5.000", (2500, 5000, "R$", False)),
    ("Menos de R$ 250", (None, 250, "R$", False)),
    ("Mais de R$ 50.000", (50000, None, "R$", False)),
    ("Menos de R$ 80 / hora", (None, 80, "R$", True)),
    ("USD 1.000 - 3.000", (1000, 3000, "USD", False)),
    ("", (None, None, "", False)),
])
def test_orcamento(texto, esperado):
    b = client.parse_budget(texto)
    assert (b["min"], b["max"], b["moeda"], b["por_hora"]) == esperado


@pytest.mark.parametrize("texto, minutos", [
    ("Publicado: há 20 horas", 1200), ("Há quase uma hora", 60), ("há 2 dias", 2880), ("algo estranho", None),
])
def test_idade(texto, minutos):
    assert client.parse_age_minutes(texto) == minutos


def test_id_cabe_no_callback_data():
    longo = "desenvolvimento-de-aplicativo-de-correspondencia-digital-com-foco-em-privacidade"
    pid = client.project_id(longo)
    assert pid.startswith("wk-") and ":" not in pid
    assert len(f"editf:o:{pid}".encode()) <= 64
    assert pid == client.project_id(longo)  # estável
    assert WORKANA.owns_id(pid) and not WORKANA.owns_id("785400")


def test_pagina_do_projeto_e_formulario():
    info = client.parse_project_page(PROJECT_PAGE)
    assert info["pode_propor"] and info["budget_text"] == "R$ 2.500 - 5.000"
    assert "- Login simples\n" in info["full_description"]
    assert client.parse_bid_form(BID_FORM) == {"tem_formulario": True, "lance_minimo": 1500.0}
    assert client.parse_bid_form("<html></html>")["tem_formulario"] is False
    assert (client.format_amount(1500.0), client.format_amount(1234.5)) == ("1500", "1234.50")


# --- regra do valor ------------------------------------------------------------------------------


@pytest.mark.parametrize("budget, minimo, ia, modo, esperado", [
    ({"min": 2500, "max": 5000}, 1500, 3200, "minimo_faixa", (2500, "minimo_faixa")),
    ({"min": 2500, "max": 5000}, 1500, 3200, "ia", (3200, "ia")),
    ({"min": 2500, "max": 5000}, 1500, 9000, "ia", (5000, "ia")),       # IA presa no teto
    ({"min": 2500, "max": 5000}, 1500, 900, "ia", (2500, "ia")),        # e no piso
    ({"min": None, "max": 250}, None, 400, "minimo_faixa", (250, "ia_sem_faixa")),
    ({"min": 500, "max": 1300}, 800, None, "minimo_faixa", (800, "lance_minimo")),  # nunca abaixo do lance mínimo
    ({"min": None, "max": None}, None, None, "minimo_faixa", (None, "")),
])
def test_oferta(budget, minimo, ia, modo, esperado):
    assert build_offer(budget, minimo, ia, modo) == esperado


def test_filtro_nao_herda_categorias_do_99freelas():
    filtro = wk_source.filter_config({**CONFIG, "workana": {"filtro": {"max_project_age_minutes": 30}}})
    assert "categories" not in filtro
    assert filtro["max_project_age_minutes"] == 30 and filtro["keywords_exclude"] == ["loja virtual"]


# --- fluxo completo ------------------------------------------------------------------------------


def test_varredura_pede_aprovacao_e_envia_depois_do_clique(telegram, workana, snapshot):
    WORKANA.run_cycle(CONFIG)

    pid = f"wk-{SLUG_APP}"
    entry = approvals.get_pending(pid)
    assert entry["proposal"]["oferta"] == 2500.0 and entry["proposal"]["prazo_dias"] == 20
    assert client.get_record(pid)["status"] == "pending_approval"
    assert client.get_record(f"wk-{SLUG_LOJA}")["status"] == "skipped"   # keywords_exclude
    assert client.get_record(f"wk-{SLUG_HORA}")["detail"] == "projeto por hora"
    pedido = telegram.last("sendMessage")
    snapshot("workana_aprovacao", {k: v for k, v in pedido.items() if k != "chat_id"})
    assert workana.cliques == []  # nada enviado antes do clique

    # 2º ciclo: nada novo, nada repetido
    enviados = len(telegram.of("sendMessage"))
    WORKANA.run_cycle(CONFIG)
    assert len(telegram.of("sendMessage")) == enviados

    workana.url_apos_enviar = f"https://www.workana.com/job/{SLUG_APP}"
    telegram.push_callback(f"approve:{pid}", message_id=entry["telegram_message_id"])
    TelegramDispatcher(registry.ALL_SOURCES, CONFIG).poll()
    from bot import main
    main.process_pending_approvals()

    assert workana.preenchidos == {
        client.BID_AMOUNT: "2500",
        client.BID_CONTENT: entry["proposal"]["texto"],
        client.BID_DELIVERY: "20 dias",
    }
    assert workana.cliques == [client.BID_SUBMIT]
    assert client.get_record(pid)["status"] == "sent"
    assert approvals.get_pending(pid) is None
    assert "✅ Proposta enviada" in telegram.last("sendMessage")["text"]


def test_recusar_nao_envia(telegram, workana):
    WORKANA.run_cycle(CONFIG)
    pid = f"wk-{SLUG_APP}"
    approvals.record_decision(pid, "rejected")
    from bot import main
    main.process_pending_approvals()
    assert workana.cliques == []
    assert client.get_record(pid)["status"] == "rejected_by_user"


def test_continuou_no_formulario_vira_falha_com_tentar_de_novo(telegram, workana):
    WORKANA.run_cycle(CONFIG)
    pid = f"wk-{SLUG_APP}"
    approvals.record_decision(pid, "approved")
    from bot import main
    main.process_pending_approvals()  # url_apos_enviar None: o site não saiu do formulário
    assert client.get_record(pid)["status"] == "failed"
    assert approvals.get_pending(pid)["decision"] == "failed"  # guardada pro "🔄 Tentar de novo"
    falha = telegram.last("sendMessage")
    assert "Falha ao enviar" in falha["text"]
    assert falha["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"retry:{pid}"


def test_oferta_editada_abaixo_do_lance_minimo_nao_clica(telegram, workana):
    WORKANA.run_cycle(CONFIG)
    pid = f"wk-{SLUG_APP}"
    entry = approvals.get_pending(pid)
    entry["proposal"]["oferta"] = 900.0
    approvals.update_proposal(pid, entry["proposal"])
    approvals.record_decision(pid, "approved")
    from bot import main
    main.process_pending_approvals()
    assert workana.cliques == []
    assert "lance mínimo" in client.get_record(pid)["detail"]


def test_sem_sessao_avisa_uma_vez_e_nao_varre(telegram, workana):
    workana.logado = False
    WORKANA.run_cycle(CONFIG)
    WORKANA.run_cycle(CONFIG)
    textos = [p["text"] for p in telegram.of("sendMessage")]
    assert len(textos) == 1 and "Sem sessão no Workana" in textos[0]
    assert approvals.get_pending(f"wk-{SLUG_APP}") is None
    ok, detail = WORKANA.deliver({"project": {"slug": SLUG_APP}, "proposal": {"oferta": 1, "prazo_dias": 1}})
    assert ok is False and "sessão" in detail


def test_desligado_por_padrao():
    assert not WORKANA.is_enabled({})
    assert WORKANA.is_enabled({"workana": {"enabled": True}})
