"""
Pacote pro Projeto do Claude (messages.process_claude_packets + attachments.py): no 1º
retorno do cliente depois da proposta, espera ele parar de escrever e manda no Telegram o
texto pronto pra colar — com o conteúdo de anexos e links já lido. A API do site é a Page
falsa de test_messages; os downloads (anexos e links) são um requests.get falso.
"""
import html
import io

import pytest
import requests

from bot import attachments, messages
from tests.conftest import read_json
from tests.test_messages import CONV, EU, conversa, msg, page, relogio  # noqa: F401 (fixtures)

PROPOSTA = (
    "Enviei uma proposta de R$&nbsp;580,00 pelo projeto com uma dura&ccedil;&atilde;o estimada de 5 dias."
    "<br/><br/>Detalhes da proposta:<br/>Ol&aacute;, tudo bem?<br/>Posso modernizar as 5 telas do seu PDV."
)
PROJETO_ID = 793959
CONFIG = {"mensagens": {"pacote_claude": {"ativo": True, "aguardar_minutos": 15}}}
MIN = 60


class _HttpResp:
    def __init__(self, url, status=200, body=b"", ctype="text/html; charset=utf-8"):
        self.url, self.status_code, self.headers = url, status, {"Content-Type": ctype}
        self._content = body

    @property
    def content(self):
        return self._content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, _tamanho):
        yield self._content

    def close(self):
        pass


@pytest.fixture
def web(monkeypatch):
    """requests.get falso: `web.sites[url]` = _HttpResp ou exceção; registra as URLs pedidas."""
    estado = type("Web", (), {"sites": {}, "pedidas": []})()

    def fake_get(url, headers=None, timeout=None, stream=False, allow_redirects=True):
        estado.pedidas.append(url)
        resp = estado.sites.get(url)
        if resp is None:
            raise requests.ConnectionError("sem rede no teste")
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(requests, "get", fake_get)
    return estado


@pytest.fixture
def projeto(page):  # noqa: F811
    page.conversas = [conversa(idProjeto=PROJETO_ID, nomeProjeto="Moderniza&ccedil;&atilde;o do PDV")]
    page.projetos[str(PROJETO_ID)] = "Preciso migrar 5 telas do PDV pra um visual mais limpo."
    page.mensagens[CONV] = [msg(10, PROPOSTA, de=EU, sistema=True, lida=True)]
    return page


def _chega(page, relogio, id_, texto, **kw):  # noqa: F811
    page.mensagens[CONV].append(msg(id_, texto, em=relogio.t, **kw))
    messages.check_and_notify(page, CONFIG)


def _avanca(page, relogio, segundos):  # noqa: F811
    relogio.t += segundos
    messages.check_and_notify(page, CONFIG)


def _pacotes(telegram):
    return [p for p in telegram.of("sendMessage") if "Pacote pro Claude" in p["text"]]


# --- Quando sai ---------------------------------------------------------------------------


def test_espera_o_cliente_parar_e_junta_tudo(projeto, telegram, relogio, snapshot):  # noqa: F811
    _chega(projeto, relogio, 11, "Ol&aacute;, Pedro! Beleza?")
    assert len(telegram.of("sendMessage")) == 1  # o aviso de sempre sai na hora
    assert _pacotes(telegram) == []

    _avanca(projeto, relogio, 10 * MIN)
    _chega(projeto, relogio, 12, "Voc&ecirc; tem algum projeto parecido pra eu ver?")
    _avanca(projeto, relogio, 14 * MIN)  # 14 min depois da 2ª: ainda espera
    assert _pacotes(telegram) == []

    _avanca(projeto, relogio, 1 * MIN)
    pacotes = _pacotes(telegram)
    assert len(pacotes) == 1
    snapshot("pacote_claude", pacotes[0]["text"])

    _avanca(projeto, relogio, 30 * MIN)
    assert len(_pacotes(telegram)) == 1  # nunca repete
    # Responder ao pacote no Telegram responde ao cliente.
    assert messages.find_thread(1003)["id"] == CONV


