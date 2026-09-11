"""Transcrição local de notas de voz com faster-whisper.
"""

import logging
from pathlib import Path

from faster_whisper import WhisperModel

import config

log = logging.getLogger(__name__)


class TranscricaoError(RuntimeError):
    pass


_modelo: WhisperModel | None = None


def _carregar() -> WhisperModel:

    global _modelo
    if _modelo is None:
        log.info("carregando modelo whisper %s...", config.WHISPER_MODEL)
        _modelo = WhisperModel(
            config.WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
            download_root=str(config.MODELS_DIR),
        )
        log.info("modelo whisper pronto")
    return _modelo


def transcrever(caminho_audio: str | Path) -> str:
    """Transcreve um .ogg do Telegram."""
    try:
        segmentos, _info = _carregar().transcribe(
            str(caminho_audio), language="pt", beam_size=5
        )
        texto = " ".join(s.text.strip() for s in segmentos).strip()
    except Exception as exc:
        raise TranscricaoError(f"falha ao transcrever o áudio ({exc})") from exc

    if not texto:
        raise TranscricaoError("não consegui entender o áudio")

    log.info("transcrição: %s", texto)
    return texto
