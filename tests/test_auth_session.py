"""Sessão do 99Freelas: save_session (cookies renovados vão pro disco) e queda/retomada com o bot rodando."""
import json
from types import SimpleNamespace

import pytest

from bot import submitter
from bot.sources.freelas99 import auth


def _page(estado):
    return SimpleNamespace(context=SimpleNamespace(storage_state=lambda: estado))


def test_grava_a_sessao_atual(monkeypatch):
    monkeypatch.setattr(submitter, "is_logged_in", lambda page: True)
    estado = {"cookies": [{"name": "sessao", "value": "nova"}], "origins": []}
    assert auth.save_session(_page(estado)) is True
    with open(auth.AUTH_STATE_PATH, encoding="utf-8") as f:
        assert json.load(f) == estado


def test_deslogado_nao_sobrescreve(monkeypatch):
    with open(auth.AUTH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": [{"name": "sessao", "value": "boa"}]}, f)
    monkeypatch.setattr(submitter, "is_logged_in", lambda page: False)
    assert auth.save_session(_page({"cookies": []})) is False
    with open(auth.AUTH_STATE_PATH, encoding="utf-8") as f:
        assert json.load(f)["cookies"][0]["value"] == "boa"


def test_erro_nunca_derruba_o_ciclo(monkeypatch):
    monkeypatch.setattr(submitter, "is_logged_in", lambda page: (_ for _ in ()).throw(RuntimeError("boom")))
    assert auth.save_session(object()) is False


# --- sessão derrubada pelo site com o bot rodando --------------------------------------------

from bot import connections, scraper  # noqa: E402
from bot.sources import registry  # noqa: E402

FREELAS99 = registry.source_of({"source": "99freelas"})


@pytest.fixture
def deslogado(api, monkeypatch):
    estado = {"logado": False, "listagens": 0, "reaberturas": 0}
    monkeypatch.setattr(FREELAS99, "_sem_sessao", False)
    monkeypatch.setattr(FREELAS99, "_session_mtime_na_queda", None)
    monkeypatch.setattr(connections, "refresh", lambda page: pytest.fail("dashboard deslogado"))

    def fetch(page):
        estado["listagens"] += 1
        return []

    def reopen(page):
        estado["reaberturas"] += 1
        return "page-nova", estado["logado"]

    monkeypatch.setattr(scraper, "fetch_open_projects", fetch)
    monkeypatch.setattr(auth, "reopen_session", reopen)
    api.patch_submitter("is_logged_in", lambda page: estado["logado"])
    return estado


def _status(telegram):
    return [p["text"] for p in telegram.of("sendMessage")]


def test_sessao_caiu_avisa_uma_vez_e_para_de_usar_o_site(api, telegram, deslogado):
    api.run_cycle({})
    api.run_cycle({})
    FREELAS99.tick({})  # mensagens/outbox não rodam deslogado
    textos = _status(telegram)
    assert len(textos) == 1 and "Sessão do 99Freelas caiu" in textos[0]
    assert deslogado["listagens"] == 1  # 2º ciclo nem abre o site: espera o arquivo mudar
    ok, detail = FREELAS99.deliver({"project": {"id": "1", "title": "x"}, "proposal": {}})
    assert ok is False and "sessão" in detail


def test_cookies_novos_restauram_sem_reiniciar(api, telegram, deslogado, monkeypatch):
    api.run_cycle({})
    deslogado["logado"] = True
    monkeypatch.setattr(connections, "refresh", lambda page: None)
    with open(auth.AUTH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": [], "origins": []}, f)  # import_cookies.py rodou
    api.run_cycle({})
    assert deslogado["reaberturas"] == 1
    assert FREELAS99.page == "page-nova" and FREELAS99._sem_sessao is False
    assert "Sessão do 99Freelas de volta" in _status(telegram)[-1]


def test_cookies_novos_ainda_deslogados_avisam(api, telegram, deslogado):
    api.run_cycle({})
    with open(auth.AUTH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": [], "origins": []}, f)
    api.run_cycle({})
    api.run_cycle({})  # mesmo arquivo: não tenta de novo
    assert deslogado["reaberturas"] == 1
    assert "também não estão logados" in _status(telegram)[-1]


def test_aceita_export_cru_do_cookie_editor():
    with open(auth.AUTH_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump([
            {"name": "JSESSIONID", "value": "x", "domain": "www.99freelas.com.br", "session": True},
            {"name": "fr", "value": "y", "domain": ".facebook.com", "expirationDate": 1},
        ], f)
    estado = auth._load_storage_state()
    assert [c["name"] for c in estado["cookies"]] == ["JSESSIONID"]
    assert estado["cookies"][0]["expires"] == -1
