"""
Hot reload (2026-10-15, pedido do usuário): alterou um .py de bot/, o config.yaml ou o .env,
o bot se reinicia sozinho com o código novo — sem `docker compose restart` nem Ctrl+C.

Não é importlib.reload: o processo inteiro é substituído (os.execv no Linux/Docker; no
Windows um processo filho, ver restart), então não sobra estado velho em memória. O que
importa já vive em data/ (approvals, fila de mensagens, applied_jobs) e sobrevive igual a
um reinício normal. main.py só checa entre ticks do loop interno — nunca no meio de um
envio — e o reinício passa pelo mesmo caminho de fechar o navegador de uma parada normal.

Travas pra não derrubar o bot com uma edição pela metade:
- espera HOT_RELOAD_DEBOUNCE_SECONDS sem nenhuma alteração nova (várias edições seguidas
  viram um reinício só, e não reinicia com só metade de uma mudança de vários arquivos salva);
- antes de reiniciar, importa main.py num processo separado (mesmo teste de
  tests/test_imports.py) e lê o config.yaml — erro de sintaxe/import/YAML vira um aviso
  (chega no Telegram pelo TelegramErrorHandler) e o bot segue com o código antigo até corrigir.

Dependência nova (requirements.txt) ainda precisa de rebuild da imagem. No Docker, a pasta
bot/ precisa estar montada no container (ver docker-compose.yml) — senão o container só
enxerga o código copiado no build.
"""
import logging
import os
import subprocess
import sys
import time

import yaml

from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DIR = os.path.join(BASE_DIR, "bot")
EXTRA_FILES = [os.path.join(BASE_DIR, "config.yaml"), os.path.join(BASE_DIR, ".env")]

# Variável que o processo novo recebe com os arquivos alterados — main.py troca o
# "Bot online" por "Bot recarregado" listando eles.
RELOADED_ENV = "BOT_HOT_RELOADED_FILES"

# Ambiente de antes do load_dotenv: o processo novo recebe ESTE, pra um .env alterado valer
# de verdade (load_dotenv não sobrescreve variável que já existe no ambiente herdado).
_ENV_ORIGINAL = dict(os.environ)


def enabled() -> bool:
    return os.environ.get("HOT_RELOAD", "true").lower() == "true"


def _snapshot() -> dict[str, tuple[int, int]]:
    """Caminho -> (mtime em ns, tamanho) de cada arquivo vigiado."""
    arquivos = {}
    for raiz, dirs, nomes in os.walk(BOT_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for nome in nomes:
            if nome.endswith(".py"):
                arquivos[os.path.join(raiz, nome)] = None
    for caminho in EXTRA_FILES:
        arquivos[caminho] = None
    for caminho in list(arquivos):
        try:
            st = os.stat(caminho)
            arquivos[caminho] = (st.st_mtime_ns, st.st_size)
        except OSError:
            del arquivos[caminho]
    return arquivos


def _validar() -> str | None:
    """Motivo pra NÃO recarregar (código/config quebrado), ou None se pode."""
    config = os.path.join(BASE_DIR, "config.yaml")
    try:
        with open(config, "r", encoding="utf-8") as f:
            yaml.safe_load(f)
    except FileNotFoundError:
        return "config.yaml não encontrado"
    except yaml.YAMLError as e:
        return f"config.yaml inválido: {e}"

    code = (
        "import runpy, sys;"
        f"sys.path.insert(0, {BOT_DIR!r});"
        f"runpy.run_path({os.path.join(BOT_DIR, 'main.py')!r}, run_name='not_main')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=BASE_DIR, capture_output=True, text=True, timeout=120
        )
    except subprocess.TimeoutExpired:
        return "importar o código novo demorou demais"
    if result.returncode != 0:
        linhas = result.stderr.strip().splitlines()
        return "o código novo não importa: " + " | ".join(linhas[-3:])
    return None


class CodeWatcher:
    def __init__(self, debounce_seconds: float | None = None):
        if debounce_seconds is None:
            debounce_seconds = float(os.environ.get("HOT_RELOAD_DEBOUNCE_SECONDS", 5))
        self._debounce = debounce_seconds
        self._base = _snapshot()
        self._visto: dict | None = None  # alteração ainda "assentando"
        self._visto_em = 0.0
        self._rejeitado: dict | None = None  # já validado e reprovado

    def check(self) -> list[str] | None:
        """
        Arquivos alterados (relativos à raiz do repo) quando já dá pra recarregar; None se
        nada mudou, se ainda está dentro do debounce ou se o código novo não passou na validação.
        """
        atual = _snapshot()
        if atual == self._base or atual == self._rejeitado:
            return None
        if atual != self._visto:
            self._visto, self._visto_em = atual, time.monotonic()
            return None
        if time.monotonic() - self._visto_em < self._debounce:
            return None

        alterados = sorted(
            os.path.relpath(p, BASE_DIR).replace(os.sep, "/")
            for p in set(atual) | set(self._base)
            if atual.get(p) != self._base.get(p)
        )
        erro = _validar()
        if erro:
            log.warning("Hot reload adiado (%s) — seguindo com o código antigo até corrigir. %s",
                        ", ".join(alterados), erro)
            self._rejeitado = atual
            return None
        return alterados


def restart(alterados: list[str]) -> None:
    """
    Substitui o processo atual por um novo rodando o mesmo comando. Chamar só DEPOIS de
    fechar o navegador. No Windows os.execv não substitui o processo de verdade (o terminal
    volta e o bot fica solto em segundo plano), então o processo atual vira só um "pai" que
    espera o novo terminar e repassa o código de saída.
    """
    env = {**_ENV_ORIGINAL, RELOADED_ENV: ", ".join(alterados)}
    argv = [sys.executable, *sys.argv]
    log.info("Hot reload: reiniciando com o código novo (%s).", ", ".join(alterados))
    logging.shutdown()  # descarrega os logs antes de trocar de processo

    if os.name != "nt":
        os.execve(sys.executable, argv, env)

    proc = subprocess.Popen(argv, env=env)
    while True:
        try:
            sys.exit(proc.wait())
        except KeyboardInterrupt:
            # Ctrl+C chega nos dois processos do console: o filho faz a parada limpa, o
            # pai só espera ele terminar.
            continue
