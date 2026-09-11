"""Configuração do bot, lida do .env e validada no import.
"""

from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import os
import shutil

RAIZ = Path(__file__).resolve().parent
TMP_DIR = RAIZ / "tmp"
MODELS_DIR = RAIZ / "models"
LOG_FILE = RAIZ / "bot.log"

load_dotenv(RAIZ / ".env")


class ConfigError(RuntimeError):
    pass


def _obrigatorio(nome: str) -> str:
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise ConfigError(
            f"Variável {nome} ausente ou vazia. Copie .env.example para .env e preencha."
        )
    return valor


TELEGRAM_BOT_TOKEN = _obrigatorio("TELEGRAM_BOT_TOKEN")

try:
    TELEGRAM_USER_ID = int(_obrigatorio("TELEGRAM_USER_ID"))
except ValueError as exc:
    raise ConfigError("TELEGRAM_USER_ID precisa ser um número inteiro.") from exc

SUPABASE_URL = _obrigatorio("SUPABASE_URL")
SUPABASE_SECRET_KEY = _obrigatorio("SUPABASE_SECRET_KEY")
# Schema dedicado: o banco é compartilhado com outros projetos seus.
SUPABASE_SCHEMA = os.getenv("SUPABASE_SCHEMA", "dieta").strip()

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet").strip()


def _resolver_claude_bin() -> str:
    """Caminho absoluto do CLI do Claude.
    """
    configurado = os.getenv("CLAUDE_BIN", "").strip()
    if configurado:
        caminho = Path(configurado).expanduser()
        if not os.access(caminho, os.X_OK):
            raise ConfigError(f"CLAUDE_BIN={configurado} não é um executável.")
        return str(caminho)

    encontrado = shutil.which("claude") or shutil.which(
        "claude", path=str(Path.home() / ".local" / "bin")
    )
    if not encontrado:
        raise ConfigError(
            "Executável `claude` não encontrado no PATH nem em ~/.local/bin. "
            "Instale o Claude Code ou aponte CLAUDE_BIN no .env."
        )
    return encontrado


CLAUDE_BIN = _resolver_claude_bin()
CLAUDE_TIMEOUT_SEC = int(os.getenv("CLAUDE_TIMEOUT_SEC", "120"))

# O banco guarda criado_em em UTC (timestamptz). "Hoje" só faz sentido no seu
# fuso: sem isto, todo jantar depois das 21h cairia no dia seguinte.
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo").strip())

TMP_DIR.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
