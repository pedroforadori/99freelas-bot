"""
Primeiro nome do cliente no cumprimento da proposta (pedido do usuário, 2026-10-28):
"Olá, tudo bem?" → "Olá, Tulio, tudo bem?". O nome vem do bloco "Cliente" da página do
projeto ("Tulio V.").
"""
import pytest

from bot import ai_writer, submitter
from bot import proposal as proposal_mod
from bot import site_selectors as sel
from tests.conftest import project_99


@pytest.mark.parametrize("bruto, esperado", [
    ("Tulio V.", "Tulio"),
    ("Otávio Q.", "Otávio"),
    ("  joão\xa0S. ", "João"),
    ("MARIA", "Maria"),
    ("Ana-Clara P.", "Ana-Clara"),
    ("V.", None),
    ("", None),
    (None, None),
    ("dev123", None),
])
def test_primeiro_nome(bruto, esperado):
    assert proposal_mod.primeiro_nome(bruto) == esperado


@pytest.mark.parametrize("texto, esperado", [
    ("Olá, tudo bem?\n\nPosso ajudar.", "Olá, Tulio, tudo bem?\n\nPosso ajudar."),
    ("Olá! Tenho interesse.", "Olá, Tulio! Tenho interesse."),
    ("Olá!\n\nTenho interesse.", "Olá, Tulio!\n\nTenho interesse."),
    ("Olá,\nTenho interesse.", "Olá, Tulio!\nTenho interesse."),
    ("Bom dia, tudo certo?", "Bom dia, Tulio, tudo certo?"),
    ("Oi tudo bem?", "Oi, Tulio, tudo bem?"),
    # sem cumprimento no começo, ou com o nome já na 1ª linha: não mexe
    ("Posso ajudar no seu site.", "Posso ajudar no seu site."),
    ("Olá, Tulio, tudo bem?", "Olá, Tulio, tudo bem?"),
    ("Olhei seu projeto.", "Olhei seu projeto."),
])
def test_cumprimento_com_nome(texto, esperado):
    assert proposal_mod.com_nome_do_cliente(texto, {"cliente": "Tulio"}) == esperado


def test_sem_nome_nao_mexe():
    assert proposal_mod.com_nome_do_cliente("Olá, tudo bem?", {}) == "Olá, tudo bem?"
    assert proposal_mod.com_nome_do_cliente("Olá, tudo bem?", {"cliente": None}) == "Olá, tudo bem?"


def test_build_proposal_poe_o_nome(monkeypatch):
    monkeypatch.setattr(ai_writer, "generate_proposal_text", lambda *a, **k: "Olá, tudo bem?\n\nPosso ajudar.")
    config = {"proposal": {"ia_provider": "gemini", "ia_model": "x", "texto_modo": "ia"}}
    p = proposal_mod.build_proposal({**project_99(), "cliente": "Tulio"}, config, full_description="desc")
    assert p["texto"].startswith("Olá, Tulio, tudo bem?")


def test_template_fixo_tambem_ganha_o_nome():
    config = {"proposal": {"texto_modo": "fixo", "texto": "Olá, tudo bem?\n\nTenho interesse em {titulo}."}}
    p = proposal_mod.build_proposal({**project_99(), "cliente": "Tulio"}, config)
    assert p["texto"].startswith("Olá, Tulio, tudo bem?")


class _El:
    def __init__(self, texto):
        self.texto = texto

    def inner_text(self):
        return self.texto


class _Page:
    def __init__(self, nome):
        self.nome = nome

    def query_selector(self, seletor):
        assert seletor == sel.PROJECT_PAGE_CLIENT_NAME
        return _El(self.nome) if self.nome is not None else None


def test_le_o_primeiro_nome_da_pagina():
    assert submitter._read_client_first_name(_Page("Tulio V.")) == "Tulio"
    assert submitter._read_client_first_name(_Page(None)) is None
