"""
Mensagens de clientes ↔ Telegram (bot/messages.py): leitura pela API da caixa de entrada,
notificação, reply no Telegram → fila → envio pela API, e os casos de falha. A API do site
é simulada por uma Page falsa (page.context.request), no mesmo formato url-encoded real.
"""
import json
import urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot import messages
from bot import site_selectors as sel
from tests.conftest import CHAT_ID, FREELAS99, read_json

EU = 107360
CONV = 17343568


def _encoded(body: dict) -> str:
    return urllib.parse.quote(json.dumps(body))


class _Resp:
    def __init__(self, body: dict | None, status: int = 200):
        self.status = status
        self._text = _encoded(body) if body is not None else "<html>erro</html>"

    def text(self):
        return self._text


class FakeInboxPage:
    """Page com a API de mensagens do 99Freelas simulada."""

    def __init__(self):
        self.conversas = []            # registros de carregarConversas
        self.mensagens = {}            # idConversa → mensagensDaConversa
        self.send_result = {"status": {"id": 1}, "result": True}
        self.get_fails = False
        self.gets: list[dict] = []
        self.posts: list[dict] = []
        self.gotos: list[str] = []
        self.deletes: list[str] = []
        self.context = SimpleNamespace(request=SimpleNamespace(get=self._get, post=self._post))

    def _get(self, url, headers=None):
        base, query = url.split("?data=")
        data = json.loads(urllib.parse.unquote(query))
        self.gets.append({"url": base, "data": data})
        if self.get_fails:
            return _Resp(None)
        if base == sel.MESSAGES_API_CONVERSATIONS:
            return _Resp({"status": {"id": 1}, "result": {"registros": self.conversas, "qtdTotalRegistros": len(self.conversas)}})
        if base == sel.MESSAGES_API_LIST:
            return _Resp({"status": {"id": 1}, "result": {"mensagensDaConversa": self.mensagens.get(data["idConversa"], [])}})
        raise AssertionError(f"GET inesperado: {url}")

    def _post(self, url, form=None, headers=None):
        if url.startswith(sel.MESSAGES_API_DELETE.format(id="")):
            self.deletes.append(url)
            apagada = int(url.rsplit("/", 1)[-1].split("?")[0])
            for mensagens in self.mensagens.values():
                for m in mensagens:
                    if m["id"] == apagada:
                        m["excluida"] = True
            return _Resp({"status": {"id": 1}, "result": True})
        assert url == sel.MESSAGES_API_SEND
        enviada = json.loads(form["data"])
        self.posts.append(enviada)
        if self.send_result == "auto":
            novo_id = 555000 + len(self.posts)
            self.mensagens.setdefault(enviada["idConversa"], []).append(msg(novo_id, enviada["texto"], de=EU, lida=True))
            return _Resp({"status": {"id": 1}, "result": {"id": novo_id}})
        return _Resp(self.send_result)

    # fallback do badge
    def goto(self, url, wait_until=None):
        self.gotos.append(url)

    def query_selector(self, selector):
        return None


def conversa(id_=CONV, cliente="Bruno M.", projeto="Finaliza&ccedil;&atilde;o de sistema para sal&otilde;es", **over):
    base = {
        "idConversa": id_, "idFreelancer": EU, "nomeFreelancer": "Pedro Foradori", "nomeCliente": cliente,
        "nomeProjeto": projeto, "visualizada": False, "idStatusProjeto": 2, "freelancerProjeto": False,
        "propostaRejeitada": False, "readOnly": False, "fechada": False,
    }
    return {**base, **over}


def msg(id_, texto, de=3873205, sistema=False, lida=False, arquivos=None, em=None):
    """`em`: epoch (s) de criação — o bot espera o cliente parar de escrever a partir dele."""
    return {
        "id": id_, "pessoa": {"id": de}, "sistema": sistema, "excluida": False, "texto": texto,
        "arquivos": arquivos or [], "dhVisualizacaoInMillis": 1790781802000 if lida else None,
        "dhCriacaoInMillis": int(em * 1000) if em else None,
    }


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(messages, "_api_falhando", False)
    fake = FakeInboxPage()
    monkeypatch.setattr(FREELAS99, "page", fake)
    return fake


def _sent_texts(telegram) -> list[str]:
    return [p["text"] for p in telegram.of("sendMessage")]


