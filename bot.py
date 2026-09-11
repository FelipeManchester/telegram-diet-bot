"""Bot de dieta no Telegram — handlers e orquestração.

Fluxo: mensagem -> filtro de user_id -> (transcrição, se áudio) -> extração pelo
Claude CLI -> gravação no Supabase -> confirmação formatada em Python.
"""

import asyncio
import logging
import shutil
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import claude_client
import config
import db
import resumo
import transcriber

# Rotação: serviço 24/7 com FileHandler simples cresce até encher o disco.
# 5 arquivos de 2MB = 10MB de teto.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(
            config.LOG_FILE, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        ),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Só o dono. Handlers registrados com este filtro nem chegam a ser chamados por
# outra conta — nenhuma resposta, nada no banco, bot invisível pra quem
# descobrir o username por acaso.
SO_EU = filters.User(user_id=config.TELEGRAM_USER_ID)


def _autorizado(update: Update) -> bool:
    """Defesa em profundidade: revalida mesmo com o filtro já aplicado, pro caso
    de algum handler novo ser registrado sem SO_EU."""
    usuario = update.effective_user
    if usuario is None or usuario.id != config.TELEGRAM_USER_ID:
        log.debug("mensagem ignorada de user_id=%s", usuario.id if usuario else None)
        return False
    return True


async def _com_retry(operacao, descricao: str, tentativas: int = 3):
    """Repete uma chamada à API do Telegram que caiu por rede.

    O PTB só faz retry sozinho no loop de getUpdates; download de foto e envio
    de resposta falham de primeira. Logo depois de um boot, com a rede ainda
    subindo, um ReadTimeout de 20s derrubava a refeição inteira — foi o que
    aconteceu com a foto reenviada. BadRequest herda de NetworkError mas é erro
    de conteúdo, não de rede: repetir não muda nada.
    """
    for tentativa in range(1, tentativas + 1):
        try:
            return await operacao()
        except BadRequest:
            raise
        except NetworkError as exc:
            if tentativa == tentativas:
                raise
            espera = 2 ** (tentativa - 1)
            log.warning(
                "%s falhou (%s); tentativa %s/%s, repetindo em %ss",
                descricao, exc, tentativa, tentativas, espera,
            )
            await asyncio.sleep(espera)


async def _sinalizar_digitando(update: Update) -> None:
    """O 'digitando...' é cosmético: nunca deve derrubar o registro.

    Era a primeira chamada de rede de cada handler, então qualquer engasgo de
    conexão matava a mensagem antes mesmo do download começar.
    """
    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except TelegramError as exc:
        log.debug("send_action falhou, seguindo assim mesmo: %s", exc)


def _formatar_confirmacao(extracao: dict) -> str:
    """Monta a resposta a partir dos dados estruturados — o Claude não formata nada."""
    nomes = ", ".join(item["nome"] for item in extracao["itens"])
    return (
        f"✅ Refeição registrada: {nomes}.\n"
        f"{extracao['calorias']:.0f} kcal · "
        f"{extracao['proteina_g']:.1f}g prot · "
        f"{extracao['gordura_g']:.1f}g gord · "
        f"{extracao['carboidrato_g']:.1f}g carb"
    )


async def _registrar(update: Update, origem: str, entrada_bruta: str, extracao: dict) -> None:
    # update.message.date é quando VOCÊ mandou, não quando o bot processou.
    # Importa quando o PC passou um tempo desligado e o Telegram entregou a fila
    # de uma vez só.
    await asyncio.to_thread(
        db.salvar_refeicao, origem, entrada_bruta, extracao, update.message.date
    )
    # Já está no banco: vale insistir na confirmação em vez de deixar você
    # achando que a refeição se perdeu.
    texto = _formatar_confirmacao(extracao)
    try:
        await _com_retry(
            lambda: update.message.reply_text(texto), "envio da confirmação"
        )
    except TelegramError:
        # A refeição ESTÁ no banco. Deixar o erro subir faria o handler
        # responder "não consegui registrar" por uma confirmação perdida —
        # mentira que levaria você a registrar a mesma refeição duas vezes.
        log.exception("refeição salva, mas a confirmação não chegou ao Telegram")


async def _baixar(update: Update, file_id: str, nome: str) -> Path:
    """Baixa o anexo num diretório próprio dentro de tmp/.

    Um diretório por mensagem, contendo só esse arquivo: no fluxo de foto ele
    vira o working directory da chamada ao Claude, e é o único lugar que a tool
    Read alcança. Ver a nota de segurança em claude_client.py.
    """
    arquivo = await _com_retry(
        lambda: update.get_bot().get_file(file_id), "get_file"
    )
    pasta = config.TMP_DIR / uuid.uuid4().hex
    pasta.mkdir()
    destino = pasta / nome
    await _com_retry(
        lambda: arquivo.download_to_drive(destino), f"download de {nome}"
    )
    return destino


