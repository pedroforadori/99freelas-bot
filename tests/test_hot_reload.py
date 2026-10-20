"""
Hot reload (bot/hot_reload.py): o vigia só pede reinício depois do debounce e se o código
novo passar na validação; código quebrado não derruba o bot.
"""
import os

import pytest

from bot import hot_reload


@pytest.fixture
def repo(tmp_path, monkeypatch):
    bot_dir = tmp_path / "bot"
    (bot_dir / "__pycache__").mkdir(parents=True)
    (bot_dir / "a.py").write_text("x = 1\n")
    (bot_dir / "__pycache__" / "a.cpython-312.pyc").write_text("lixo")
    (tmp_path / "config.yaml").write_text("proposal: {}\n")
    monkeypatch.setattr(hot_reload, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(hot_reload, "BOT_DIR", str(bot_dir))
    monkeypatch.setattr(hot_reload, "EXTRA_FILES", [str(tmp_path / "config.yaml"), str(tmp_path / ".env")])
    monkeypatch.setattr(hot_reload, "_validar", lambda: None)
    return tmp_path


class Relogio:
    def __init__(self, monkeypatch):
        self.agora = 1000.0
        monkeypatch.setattr(hot_reload.time, "monotonic", lambda: self.agora)


_MTIME = [2_000_000_000]


def _altera(caminho, texto="y = 2\n"):
    caminho.write_text(texto)
    _MTIME[0] += 10  # mtime sempre novo — o relógio do Windows tem resolução de ~15ms
    os.utime(caminho, (_MTIME[0], _MTIME[0]))


def test_sem_alteracao_nao_recarrega(repo):
    assert hot_reload.CodeWatcher(debounce_seconds=0).check() is None


def test_recarrega_so_depois_do_debounce(repo, monkeypatch):
    relogio = Relogio(monkeypatch)
    watcher = hot_reload.CodeWatcher(debounce_seconds=5)
    _altera(repo / "bot" / "a.py")

    assert watcher.check() is None  # acabou de mudar
    relogio.agora += 3
    assert watcher.check() is None
    relogio.agora += 3
    assert watcher.check() == ["bot/a.py"]


def test_nova_alteracao_reinicia_o_debounce(repo, monkeypatch):
    relogio = Relogio(monkeypatch)
    watcher = hot_reload.CodeWatcher(debounce_seconds=5)
    _altera(repo / "bot" / "a.py")
    watcher.check()
    relogio.agora += 4
    (repo / "bot" / "b.py").write_text("z = 3\n")  # arquivo novo no meio
    assert watcher.check() is None
    relogio.agora += 4
    assert watcher.check() is None
    relogio.agora += 2
    assert watcher.check() == ["bot/a.py", "bot/b.py"]


def test_config_e_env_tambem_contam_e_pycache_nao(repo, monkeypatch):
    Relogio(monkeypatch)
    watcher = hot_reload.CodeWatcher(debounce_seconds=0)
    _altera(repo / "bot" / "__pycache__" / "a.cpython-312.pyc")
    watcher.check()
    assert watcher.check() is None

    _altera(repo / "config.yaml", "proposal: {valor_modo: manual}\n")
    (repo / ".env").write_text("HEADLESS=true\n")
    watcher.check()
    assert watcher.check() == [".env", "config.yaml"]


def test_codigo_quebrado_nao_recarrega_ate_corrigir(repo, monkeypatch):
    Relogio(monkeypatch)
    erros = ["o código novo não importa: SyntaxError"]
    monkeypatch.setattr(hot_reload, "_validar", lambda: erros[0])
    watcher = hot_reload.CodeWatcher(debounce_seconds=0)

    _altera(repo / "bot" / "a.py", "x = (\n")
    watcher.check()
    assert watcher.check() is None
    assert watcher.check() is None  # não revalida (nem avisa de novo) o mesmo estado

    erros[0] = None
    _altera(repo / "bot" / "a.py", "x = 2\n")
    watcher.check()
    assert watcher.check() == ["bot/a.py"]


def test_validar_pega_yaml_invalido(repo, monkeypatch):
    monkeypatch.undo()  # _validar de verdade, mas ainda apontando pro tmp
    monkeypatch.setattr(hot_reload, "BASE_DIR", str(repo))
    (repo / "config.yaml").write_text("proposal: [\n")
    assert "config.yaml inválido" in hot_reload._validar()


@pytest.mark.skipif(not os.path.exists(os.path.join(hot_reload.BASE_DIR, "config.yaml")), reason="sem config.yaml")
def test_validar_aceita_o_codigo_atual_do_repo():
    assert hot_reload._validar() is None