# --- Leitura -------------------------------------------------------------------------------


def test_notifica_mensagem_nova_do_cliente(page, telegram, snapshot):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [
        msg(10, "Enviei uma proposta de R$&nbsp;1.200,00", de=EU, sistema=True),
        msg(11, "Oi! Voc&ecirc; consegue<br/>entregar em 5 dias?"),
        msg(12, "Segue o briefing", arquivos=[{"id": 1}]),
    ]
    messages.check_and_notify(page)

    snapshot("messages_notificacao_cliente", _sent_texts(telegram))
    # Leu sem marcar como lida.
    assert all(g["data"]["visualizar"] is False for g in page.gets if g["url"] == sel.MESSAGES_API_LIST)
    thread = messages.find_thread(1001)
    assert thread == {"id": CONV, "cliente": "Bruno M.", "projeto": "Finalização de sistema para salões", "freelancer": "Pedro"}
    assert page.gotos == []  # API funcionou: nada de navegar até /dashboard


def test_nao_notifica_de_novo(page, telegram):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [msg(11, "Oi")]
    messages.check_and_notify(page)
    messages.check_and_notify(page)
    assert len(telegram.of("sendMessage")) == 1

    page.mensagens[CONV].append(msg(12, "Alô?"))
    messages.check_and_notify(page)
    assert len(telegram.of("sendMessage")) == 2
    assert "Alô?" in telegram.last("sendMessage")["text"]
    assert "Oi" not in telegram.last("sendMessage")["text"].split("\n\n")[1]


def test_ignora_mensagens_minhas_e_ja_lidas(page, telegram):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [msg(11, "minha", de=EU), msg(12, "lida", lida=True)]
    messages.check_and_notify(page)
    assert telegram.of("sendMessage") == []


def test_telegram_fora_tenta_de_novo(page, telegram):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [msg(11, "Oi")]
    telegram.down = True
    messages.check_and_notify(page)
    telegram.down = False
    messages.check_and_notify(page)
    assert len(_sent_texts(telegram)) == 2  # a 1ª falhou, a 2ª chegou


def test_api_falhando_cai_pro_badge(page, telegram):
    page.get_fails = True
    messages.check_and_notify(page)
    assert page.gotos == [sel.DASHBOARD_URL]


# --- Resposta pelo Telegram ------------------------------------------------------------------


@pytest.fixture
def notificada(page, telegram):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [msg(11, "Consegue em 5 dias?")]
    messages.check_and_notify(page)
    telegram.clear()
    return 1001  # message_id da notificação no Telegram falso


def test_reply_envia_pro_cliente(api, page, telegram, notificada, snapshot):
    telegram.push_message("Consigo sim! Te mando o cronograma.", reply_to=notificada)
    api.poll({})
    assert page.posts == []  # o polling do Telegram nunca envia — só enfileira

    FREELAS99.tick({})
    assert page.posts == [{"idConversa": CONV, "texto": "Consigo sim! Te mando o cronograma.", "idsArquivos": []}]
    snapshot("messages_resposta_enviada", _sent_texts(telegram))
    assert read_json(messages.THREADS_PATH)["outbox"] == []

    # Responder à confirmação manda outra mensagem na mesma conversa.
    confirmacao = 1002
    telegram.push_message("Qualquer dúvida me chama.", reply_to=confirmacao)
    api.poll({})
    FREELAS99.tick({})
    assert page.posts[-1]["texto"] == "Qualquer dúvida me chama."


def test_padrao_suspeito_nao_confirma(api, page, telegram, notificada, snapshot):
    page.send_result = {"status": {"id": 6}, "failInfo": {"key": "SUSPECT_PATTERN_REQUIRE_CONFIRMATION"}}
    telegram.push_message("me chama no zap 11 99999-9999", reply_to=notificada)
    api.poll({})
    FREELAS99.tick({})
    assert len(page.posts) == 1
    assert "confirmarPadraoDetectado" not in page.posts[0]
    snapshot("messages_resposta_padrao_suspeito", _sent_texts(telegram))


def test_crash_no_meio_do_envio_nao_reenvia(api, page, telegram, notificada):
    messages.queue_reply(notificada, "Oi")
    data = read_json(messages.THREADS_PATH)
    data["outbox"][0]["status"] = "sending"
    with open(messages.THREADS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)

    FREELAS99.tick({})
    assert page.posts == []
    assert "Não sei se a resposta" in telegram.last("sendMessage")["text"]
    assert read_json(messages.THREADS_PATH)["outbox"] == []