def test_nao_sai_se_eu_ja_tinha_conversado(projeto, telegram, relogio):  # noqa: F811
    projeto.mensagens[CONV].append(msg(11, "Oi! Alguma dúvida?", de=EU, lida=True))
    _chega(projeto, relogio, 12, "Tenho sim")
    _avanca(projeto, relogio, 20 * MIN)
    assert _pacotes(telegram) == []


def test_nao_sai_sem_proposta(page, telegram, relogio):  # noqa: F811
    page.conversas = [conversa()]
    page.mensagens[CONV] = []
    _chega(page, relogio, 11, "Oi, vi seu perfil")  # cliente puxou conversa, sem proposta minha
    _avanca(page, relogio, 20 * MIN)
    assert _pacotes(telegram) == []


def test_respondi_pelo_site_antes_cancela(projeto, telegram, relogio):  # noqa: F811
    _chega(projeto, relogio, 11, "Consegue começar amanhã?")
    projeto.mensagens[CONV].append(msg(12, "Consigo sim!", de=EU, lida=True))
    _avanca(projeto, relogio, 20 * MIN)
    assert _pacotes(telegram) == []
    assert read_json(messages.CACHE_PATH)["pacotes"]["pendentes"] == {}


def test_sai_mesmo_depois_de_lida_no_site(projeto, telegram, relogio):  # noqa: F811
    _chega(projeto, relogio, 11, "Consegue começar amanhã?")
    projeto.conversas = []  # li no site: a conversa saiu das não lidas
    _avanca(projeto, relogio, 16 * MIN)
    assert len(_pacotes(telegram)) == 1


def test_telegram_fora_tenta_de_novo_sem_duplicar(projeto, telegram, relogio):  # noqa: F811
    _chega(projeto, relogio, 11, "Consegue começar amanhã?")
    telegram.down = True
    _avanca(projeto, relogio, 16 * MIN)
    telegram.down = False
    telegram.clear()  # a tentativa com o Telegram fora também fica gravada
    _avanca(projeto, relogio, 30)
    assert _pacotes(telegram) == []  # espera 1 min antes de tentar de novo
    _avanca(projeto, relogio, 30)
    _avanca(projeto, relogio, 5 * MIN)
    assert len(_pacotes(telegram)) == 1


def test_desligado_nao_faz_nada(projeto, telegram, relogio):  # noqa: F811
    projeto.mensagens[CONV].append(msg(11, "Oi", em=relogio.t))
    messages.check_and_notify(projeto, {})
    relogio.t += 20 * MIN
    messages.check_and_notify(projeto, {})
    assert _pacotes(telegram) == []


def test_pacote_grande_vai_como_arquivo(projeto, telegram, relogio):  # noqa: F811
    projeto.projetos[str(PROJETO_ID)] = "Escopo detalhado. " * 400
    _chega(projeto, relogio, 11, "Consegue?")
    _avanca(projeto, relogio, 16 * MIN)
    assert _pacotes(telegram) == []
    documento = telegram.last("sendDocument")
    arquivo = documento["files"]["document"]
    assert arquivo["name"] == f"pacote-{PROJETO_ID}.md"
    assert "Escopo detalhado." in arquivo["content"].decode("utf-8")
    assert "Pacote pro Claude" in documento["caption"] and len(documento["caption"]) <= 1024
    assert messages.find_thread(1002)["id"] == CONV  # reply ao arquivo também responde


# --- Anexos e links -----------------------------------------------------------------------


def _anexo(nome, ext):
    return {"id": 1, "token": "tk", "nome": nome, "extensao": ext, "idPessoa": 4242, "nomeUrl": nome.lower(), "excluido": False}


def _url_anexo(nome, ext):
    return f"https://duqxk0v9olda1.cloudfront.net/messages/4242/tk/{nome.lower()}.{ext}"


def _docx(*paragrafos) -> bytes:
    import docx

    documento = docx.Document()
    for p in paragrafos:
        documento.add_paragraph(p)
    buf = io.BytesIO()
    documento.save(buf)
    return buf.getvalue()


def _pacote_texto(telegram) -> str:
    return html.unescape(_pacotes(telegram)[0]["text"])  # o pacote vai escapado dentro do <pre>


