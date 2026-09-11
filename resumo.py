import logging
from datetime import date, datetime, time, timedelta

import config
import db

log = logging.getLogger(__name__)

HOJE = "hoje"
SEMANA = "semana"


DIAS_DA_SEMANA = 7


class PeriodoInvalido(ValueError):
    pass


def _meia_noite(dia: date) -> datetime:

    return datetime.combine(dia, time.min, tzinfo=config.TIMEZONE)


def janela(periodo: str, agora: datetime | None = None) -> tuple[datetime, datetime]:

    agora = agora or datetime.now(config.TIMEZONE)
    hoje = agora.astimezone(config.TIMEZONE).date()

    if periodo == HOJE:
        return _meia_noite(hoje), _meia_noite(hoje + timedelta(days=1))
    if periodo == SEMANA:
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