def test_reply_de_outro_chat_ignorado(api, page, telegram, notificada):
    telegram.push_message("oi", reply_to=notificada, chat_id="999")
    api.poll({})
    FREELAS99.tick({})
    assert page.posts == []


def test_reply_a_mensagem_qualquer_ignorado(api, page, telegram, notificada):
    telegram.push_message("oi", reply_to=424242)
    api.poll({})
    FREELAS99.tick({})
    assert page.posts == []
    assert telegram.of("sendMessage") == []


def test_reply_sem_texto(api, page, telegram, notificada):
    telegram.push_update(message={
        "message_id": 9999, "chat": {"id": int(CHAT_ID)}, "photo": [{}], "reply_to_message": {"message_id": notificada},
    })
    api.poll({})
    FREELAS99.tick({})
    assert page.posts == []
    assert "Só dá pra responder o cliente com texto" in telegram.last("sendMessage")["text"]


def test_varias_conversas_nao_lidas(page, telegram):
    page.conversas = [conversa(), conversa(id_=222, cliente="Ana S.", projeto="Loja virtual")]
    page.mensagens[CONV] = [msg(11, "Oi, tudo bem?")]
    page.mensagens[222] = [msg(21, "Qual o prazo?"), msg(22, "E aceita parcelar?")]
    messages.check_and_notify(page)

    textos = _sent_texts(telegram)
    assert len(textos) == 2  # uma notificação por conversa
    assert "Bruno M." in textos[0] and "Oi, tudo bem?" in textos[0]
    assert "Ana S." in textos[1] and "Qual o prazo?" in textos[1] and "E aceita parcelar?" in textos[1]
    assert messages.find_thread(1001)["id"] == CONV
    assert messages.find_thread(1002)["id"] == 222


# --- Resposta automática pela IA ------------------------------------------------------------

CONFIG_AUTO = {"mensagens": {"resposta_automatica": True, "max_respostas_ia_por_conversa": 2}, "proposal": {}}
PROPOSTA = "Enviei uma proposta de R$&nbsp;1.100,00 pelo projeto com uma dura&ccedil;&atilde;o estimada de 8 dias."


@pytest.fixture
def ia(monkeypatch):
    """Troca a IA: `ia.resposta` é o que generate_chat_reply devolve; `ia.ctx` o que recebeu."""
    estado = SimpleNamespace(resposta={"acao": "responder", "texto": "Fala, Bruno! Consigo sim. Bora?"}, ctx=None)

    def fake(ctx, config):
        estado.ctx = ctx
        return estado.resposta
    monkeypatch.setattr(messages.ai_writer, "generate_chat_reply", fake)
    return estado


@pytest.fixture
def conversa_em_negociacao(page):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [
        msg(10, PROPOSTA, de=EU, sistema=True),
        msg(11, "fala meu brother"),
        msg(12, "Voc&ecirc; consegue integrar com o Asaas?"),
    ]
    page.send_result = "auto"