def test_anexos_lidos_no_pacote(projeto, telegram, relogio, web, monkeypatch):  # noqa: F811
    web.sites[_url_anexo("Briefing", "docx")] = _HttpResp("", body=_docx("Quero 3 telas.", "Prazo curto."))
    web.sites[_url_anexo("notas", "md")] = _HttpResp("", body="# Regras\nCaixa fecha às 18h".encode())
    web.sites[_url_anexo("tela", "png")] = _HttpResp("", body=b"\x89PNG...")
    web.sites[_url_anexo("Backup", "zip")] = _HttpResp("", body=b"PK...")
    lidas = []
    monkeypatch.setattr(attachments.ai_writer, "describe_file", lambda c, mime, cfg: lidas.append(mime) or "Tela de login azul")

    _chega(projeto, relogio, 11, "Segue o material", arquivos=[
        _anexo("Briefing", "docx"), _anexo("notas", "md"), _anexo("tela", "png"), _anexo("Backup", "zip"),
        {**_anexo("velho", "pdf"), "excluido": True},
    ])
    _avanca(projeto, relogio, 16 * MIN)

    texto = _pacote_texto(telegram)
    assert "[Anexo: Briefing.docx]\nQuero 3 telas.\nPrazo curto." in texto
    assert "[Anexo: notas.md]\n# Regras\nCaixa fecha às 18h" in texto
    assert "[Anexo: tela.png]\nTela de login azul" in texto and lidas == ["image/png"]
    assert "[Anexo: Backup.zip — não consegui ler: formato .zip não suportado]" in texto
    assert "velho" not in texto


def test_anexo_que_nao_baixa(projeto, telegram, relogio, web):  # noqa: F811
    web.sites[_url_anexo("contrato", "pdf")] = _HttpResp("", status=403)
    _chega(projeto, relogio, 11, "Segue", arquivos=[_anexo("contrato", "pdf")])
    _avanca(projeto, relogio, 16 * MIN)
    assert "[Anexo: contrato.pdf — não consegui ler: falha ao baixar: HTTP 403" in _pacote_texto(telegram)


def test_links_lidos_ou_com_o_erro(projeto, telegram, relogio, web):  # noqa: F811
    pagina = "<html><head><title>Loja Exemplo</title><script>x()</script></head><body>"
    pagina += "<nav>Menu</nav><p>" + "Vendemos roupas femininas com entrega em todo o Brasil. " * 6 + "</p></body></html>"
    web.sites["https://exemplo.com/ref"] = _HttpResp("https://exemplo.com/ref", body=pagina.encode())
    web.sites["https://privado.com/doc"] = _HttpResp("https://privado.com/doc", status=403)
    web.sites["https://lento.com"] = requests.Timeout("timeout")
    web.sites["https://figma.com/file/abc"] = _HttpResp("https://figma.com/file/abc", body=b"<html><body><div id=app></div></body></html>")
    web.sites["https://docs.google.com/document/d/AbC_1/export?format=txt"] = _HttpResp(
        "", body="Briefing do Google Docs".encode(), ctype="text/plain")

    _chega(projeto, relogio, 11, (
        "Referência: https://exemplo.com/ref. Doc: https://privado.com/doc<br/>"
        'Outros: https://lento.com, <a href="https://figma.com/file/abc">meu figma</a> e '
        "https://docs.google.com/document/d/AbC_1/edit?usp=sharing"
    ))
    _avanca(projeto, relogio, 16 * MIN)

    texto = _pacote_texto(telegram)
    assert "[Link: https://exemplo.com/ref]\nLoja Exemplo\n\nVendemos roupas" in texto
    assert "Menu" not in texto and "x()" not in texto
    assert "[Link: https://privado.com/doc — não consegui ler: HTTP 403 (acesso negado — precisa de login ou é privado?)]" in texto
    assert "[Link: https://lento.com — não consegui ler: o site não respondeu em 15s]" in texto
    assert "[Link: https://figma.com/file/abc — não consegui ler: a página não tem texto legível" in texto
    assert "[Link: https://docs.google.com/document/d/AbC_1/edit?usp=sharing]\nBriefing do Google Docs" in texto


