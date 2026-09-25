"""
Importa os pontos de entrada num processo novo, do jeito que eles rodam de verdade
(`python bot/x.py` → a pasta bot/ fica em sys.path[0]). Pega import circular e colisão de
nome com módulo da stdlib/terceiros (já aconteceu com selectors.py).
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY_POINTS = ["main", "dry_run", "report", "portfolio", "import_cookies"]


@pytest.mark.parametrize("script", ENTRY_POINTS)
def test_script_importa(script):
    code = (
        "import runpy, sys;"
        f"sys.path.insert(0, {os.path.join(ROOT, 'bot')!r});"
        f"runpy.run_path({os.path.join(ROOT, 'bot', script + '.py')!r}, run_name='not_main')"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize("module", ["bot.submitter", "bot.messages", "bot.notifier", "bot.approvals"])
def test_modulo_importa_primeiro(module):
    """Cada módulo importado sozinho, primeiro, num processo limpo (ordem de import expõe ciclos)."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
