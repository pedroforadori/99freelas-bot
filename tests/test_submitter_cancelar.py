"""
submitter.cancel_proposal com uma página falsa (sem Playwright). O link "Cancelar proposta"
só abre o modal de confirmação do site; quem cancela de fato é o "Sim" do modal (antes de
2026-10-29 o bot não clicava nele e a proposta continuava lá).
"""
from contextlib import contextmanager

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from bot import site_selectors as sel
from bot import submitter

URL = "https://www.99freelas.com.br/project/site-institucional-785400"


class FakePage:
    def __init__(self, tem_proposta: bool = True, modal_abre: bool = True):
        self.tem_proposta = tem_proposta
        self.modal_abre = modal_abre
        self.modal_aberto = False
        self.cliques = []

    def goto(self, *a, **k):
        self.modal_aberto = False

    def query_selector(self, selector):
        if selector == sel.PROPOSAL_CANCEL_LINK:
            return object() if self.tem_proposta else None
        return None

    def click(self, selector, **k):
        self.cliques.append(selector)
        if selector == sel.PROPOSAL_CANCEL_LINK:
            self.modal_aberto = self.modal_abre
        elif selector == sel.PROPOSAL_CANCEL_CONFIRM_BUTTON:
            assert self.modal_aberto, "clicou em Sim com o modal fechado"
            self.tem_proposta = False

    def wait_for_selector(self, selector, **k):
        if selector == sel.PROPOSAL_CANCEL_CONFIRM_BUTTON and not self.modal_aberto:
            raise PlaywrightTimeoutError("modal não abriu")

    @contextmanager
    def expect_navigation(self, **k):
        yield


def test_confirma_no_modal_e_cancela():
    page = FakePage()
    assert submitter.cancel_proposal(page, URL) == (True, "proposta cancelada")
    assert page.cliques == [sel.PROPOSAL_CANCEL_LINK, sel.PROPOSAL_CANCEL_CONFIRM_BUTTON]


def test_modal_nao_abre_nao_declara_sucesso():
    page = FakePage(modal_abre=False)
    ok, detail = submitter.cancel_proposal(page, URL)
    assert not ok and "modal" in detail
    assert sel.PROPOSAL_CANCEL_CONFIRM_BUTTON not in page.cliques


def test_sem_proposta_nao_clica_em_nada():
    page = FakePage(tem_proposta=False)
    ok, _ = submitter.cancel_proposal(page, URL)
    assert not ok and page.cliques == []