def test_limite_de_links(projeto, telegram, relogio, web):  # noqa: F811
    links = " ".join(f"https://site{i}.com" for i in range(7))
    _chega(projeto, relogio, 11, links)
    _avanca(projeto, relogio, 16 * MIN)
    assert len(web.pedidas) == 5
    assert "[Link: https://site6.com — não li: limite de 5 links por pacote]" in _pacote_texto(telegram)


# --- attachments.py -----------------------------------------------------------------------


def test_find_links():
    texto = "Veja www.loja.com.br, e (https://a.com/x?y=1). Repetido: https://a.com/x?y=1!"
    assert attachments.find_links(texto) == ["https://www.loja.com.br", "https://a.com/x?y=1"]


def test_google_drive_vira_download():
    assert attachments._google_export_url("https://drive.google.com/file/d/XyZ/view") == \
        "https://drive.google.com/uc?export=download&id=XyZ"
    assert attachments._google_export_url("https://docs.google.com/spreadsheets/d/S1/edit#gid=0") == \
        "https://docs.google.com/spreadsheets/d/S1/export?format=csv"
    assert attachments._google_export_url("https://exemplo.com") is None


def test_google_privado_pede_login(web):
    web.sites["https://docs.google.com/document/d/P/export?format=txt"] = _HttpResp("", body=b"<html>login</html>")
    assert attachments.read_link("https://docs.google.com/document/d/P/edit", {}) == \
        (None, "o Google pediu login (arquivo não está público)")


def test_texto_longo_e_cortado():
    texto, erro = attachments.extract_text("a.txt", ("x" * 7000).encode(), {})
    assert erro is None and texto.endswith("… (cortado)") and len(texto) < 6100


def test_html_de_tela_junta_rotulos_curtos():
    html_ = "<html><body>" + "".join(f"<button>Botão {i}</button>" for i in range(6)) + "<p>Frase longa explicando a tela inteira do sistema.</p></body></html>"
    texto, _ = attachments.extract_text("tela.html", html_.encode(), {})
    assert texto.splitlines() == [
        "Botão 0 · Botão 1 · Botão 2 · Botão 3 · Botão 4 · Botão 5",
        "Frase longa explicando a tela inteira do sistema.",
    ]


# --- Proposta rejeitada pelo cliente ---------------------------------------------------------

REJEICAO = "Rejeitei a sua proposta de R$&nbsp;100,00 pelo projeto com uma dura&ccedil;&atilde;o estimada de 1 dia."


@pytest.mark.parametrize("flag_sistema", [True, False])
def test_rejeicao_sozinha_nao_avisa_nem_gera_pacote(projeto, telegram, relogio, flag_sistema):  # noqa: F811
    """O site manda a rejeição como mensagem do CLIENTE com sistema=True; sem a flag, o texto basta."""
    _chega(projeto, relogio, 11, REJEICAO, sistema=flag_sistema)
    _avanca(projeto, relogio, 20 * MIN)
    assert telegram.of("sendMessage") == []


def test_cliente_rejeita_e_depois_escreve_nao_gera_pacote(projeto, telegram, relogio):  # noqa: F811
    _chega(projeto, relogio, 11, REJEICAO, sistema=True)
    _chega(projeto, relogio, 12, "Achei caro, obrigado")
    assert len(telegram.of("sendMessage")) == 1  # o aviso da mensagem dele chega normal
    _avanca(projeto, relogio, 20 * MIN)
    assert _pacotes(telegram) == []


def test_rejeicao_durante_a_espera_cancela_o_pacote(projeto, telegram, relogio):  # noqa: F811
    _chega(projeto, relogio, 11, "Vou pensar")
    _avanca(projeto, relogio, 5 * MIN)
    _chega(projeto, relogio, 12, REJEICAO, sistema=True)
    _avanca(projeto, relogio, 20 * MIN)
    assert _pacotes(telegram) == []
    assert read_json(messages.CACHE_PATH)["pacotes"]["pendentes"] == {}
