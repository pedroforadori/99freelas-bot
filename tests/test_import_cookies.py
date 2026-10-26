"""import_cookies.py: o mesmo importador serve qualquer site registrado em SITES (--site)."""
import json
import sys
from contextlib import nullcontext

from bot import import_cookies


def test_importa_so_os_cookies_do_site_escolhido(monkeypatch, tmp_path):
    destino = tmp_path / "outro_auth_state.json"
    validadas = []
    site = import_cookies.CookieSite("outro.com", str(destino), lambda p, path: validadas.append(path) or True)
    monkeypatch.setitem(import_cookies.SITES, "outro", site)
    monkeypatch.setattr(import_cookies, "sync_playwright", lambda: nullcontext("playwright"))

    export = tmp_path / "export.json"
    export.write_text(json.dumps([
        {"name": "sessao", "value": "x", "domain": ".outro.com", "session": True},
        {"name": "JSESSIONID", "value": "y", "domain": "www.99freelas.com.br", "session": True},
    ]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["import_cookies.py", "--site", "outro", str(export)])

    import_cookies.main()

    estado = json.loads(destino.read_text(encoding="utf-8"))
    assert [c["name"] for c in estado["cookies"]] == ["sessao"]
    assert validadas == [str(destino)]  # validou com a sessão recém-gravada


def test_workana_tem_sessao_propria():
    assert import_cookies.SITES["workana"].auth_state_path.endswith("workana_auth_state.json")
    assert import_cookies.SITES["workana"].auth_state_path != import_cookies.AUTH_STATE_PATH


def test_99freelas_continua_o_padrao():
    assert import_cookies.SITES["99freelas"].auth_state_path == import_cookies.AUTH_STATE_PATH
    assert import_cookies._convert_cookie({"name": "a", "value": "b", "domain": ".google.com"}) is None
