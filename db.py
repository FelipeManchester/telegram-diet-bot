"""Escrita no Supabase.

O cliente supabase-py é síncrono — chame `salvar_refeicao` de dentro de
`asyncio.to_thread()` pra não travar o event loop do bot.
"""

import logging
from datetime import datetime

from supabase import Client, create_client

import config

log = logging.getLogger(__name__)

TABELA = "refeicoes"

_cliente: Client = create_client(config.SUPABASE_URL, config.SUPABASE_SECRET_KEY)

def _tabela():
    """.schema() em vez do default "public": o projeto Supabase é compartilhado
    com outros trabalhos seus, e o schema "dieta" isola este bot."""
    return _cliente.schema(config.SUPABASE_SCHEMA).table(TABELA)


class DBError(RuntimeError):
    pass


def salvar_refeicao(
    origem: str,
    entrada_bruta: str,
    extracao: dict,
    criado_em: datetime | None = None,
) -> dict:
    """Grava uma refeição e devolve a linha criada.

    `extracao` é o dict já validado que veio do claude_client.

    `criado_em` é a hora em que a MENSAGEM foi enviada, não a hora em que o bot
    processou. As duas divergem quando o PC ficou desligado: o Telegram segura os
    updates por 24h e entrega tudo de uma vez quando o bot volta. Sem isso, uma
    refeição de sábado entraria no total de segunda.
    """
    linha = {
        "origem": origem,
        "entrada_bruta": entrada_bruta,
        "itens": extracao["itens"],
        "calorias": extracao["calorias"],
        "proteina_g": extracao["proteina_g"],
        "carboidrato_g": extracao["carboidrato_g"],
        "gordura_g": extracao["gordura_g"],
    }
    if criado_em is not None:
        linha["criado_em"] = criado_em.isoformat()

    try:
        resposta = _tabela().insert(linha).execute()
    except Exception as exc:
        raise DBError(f"falha ao salvar no banco ({exc})") from exc

    if not resposta.data:
        raise DBError("o insert não retornou nenhuma linha")

    log.info("refeição salva: id=%s origem=%s", resposta.data[0].get("id"), origem)
    return resposta.data[0]
