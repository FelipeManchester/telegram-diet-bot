"""Resumos de consumo — as janelas de tempo e os totais.

Sem Claude: os macros já estão estruturados no banco, somar é `sum()`. Chamar o
modelo aqui só somaria latência, cota e risco de erro numa conta exata.

O módulo é síncrono (o supabase-py também é) — o bot chama via
`asyncio.to_thread()`. Também roda direto no terminal:

    python resumo.py            # hoje
    python resumo.py semana     # últimos 7 dias, sem contar hoje
"""

import logging
from datetime import date, datetime, time, timedelta

import config
import db

log = logging.getLogger(__name__)

HOJE = "hoje"
SEMANA = "semana"

# Quantos dias fechados o /semana cobre. Termina na meia-noite de hoje, então o
# dia em curso nunca entra na conta.
DIAS_DA_SEMANA = 7


class PeriodoInvalido(ValueError):
    pass


def _meia_noite(dia: date) -> datetime:
    """00:00 do dia, no seu fuso.

    `combine` com tzinfo em vez de `.replace()` num datetime já existente: o
    replace mantém o offset antigo, que estaria errado se o fuso mudasse de
    regra entre as duas datas.
    """
    return datetime.combine(dia, time.min, tzinfo=config.TIMEZONE)


def janela(periodo: str, agora: datetime | None = None) -> tuple[datetime, datetime]:
    """Início (inclusivo) e fim (exclusivo) do período, no fuso local.

    O fim exclusivo é sempre uma meia-noite: pegar até 23:59:59 deixaria de fora
    o último segundo do dia.
    """
    agora = agora or datetime.now(config.TIMEZONE)
    hoje = agora.astimezone(config.TIMEZONE).date()

    if periodo == HOJE:
        return _meia_noite(hoje), _meia_noite(hoje + timedelta(days=1))
    if periodo == SEMANA:
        # Termina na meia-noite de hoje: os 7 dias são todos fechados, e o dia
        # em curso fica de fora por definição.
        return _meia_noite(hoje - timedelta(days=DIAS_DA_SEMANA)), _meia_noite(hoje)
    raise PeriodoInvalido(f"período desconhecido: {periodo!r}")


def resumir(periodo: str, agora: datetime | None = None) -> dict:
    """Totais do período. `refeicoes` é a contagem, não a lista."""
    inicio, fim = janela(periodo, agora)
    linhas = db.buscar_refeicoes(inicio, fim)

    # numeric do Postgres chega como int ou float conforme o valor; float()
    # normaliza antes de somar.
    totais = {
        campo: sum(float(linha[campo]) for linha in linhas)
        for campo in db.CAMPOS_MACRO
    }
    log.info(
        "resumo %s: %s refeições entre %s e %s",
        periodo, len(linhas), inicio.date(), fim.date(),
    )
    return {
        "periodo": periodo,
        "inicio": inicio,
        # O fim é exclusivo; o último dia coberto é o anterior a ele.
        "ultimo_dia": (fim - timedelta(days=1)).date(),
        "refeicoes": len(linhas),
        **totais,
    }


def _macros(dados: dict) -> str:
    return (
        f"{dados['calorias']:.0f} kcal · "
        f"{dados['proteina_g']:.1f}g prot · "
        f"{dados['gordura_g']:.1f}g gord · "
        f"{dados['carboidrato_g']:.1f}g carb"
    )


def formatar(dados: dict) -> str:
    if dados["periodo"] == HOJE:
        if not dados["refeicoes"]:
            return "Nada registrado hoje ainda."
        return f"📊 Hoje você consumiu {_macros(dados)}."

    periodo = (
        f"{dados['inicio']:%d/%m} a {dados['ultimo_dia']:%d/%m}"
    )
    if not dados["refeicoes"]:
        return f"Nada registrado de {periodo}."
    return f"📊 De {periodo} ({DIAS_DA_SEMANA} dias) você consumiu {_macros(dados)}."


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    escolhido = sys.argv[1] if len(sys.argv) > 1 else HOJE
    print(formatar(resumir(escolhido)))
