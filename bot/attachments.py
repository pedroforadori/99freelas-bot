"""
Lê o conteúdo de anexos e links que o cliente mandou na conversa, pra entrar no pacote do
Claude (messages.process_claude_packets) — o usuário cola o pacote num Projeto do claude.ai
e não precisa abrir arquivo nem link nenhum.

Nunca levanta exceção: cada leitura devolve (texto, None) ou (None, motivo) — o motivo vai
pro pacote junto com o nome/link, pra o usuário saber o que ficou de fora e por quê.
"""
import io
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

from bot import ai_writer
from bot import site_selectors as sel
from bot.logger_setup import get_logger

log = get_logger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_CHARS = 6000          # por anexo/link, no pacote
MIN_TEXTO_PAGINA = 200    # menos que isso numa página HTML = montada por JavaScript (Figma, Canva...)
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

_URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.I)
_IMAGENS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
_TEXTO_PURO = {"txt", "md", "csv", "json", "xml", "log"}
_HTML = {"html", "htm"}

# Tipo do conteúdo (Content-Type de um link) → extensão tratada por extract_text.
_MIME_EXT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/plain": "txt", "text/markdown": "md", "text/csv": "csv", "application/json": "json",
    "text/html": "html", "application/xhtml+xml": "html",
    **{mime: ext for ext, mime in _IMAGENS.items()},
}


def clip(texto: str, limite: int = MAX_CHARS) -> str:
    texto = texto.strip()
    return texto if len(texto) <= limite else texto[:limite].rstrip() + "\n… (cortado)"


def find_links(texto: str) -> list[str]:
    """URLs na ordem em que aparecem, sem repetir e sem a pontuação colada no fim."""
    vistos = []
    for bruto in _URL_PATTERN.findall(texto or ""):
        url = bruto.rstrip(".,;:!?)]}")
        if url.lower().startswith("www."):
            url = "https://" + url
        if url not in vistos:
            vistos.append(url)
    return vistos


def _decode(conteudo: bytes) -> str:
    try:
        return conteudo.decode("utf-8")
    except UnicodeDecodeError:
        return conteudo.decode("latin-1")


def _html_text(conteudo: bytes) -> str:
    soup = BeautifulSoup(_decode(conteudo), "html.parser")
    titulo = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "svg", "head"]):
        tag.decompose()
    linhas = [linha.strip() for linha in soup.get_text("\n").splitlines() if linha.strip()]
    corpo = "\n".join(_juntar_curtas(linhas))
    return f"{titulo}\n\n{corpo}".strip() if titulo and titulo not in corpo[:200] else corpo


def _juntar_curtas(linhas: list[str], curta: int = 40, max_linha: int = 160) -> list[str]:
    """
    Telas/mockups em HTML viram uma palavra por linha (botão, rótulo, valor) — junta as
    curtas com " · " pra o pacote não ficar com centenas de linhas.
    """
    saida: list[str] = []
    for linha in linhas:
        if saida and len(linha) < curta and len(saida[-1]) + len(linha) < max_linha and not saida[-1].endswith((".", ":")):
            saida[-1] += " · " + linha
        else:
            saida.append(linha)
    return saida


def _pdf_text(conteudo: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(conteudo))
    return "\n\n".join((pagina.extract_text() or "").strip() for pagina in reader.pages).strip()


def _docx_text(conteudo: bytes) -> str:
    import docx

    documento = docx.Document(io.BytesIO(conteudo))
    partes = [p.text for p in documento.paragraphs if p.text.strip()]
    for tabela in documento.tables:
        for linha in tabela.rows:
            partes.append(" | ".join(c.text.strip() for c in linha.cells))
    return "\n".join(partes)


def _xlsx_text(conteudo: bytes) -> str:
    import openpyxl

    planilha = openpyxl.load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    partes = []
    for aba in planilha.worksheets:
        partes.append(f"Aba: {aba.title}")
        for linha in aba.iter_rows(values_only=True):
            valores = ["" if v is None else str(v) for v in linha]
            if any(valores):
                partes.append(" | ".join(valores).rstrip(" |"))
    return "\n".join(partes)


def _with_ai(conteudo: bytes, mime: str, config: dict | None) -> tuple[str | None, str | None]:
    texto = ai_writer.describe_file(conteudo, mime, config or {})
    if texto:
        return clip(texto), None
    return None, "a IA não conseguiu ler"