async def handler_texto(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return

    texto = update.message.text
    try:
        await _sinalizar_digitando(update)
        extracao = await claude_client.extrair_de_texto(texto)
        await _registrar(update, "texto", texto, extracao)
    except (claude_client.ClaudeError, db.DBError) as exc:
        await _responder_erro(update, exc)
    except NetworkError:
        log.exception("rede falhou no fluxo de texto")
        await _responder_erro(
            update, RuntimeError("Telegram fora de alcance. Manda de novo.")
        )
    except Exception:
        log.exception("erro inesperado no fluxo de texto")
        await _responder_erro(update, RuntimeError("erro inesperado (veja bot.log)"))


async def handler_audio(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return

    anexo = update.message.voice or update.message.audio
    caminho: Path | None = None
    try:
        await _sinalizar_digitando(update)
        caminho = await _baixar(update, anexo.file_id, "audio.ogg")

        # Whisper é síncrono e pesado em CPU: fora do event loop.
        transcricao = await asyncio.to_thread(transcriber.transcrever, caminho)

        # Daqui pra frente é idêntico ao fluxo de texto — o Claude nunca vê áudio.
        extracao = await claude_client.extrair_de_texto(transcricao)
        await _registrar(update, "audio", transcricao, extracao)
    except (
        transcriber.TranscricaoError,
        claude_client.ClaudeError,
        db.DBError,
    ) as exc:
        await _responder_erro(update, exc)
    except NetworkError:
        log.exception("rede falhou no fluxo de áudio")
        await _responder_erro(
            update, RuntimeError("Telegram fora de alcance. Manda de novo.")
        )
    except Exception:
        log.exception("erro inesperado no fluxo de áudio")
        await _responder_erro(update, RuntimeError("erro inesperado (veja bot.log)"))
    finally:
        _limpar(caminho)


async def handler_foto(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return

    legenda = update.message.caption
    caminho: Path | None = None
    try:
        await _sinalizar_digitando(update)
        # photo[-1] é a maior resolução disponível.
        caminho = await _baixar(update, update.message.photo[-1].file_id, "foto.jpg")

        extracao = await claude_client.extrair_de_foto(caminho, legenda)
        await _registrar(
            update, "foto", legenda or "[foto sem legenda]", extracao
        )
    except (claude_client.ClaudeError, db.DBError) as exc:
        await _responder_erro(update, exc)
    except NetworkError:
        log.exception("rede falhou no fluxo de foto")
        await _responder_erro(
            update, RuntimeError("Telegram fora de alcance. Manda de novo.")
        )
    except Exception:
        log.exception("erro inesperado no fluxo de foto")
        await _responder_erro(update, RuntimeError("erro inesperado (veja bot.log)"))
    finally:
        _limpar(caminho)


async def _responder_resumo(update: Update, periodo: str) -> None:
    """Caminho dos dois comandos: nada de Claude, só soma do que está no banco."""
    if not _autorizado(update):
        return

    try:
        await _sinalizar_digitando(update)
        # supabase-py é síncrono; fora do event loop, como na escrita.
        dados = await asyncio.to_thread(resumo.resumir, periodo)
        texto = resumo.formatar(dados)
        await _com_retry(
            lambda: update.message.reply_text(texto), f"envio do resumo {periodo}"
        )
    except db.DBError as exc:
        await _responder_erro(update, exc, acao="consultar")
    except NetworkError:
        log.exception("rede falhou no resumo %s", periodo)
        await _responder_erro(
            update, RuntimeError("Telegram fora de alcance. Manda de novo."),
            acao="consultar",
        )
    except Exception:
        log.exception("erro inesperado no resumo %s", periodo)
        await _responder_erro(
            update, RuntimeError("erro inesperado (veja bot.log)"), acao="consultar"
        )


async def handler_hoje(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _responder_resumo(update, resumo.HOJE)


async def handler_semana(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _responder_resumo(update, resumo.SEMANA)


async def _responder_erro(update: Update, exc: Exception, acao: str = "registrar") -> None:
    log.warning("falha ao %s: %s", acao, exc)
    try:
        await _com_retry(
            lambda: update.message.reply_text(f"❌ Não consegui {acao}: {exc}"),
            "envio da mensagem de erro",
        )
    except TelegramError:
        # Sem rede não há como avisar. Fica no log e a mensagem não vira
        # exceção não tratada no handler.
        log.exception("nem a mensagem de erro conseguiu sair")


def _limpar(caminho: Path | None) -> None:
    """Apaga o diretório da mensagem inteiro, não só o arquivo."""
    if caminho is not None:
        shutil.rmtree(caminho.parent, ignore_errors=True)


async def _registrar_menu(app: Application) -> None:
    """Põe os comandos no menu do Telegram (o "/" do teclado).

    Best-effort: é conveniência de interface, não vale derrubar a subida do bot
    se a rede estiver ruim bem na hora do boot.
    """
    try:
        await app.bot.set_my_commands(
            [
                ("hoje", "Consumo de hoje"),
                ("semana", "Consumo dos últimos 7 dias (sem contar hoje)"),
            ]
        )
    except TelegramError as exc:
        log.warning("não consegui registrar o menu de comandos: %s", exc)


def main() -> None:
    # Defaults do PTB são 5s de connect/read/write. Logo depois de um boot,
    # com o Wi-Fi ainda negociando, 5s não é o bastante: foi um ReadTimeout
    # desses que derrubou a primeira foto reenviada. media_write cobre o upload
    # interno de arquivos; get_updates_read é o long polling em si.
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .connect_timeout(20.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .media_write_timeout(120.0)
        .get_updates_read_timeout(40.0)
        .build()
    )

    app.add_handler(MessageHandler(SO_EU & filters.TEXT & ~filters.COMMAND, handler_texto))
    app.add_handler(MessageHandler(SO_EU & (filters.VOICE | filters.AUDIO), handler_audio))
    app.add_handler(MessageHandler(SO_EU & filters.PHOTO, handler_foto))
    app.add_handler(CommandHandler("hoje", handler_hoje, filters=SO_EU))
    app.add_handler(CommandHandler("semana", handler_semana, filters=SO_EU))
    app.post_init = _registrar_menu

    log.info("bot no ar (long polling), autorizado: %s", config.TELEGRAM_USER_ID)
    # Long polling: sem webhook, sem porta exposta.
    # drop_pending_updates=False: ao subir, processa a fila que o Telegram
    # segurou enquanto o PC estava no Windows ou desligado (retenção de 24h).
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