@pytest.fixture
def relogio(monkeypatch):
    """Relógio controlado + sorteios sempre no mínimo da faixa (horários previsíveis)."""
    agora = SimpleNamespace(t=1_790_000_000.0)
    monkeypatch.setattr(messages, "_now", lambda: agora.t)
    monkeypatch.setattr(messages.random, "uniform", lambda a, b: a)
    # Horário exibido no Telegram em UTC: o snapshot não pode depender do fuso da máquina.
    monkeypatch.setattr(messages.views, "_hora", lambda epoch: datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M"))
    return agora


def test_ia_responde_sozinha(page, telegram, ia, conversa_em_negociacao, relogio, snapshot):
    messages.check_and_notify(page, CONFIG_AUTO)

    # Contexto que foi pra IA: histórico com a proposta, e as 2 mensagens novas juntas.
    assert ia.ctx["freelancer"] == "Pedro"
    assert ia.ctx["novas"] == ["fala meu brother", "Você consegue integrar com o Asaas?"]
    assert ia.ctx["historico"] == [
        ("Site (sistema)", "Enviei uma proposta de R$ 1.100,00 pelo projeto com uma duração estimada de 8 dias.")
    ]
    # O cumprimento sai na hora; o resto espera o tempo de ler + pensar + digitar.
    assert [p["texto"] for p in page.posts] == ["Fala, Bruno!"]
    plano = telegram.of("editMessageText")[-1]
    snapshot("messages_ia_respondendo", {"text": plano["text"], "reply_markup": plano["reply_markup"]})

    relogio.t += 10  # parte curta: ~3s lendo + 15s pensando + 5s digitando = ~23s
    messages.process_outbox(page)
    assert len(page.posts) == 1  # ainda "digitando"

    relogio.t += 60
    messages.process_outbox(page)
    assert [p["texto"] for p in page.posts] == ["Fala, Bruno!", "Consigo sim. Bora?"]
    final = telegram.of("editMessageText")[-1]
    snapshot("messages_ia_respondeu", {"text": final["text"], "reply_markup": final["reply_markup"]})

    # Não responde de novo a mesma mensagem.
    messages.check_and_notify(page, CONFIG_AUTO)
    assert len(page.posts) == 2


def test_cancelar_o_que_falta(api, page, telegram, ia, conversa_em_negociacao, relogio):
    messages.check_and_notify(page, CONFIG_AUTO)
    assert len(page.posts) == 1
    telegram.push_callback(f"msgcancel:{CONV}-12", message_id=1001)
    api.poll({})
    assert "já tinha(m) saído" in telegram.last("answerCallbackQuery")["text"]

    relogio.t += 3600
    FREELAS99.tick({})
    assert len(page.posts) == 1  # o resto nunca saiu
    final = telegram.of("editMessageText")[-1]
    assert "✋ cancelada — não enviada" in final["text"]
    assert final["reply_markup"]["inline_keyboard"] == [[{"text": "🗑️ Apagar do site", "callback_data": f"msgdel:{CONV}-12"}]]


def test_parte_recusada_pelo_site_vira_sugestao(page, telegram, ia, conversa_em_negociacao, relogio):
    messages.check_and_notify(page, CONFIG_AUTO)
    page.send_result = {"status": {"id": 6}, "failInfo": {"key": "SUSPECT_PATTERN_REQUIRE_CONFIRMATION"}}
    relogio.t += 3600
    messages.process_outbox(page)
    ultima = telegram.last("sendMessage")
    assert "Precisa de você" in ultima["text"] and "Consigo sim. Bora?" in ultima["text"]
    assert ultima["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Enviar sugestão"


def test_caiu_no_meio_de_uma_parte(page, telegram, ia, conversa_em_negociacao, relogio):
    ia.resposta = {"acao": "responder", "texto": "Consigo sim.\n\nMe manda o acesso ao repositório?"}
    messages.check_and_notify(page, CONFIG_AUTO)
    data = read_json(messages.THREADS_PATH)
    data["groups"][f"{CONV}-12"]["partes"][0]["status"] = "sending"  # simula queda durante o POST
    with open(messages.THREADS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)

    relogio.t += 3600
    messages.process_outbox(page)
    assert page.posts == []  # nem reenvia a parte incerta, nem manda a seguinte
    assert "Não sei se a resposta" in telegram.last("sendMessage")["text"]
    partes = read_json(messages.THREADS_PATH)["groups"][f"{CONV}-12"]["partes"]
    assert [p["status"] for p in partes] == ["uncertain", "cancelled"]


def test_divisao_em_mensagens():
    assert messages.split_human_messages("Fala, Bruno!\n\nConsigo sim.\n\nBora?") == ["Fala, Bruno!", "Consigo sim.", "Bora?"]
    assert messages.split_human_messages("Bom dia, Ana! Tudo certo por aqui.") == ["Bom dia, Ana!", "Tudo certo por aqui."]
    assert messages.split_human_messages("Consigo sim, sem problema.") == ["Consigo sim, sem problema."]
    # "Oi" no começo de uma frase longa não é cumprimento separado.
    frase = "Oi, então, sobre o que você perguntou do pagamento, funciona assim: tudo automático."
    assert messages.split_human_messages(frase) == [frase]
    # No máximo 4 mensagens: o excesso junta na última.
    assert messages.split_human_messages("a\n\nb\n\nc\n\nd\n\ne") == ["a", "b", "c", "d\n\ne"]


def test_horarios_de_cada_parte():
    cfg = {"cps": (4.0, 4.0), "pensar": (20, 20), "pausa": (5, 5), "leitura_cps": 25.0}
    partes = ["Fala, Bruno!", "x" * 200, "y" * 40]
    horarios = messages.schedule_parts(partes, ["z" * 250], cfg, 1000.0)
    # cumprimento na hora; 1ª parte: ler 250/25=10s + pensar 20s + digitar 200/4=50s; 2ª: pausa 5s + digitar 10s
    assert horarios == [1000.0, 1080.0, 1095.0]
    # Sem cumprimento, a 1ª parte já espera ler + pensar + digitar.
    assert messages.schedule_parts(["y" * 40], ["z" * 250], cfg, 1000.0) == [1040.0]


def test_ia_citando_valor_novo_vira_sugestao(api, page, telegram, ia, conversa_em_negociacao, snapshot):
    ia.resposta = {"acao": "responder", "texto": "Fecho por R$ 900,00, pode ser?"}
    messages.check_and_notify(page, CONFIG_AUTO)

    assert page.posts == []  # não enviou
    aviso = telegram.last("sendMessage")
    snapshot("messages_ia_sugestao", {"text": aviso["text"], "reply_markup": aviso["reply_markup"]})

    # "✅ Enviar sugestão" manda exatamente a sugestão.
    callback = aviso["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    telegram.push_callback(callback, message_id=1001)
    api.poll({})
    FREELAS99.tick({})
    assert page.posts == [{"idConversa": CONV, "texto": "Fecho por R$ 900,00, pode ser?", "idsArquivos": []}]
    assert telegram.of("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Sugestão enviada"

    # Clicar de novo não manda duas vezes.
    telegram.push_callback(callback, message_id=1001)
    api.poll({})
    FREELAS99.tick({})
    assert len(page.posts) == 1


def test_ia_escala(page, telegram, ia, conversa_em_negociacao):
    ia.resposta = {"acao": "escalar", "motivo": "cliente quer marcar uma ligação amanhã", "rascunho": "Claro! Que horário?"}
    messages.check_and_notify(page, CONFIG_AUTO)
    assert page.posts == []
    texto = telegram.last("sendMessage")["text"]
    assert "cliente quer marcar uma ligação amanhã" in texto and "Claro! Que horário?" in texto


def test_ia_falhou_avisa_sem_sugestao(page, telegram, ia, conversa_em_negociacao):
    ia.resposta = None
    messages.check_and_notify(page, CONFIG_AUTO)
    assert page.posts == []
    enviada = telegram.last("sendMessage")
    assert "a IA falhou ao gerar a resposta" in enviada["text"]
    assert "reply_markup" not in enviada  # sem sugestão → sem botão; reply manual continua valendo
    assert messages.find_thread(1001)["id"] == CONV


def test_projeto_contratado_so_notifica(page, telegram, ia):
    page.conversas = [conversa(idStatusProjeto=3, freelancerProjeto=True)]
    page.mensagens[CONV] = [msg(11, "Como está o andamento?")]
    messages.check_and_notify(page, CONFIG_AUTO)
    assert ia.ctx is None and page.posts == []
    assert "projeto em andamento (você já foi contratado)" in telegram.last("sendMessage")["text"]


def test_limite_de_respostas_da_ia(page, telegram, ia, conversa_em_negociacao, relogio):
    for novo_id in (13, 14):
        messages.check_and_notify(page, CONFIG_AUTO)
        page.mensagens[CONV].append(msg(novo_id, "e mais uma coisa"))
    assert len(read_json(messages.THREADS_PATH)["groups"]) == 2
    messages.check_and_notify(page, CONFIG_AUTO)  # 3ª: passou do limite (2)
    assert len(read_json(messages.THREADS_PATH)["groups"]) == 2
    assert "a IA já mandou 2 respostas" in telegram.last("sendMessage")["text"]


def test_apagar_do_site(api, page, telegram, ia, conversa_em_negociacao, relogio):
    messages.check_and_notify(page, CONFIG_AUTO)
    relogio.t += 3600
    messages.process_outbox(page)
    botao = telegram.of("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][0]
    assert botao == {"text": "🗑️ Apagar do site", "callback_data": f"msgdel:{CONV}-12"}

    telegram.push_callback(botao["callback_data"], message_id=1001)
    api.poll({})
    assert page.deletes == []  # o polling do Telegram só enfileira
    FREELAS99.tick({})
    # As duas partes (cumprimento + conteúdo) saem do site.
    assert page.deletes == [sel.MESSAGES_API_DELETE.format(id=i) + "?deletar=true" for i in (555001, 555002)]
    rotulos = [c["reply_markup"]["inline_keyboard"][0][0]["text"] for c in telegram.of("editMessageReplyMarkup")]
    assert rotulos == ["⏳ Apagando...", "🗑️ 2 mensagens apagadas do site"]


def test_sem_config_nunca_responde_sozinho(page, telegram, ia, conversa_em_negociacao):
    messages.check_and_notify(page)  # dry_run.py chama assim
    assert page.posts == [] and ia.ctx is None


# --- Esperar o cliente parar de escrever / mensagem nova no meio da resposta ------------------


def _rodar_linha_do_tempo(page, relogio, chegadas, ate, config=CONFIG_AUTO):
    """Simula o loop: checagem a cada 15s; `chegadas` = [(segundos, _, texto)] a partir do relógio."""
    t0 = relogio.t
    log = []
    enviados = 0
    for s in range(0, ate, 15):
        relogio.t = t0 + s
        for seg, mid, texto in chegadas:
            if s - 15 < seg <= s:
                # Ids crescentes na ordem de chegada, como no site (as do bot são 5550xx).
                novo_id = max(m["id"] for ms in page.mensagens.values() for m in ms) + 1
                page.mensagens[CONV].append(msg(novo_id, texto, em=t0 + seg))
                log.append((seg, "cliente", texto))
        messages.check_and_notify(page, config)
        messages.process_outbox(page)
        log += [(s, "bot", p["texto"]) for p in page.posts[enviados:]]
        enviados = len(page.posts)
    return log


def test_cenario_bruno_espera_o_cliente_terminar(page, telegram, ia, relogio):
    """Linha do tempo real (30/09): 'fala meu brother', 15s depois as tecnologias, 3min depois o escopo."""
    page.conversas = [conversa()]
    page.mensagens[CONV] = [msg(10, PROPOSTA, de=EU, sistema=True)]
    page.send_result = "auto"
    vistas = []

    def fake(ctx, config):
        vistas.append((list(ctx["novas"]), ctx["ja_conversou"]))
        if len(vistas) == 1:
            return {"acao": "responder", "texto": "Fala, Bruno!\n\nShow, stack que eu domino. Me conta o que falta?"}
        return {"acao": "escalar", "motivo": "o escopo cresceu", "rascunho": "Valeu pelo detalhamento!"}
    messages.ai_writer.generate_chat_reply = fake

    log = _rodar_linha_do_tempo(page, relogio, [
        (9, 11, "fala meu brother"),
        (24, 12, "as tecnologias utilizadas no sistema são Next.js / React / TypeScript"),
        (216, 13, "O Agendle é um sistema SaaS multiempresa..."),
    ], ate=420)

    # UMA resposta pras duas primeiras mensagens, só depois de 60s sem o cliente escrever.
    assert vistas[0] == (["fala meu brother", "as tecnologias utilizadas no sistema são Next.js / React / TypeScript"], False)
    respostas = [(seg, texto) for seg, quem, texto in log if quem == "bot"]
    assert respostas[0] == (90, "Fala, Bruno!")  # 11:25:24 + 60s → checagem das 11:26:30
    assert [t for _, t in respostas] == ["Fala, Bruno!", "Show, stack que eu domino. Me conta o que falta?"]
    # O escopo (3min depois) é uma conversa nova: a IA vê só ele, sabendo que já conversou.
    assert vistas[1] == (["O Agendle é um sistema SaaS multiempresa..."], True)
    assert len(vistas) == 2


def test_mensagem_nova_substitui_o_que_falta(page, telegram, ia, relogio):
    page.conversas = [conversa()]
    page.mensagens[CONV] = [msg(10, PROPOSTA, de=EU, sistema=True)]
    page.send_result = "auto"
    vistas = []

    def fake(ctx, config):
        vistas.append((list(ctx["novas"]), ctx["ja_conversou"]))
        saudacao = "" if ctx["ja_conversou"] else "Fala, Bruno!\n\n"
        return {"acao": "responder", "texto": saudacao + "x" * 400}  # conteúdo longo: ~2min digitando
    messages.ai_writer.generate_chat_reply = fake

    log = _rodar_linha_do_tempo(page, relogio, [
        (0, 11, "Você faz integração com gateway de pagamento?"),
        (90, 12, "Ah, e também preciso de área de login"),  # chega com o conteúdo ainda "sendo digitado"
    ], ate=400)

    respostas = [texto for _, quem, texto in log if quem == "bot"]
    assert respostas[0] == "Fala, Bruno!"
    assert len(respostas) == 2  # cumprimento + UMA resposta de conteúdo (a 1ª nunca saiu)
    # A 2ª resposta cobre as duas perguntas e não cumprimenta de novo.
    assert vistas[1] == (["Você faz integração com gateway de pagamento?", "Ah, e também preciso de área de login"], True)
    substituida = [c["text"] for c in telegram.of("editMessageText") if "substituída" in c["text"]]
    assert substituida and "🔁 não enviada — o cliente escreveu de novo" in substituida[-1]


def test_aviso_sem_ia_nao_espera(page, telegram, ia, relogio):
    """Projeto já contratado: sem resposta da IA, o aviso chega na hora (nada pra esperar)."""
    page.conversas = [conversa(idStatusProjeto=3, freelancerProjeto=True)]
    page.mensagens[CONV] = [msg(11, "Como está o andamento?", em=relogio.t)]
    messages.check_and_notify(page, CONFIG_AUTO)
    assert "Como está o andamento?" in telegram.last("sendMessage")["text"]


# --- Modo aprovação (mensagens.aprovar_antes_de_enviar) ------------------------------------

CONFIG_APROVACAO = {"mensagens": {**CONFIG_AUTO["mensagens"], "aprovar_antes_de_enviar": True}, "proposal": {}}


def test_modo_aprovacao_so_envia_depois_do_clique(api, page, telegram, ia, conversa_em_negociacao, relogio, snapshot):
    ia.resposta = {"acao": "responder", "texto": "Fala, Bruno!\n\nConsigo sim, já integrei com o Asaas.\n\nBora?"}
    messages.check_and_notify(page, CONFIG_APROVACAO)
    assert page.posts == []  # nada sai sem o clique
    pedido = telegram.last("sendMessage")
    snapshot("messages_ia_aprovar", {"text": pedido["text"], "reply_markup": pedido["reply_markup"]})

    relogio.t += 600  # você aprova 10 min depois
    callback = pedido["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    telegram.push_callback(callback, message_id=1001)
    api.poll({})
    assert telegram.last("answerCallbackQuery")["text"] == "Aprovada! Sai em partes, como alguém digitando."
    assert telegram.of("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == (
        "✅ Aprovada — acompanhe o envio abaixo"
    )

    FREELAS99.tick({})
    assert [p["texto"] for p in page.posts] == ["Fala, Bruno!"]  # cumprimento na hora do clique
    relogio.t += 12  # já "pensou" (você aprovou): só digitar a 1ª parte (37 caracteres ≈ 10,6s)
    FREELAS99.tick({})
    assert [p["texto"] for p in page.posts] == ["Fala, Bruno!", "Consigo sim, já integrei com o Asaas."]
    relogio.t += 60
    FREELAS99.tick({})
    assert len(page.posts) == 3
    assert "🤖 A IA respondeu" in telegram.of("editMessageText")[-1]["text"]

    # Clique repetido não envia de novo.
    telegram.push_callback(callback, message_id=1001)
    api.poll({})
    FREELAS99.tick({})
    assert len(page.posts) == 3
    assert telegram.last("answerCallbackQuery")["text"] == "Essa resposta já foi enviada (ou expirou)."


def test_modo_aprovacao_mantem_as_travas(page, telegram, ia, conversa_em_negociacao, relogio):
    """Valor novo continua virando "Precisa de você" (com o motivo), não um pedido de aprovação comum."""
    ia.resposta = {"acao": "responder", "texto": "Faço por R$ 900,00."}
    messages.check_and_notify(page, CONFIG_APROVACAO)
    assert "Precisa de você" in telegram.last("sendMessage")["text"]
    assert "cita um valor que você ainda não tinha passado" in telegram.last("sendMessage")["text"]
