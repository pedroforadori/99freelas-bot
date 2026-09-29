"""
Ramos de proposta promovida em submitter.finalize_submission, com uma página falsa (sem
Playwright). Só um freelancer por projeto pode promover: quando alguém já promoveu, o
checkbox #highlight-bid deixa de ficar visível.
"""
from contextlib import contextmanager

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from bot import site_selectors as sel
from bot import submitter
from tests.conftest import project_99, proposal_99


class _Checkbox:
    def __init__(self, visible: bool):
        self.visible = visible

    def is_visible(self):
        return self.visible

    def is_disabled(self):
        return False


class FakePage:
    def __init__(self, checkbox_visible: bool | None, checked: bool = False):
        self.checkbox = None if checkbox_visible is None else _Checkbox(checkbox_visible)
        self.checked = checked
        self.submitted = False

    def goto(self, *a, **k):
        pass

    def query_selector(self, selector):
        if selector == sel.PROPOSAL_HIGHLIGHT_CHECKBOX:
            return self.checkbox
        if selector == sel.PROPOSAL_SUCCESS_MARKER:
            return object() if self.submitted else None
        return None

    @contextmanager
    def expect_navigation(self, **k):
        yield

    def click(self, selector, **k):
        if selector == sel.PROPOSAL_SUBMIT_BUTTON:
            self.submitted = True

    def fill(self, selector, value):
        pass

    def set_checked(self, selector, value, force=False, timeout=None):
        if not self.checkbox.visible:
            if not force:
                raise PlaywrightTimeoutError("checkbox escondido")
            return  # clique forçado num elemento escondido não muda nada
        self.checked = value

    def is_checked(self, selector):
        return self.checked

    def wait_for_selector(self, *a, **k):
        pass


def test_promovida_disponivel_marca_e_envia(telegram):
    page = FakePage(checkbox_visible=True)
    ok, detail = submitter.finalize_submission(page, project_99(), proposal_99(promovida=True))
    assert (ok, detail) == (True, "proposta enviada com sucesso")
    assert page.checked and page.submitted


def test_promovida_sem_media_perdida_nao_envia(telegram):
    page = FakePage(checkbox_visible=False)
    proposal = proposal_99(promovida=True, promocao_sem_media=True)
    ok, detail = submitter.finalize_submission(page, project_99(), proposal)
    assert (ok, detail) == (False, submitter.PROMOCAO_INDISPONIVEL)
    assert not page.submitted
    assert not telegram.of("sendMessage")  # não é falha de envio: quem chama avisa


def test_promovida_com_media_perdida_envia_sem_destaque(telegram):
    page = FakePage(checkbox_visible=None)  # checkbox nem existe mais
    proposal = proposal_99(promovida=True)
    ok, detail = submitter.finalize_submission(page, project_99(), proposal)
    assert ok and page.submitted
    assert "sem destaque" in detail
    assert proposal["promovida"] is False and proposal["promocao_perdida"] is True


def test_nao_promovida_desmarca(telegram):
    page = FakePage(checkbox_visible=True, checked=True)
    ok, _ = submitter.finalize_submission(page, project_99(), proposal_99())
    assert ok and page.submitted and not page.checked
