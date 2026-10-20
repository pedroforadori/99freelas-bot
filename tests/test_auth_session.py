"""auth.save_session: a sessão renovada pelo site vai pro disco (senão um reinício usa cookies velhos)."""
import json
from types import SimpleNamespace

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