def extract_text(nome: str, conteudo: bytes, config: dict | None, ext: str | None = None) -> tuple[str | None, str | None]:
    """Texto de um arquivo, pela extensão (ou `ext`, quando vem do Content-Type de um link)."""
    if not ext:
        ext = nome.rsplit(".", 1)[-1] if "." in nome else ""
    ext = ext.lower()
    if len(conteudo) > MAX_DOWNLOAD_BYTES:
        return None, "arquivo grande demais"
    try:
        if ext in _IMAGENS:
            return _with_ai(conteudo, _IMAGENS[ext], config)
        if ext == "pdf":
            texto = _pdf_text(conteudo)
            if len(texto) < 50:  # PDF escaneado (só imagem): a IA lê
                return _with_ai(conteudo, "application/pdf", config)
        elif ext == "docx":
            texto = _docx_text(conteudo)
        elif ext == "xlsx":
            texto = _xlsx_text(conteudo)
        elif ext in _HTML:
            texto = _html_text(conteudo)
        elif ext in _TEXTO_PURO:
            texto = _decode(conteudo)
        else:
            return None, f"formato .{ext or '?'} não suportado"
    except Exception as e:
        log.info("Não consegui ler o arquivo %s: %s", nome, e)
        return None, f"arquivo ilegível ({type(e).__name__})"
    if not texto.strip():
        return None, "arquivo sem texto"
    return clip(texto), None


def _google_export_url(url: str) -> str | None:
    """Docs/Sheets/Drive públicos: a página é montada por JavaScript; a exportação vem em texto."""
    m = re.match(r"https://docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]+)", url)
    if m:
        formato = {"document": "txt", "spreadsheets": "csv", "presentation": "txt"}[m.group(1)]
        return f"https://docs.google.com/{m.group(1)}/d/{m.group(2)}/export?format={formato}"
    m = re.match(r"https://drive\.google\.com/(?:file/d/([\w-]+)|open\?id=([\w-]+))", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1) or m.group(2)}"
    return None


def _download(url: str) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, stream=True, allow_redirects=True)
    resp.raise_for_status()
    corpo = b""
    for pedaco in resp.iter_content(64 * 1024):
        corpo += pedaco
        if len(corpo) > MAX_DOWNLOAD_BYTES:
            resp.close()
            raise ValueError("arquivo grande demais")
    resp._content = corpo
    return resp


def _http_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        dica = {401: " (precisa de login)", 403: " (acesso negado — precisa de login ou é privado?)",
                404: " (página não encontrada)"}.get(status, "")
        return f"HTTP {status}{dica}"
    if isinstance(e, requests.Timeout):
        return f"o site não respondeu em {TIMEOUT}s"
    if isinstance(e, requests.ConnectionError):
        return "não consegui conectar no site"
    return str(e) or type(e).__name__


def read_attachment(arquivo: dict, config: dict | None) -> tuple[str | None, str | None]:
    """Anexo de mensagem do 99Freelas (item de "arquivos", ver sel.MESSAGES_ATTACHMENT_URL)."""
    try:
        url = sel.MESSAGES_ATTACHMENT_URL.format(**arquivo)
        resp = _download(url)
    except KeyError as e:
        return None, f"anexo sem o campo {e} (o site mudou o formato?)"
    except Exception as e:
        log.info("Não consegui baixar o anexo %s: %s", arquivo.get("nome"), e)
        return None, f"falha ao baixar: {_http_error(e)}"
    return extract_text(f"{arquivo.get('nome') or 'arquivo'}.{arquivo['extensao']}", resp.content, config)


def read_link(url: str, config: dict | None) -> tuple[str | None, str | None]:
    """Conteúdo de um link mandado pelo cliente (sem cookies do 99Freelas — é site de fora)."""
    export = _google_export_url(url)
    try:
        resp = _download(export or url)
    except Exception as e:
        log.info("Não consegui ler o link %s: %s", url, e)
        return None, _http_error(e)

    mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if export and "html" in mime:
        return None, "o Google pediu login (arquivo não está público)"
    caminho = urllib.parse.urlparse(resp.url or url).path
    ext = _MIME_EXT.get(mime) or (caminho.rsplit(".", 1)[-1].lower() if "." in caminho.rsplit("/", 1)[-1] else "")
    if export and not ext:
        ext = "txt"
    if ext in _HTML:
        texto = _html_text(resp.content)
        if len(texto) < MIN_TEXTO_PAGINA:
            return None, "a página não tem texto legível (é montada por JavaScript ou é só imagem)"
        return clip(texto), None
    if not ext:
        return None, f"formato não suportado ({mime or 'desconhecido'})"
    return extract_text(caminho.rsplit("/", 1)[-1] or "link", resp.content, config, ext=ext)
