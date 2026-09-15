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


def _agrupar_por_dia(linhas: list[dict]) -> dict[date, dict]:
    """Soma os macros por dia, no fuso de `config.TIMEZONE` — mesmo motivo do
    `criado_em` em `janela()`: o banco guarda tudo em UTC."""
    por_dia: dict[date, dict] = {}
    for linha in linhas:
        dia = datetime.fromisoformat(linha["criado_em"]).astimezone(config.TIMEZONE).date()
        acumulado = por_dia.setdefault(dia, {campo: 0.0 for campo in db.CAMPOS_MACRO})
        for campo in db.CAMPOS_MACRO:
            acumulado[campo] += float(linha[campo])
    return por_dia


def _agrupar_atividades_por_dia(atividades: list[dict]) -> dict[date, list[dict]]:
    por_dia: dict[date, list[dict]] = {}
    for atividade in atividades:
        dia = datetime.fromisoformat(atividade["criado_em"]).astimezone(config.TIMEZONE).date()
        por_dia.setdefault(dia, []).append(atividade)
    return por_dia


def resumir(periodo: str, agora: datetime | None = None) -> dict:
    """Totais do período. `refeicoes` é a contagem, não a lista."""
    inicio, fim = janela(periodo, agora)
    linhas = db.buscar_refeicoes(inicio, fim)
    atividades = db.buscar_atividades(inicio, fim)

    # numeric do Postgres chega como int ou float conforme o valor; float()
    # normaliza antes de somar.
    totais = {
        campo: sum(float(linha[campo]) for linha in linhas)
        for campo in db.CAMPOS_MACRO
    }
    log.info(
        "resumo %s: %s refeições, %s atividades entre %s e %s",
        periodo, len(linhas), len(atividades), inicio.date(), fim.date(),
    )
    return {
        "periodo": periodo,
        "inicio": inicio,
        # O fim é exclusivo; o último dia coberto é o anterior a ele.
        "ultimo_dia": (fim - timedelta(days=1)).date(),
        "refeicoes": len(linhas),
        "por_dia": _agrupar_por_dia(linhas),
        "atividades_por_dia": _agrupar_atividades_por_dia(atividades),
        **totais,
    }


def _macros(dados: dict) -> str:
    return (
        f"{dados['calorias']:.0f} kcal · "
        f"{dados['proteina_g']:.1f}g prot · "
        f"{dados['gordura_g']:.1f}g gord · "
        f"{dados['carboidrato_g']:.1f}g carb"
    )


def _atividade(atividade: dict) -> str:
    return (
        f"{atividade['descricao']}.\n"
        f"~{atividade['calorias']:.0f} kcal em {atividade['duracao_min']:.0f} min"
    )


def formatar(dados: dict) -> str:
    if dados["periodo"] == HOJE:
        atividades_hoje = dados["atividades_por_dia"].get(dados["ultimo_dia"], [])
        partes = []
        if dados["refeicoes"]:
            partes.append(f"📊 Hoje você consumiu {_macros(dados)}.")
        partes.extend(
            f"🏃 Hoje você realizou: {_atividade(atividade)}"
            for atividade in atividades_hoje
        )
        if not partes:
            return "Nada registrado hoje ainda."
        return "\n".join(partes)

    periodo = (
        f"{dados['inicio']:%d/%m} a {dados['ultimo_dia']:%d/%m}"
    )
    dias_com_dados = sorted(set(dados["por_dia"]) | set(dados["atividades_por_dia"]))
    if not dias_com_dados:
        return f"Nada registrado de {periodo}."

    # Só dias com refeição ou atividade aparecem — um dia sem registro não vira
    # linha "0 kcal".
    blocos_por_dia = []
    for dia in dias_com_dados:
        valores = dados["por_dia"].get(dia)
        linhas_dia = [f"{dia:%d/%m} - {_macros(valores)}" if valores else f"{dia:%d/%m}"]
        linhas_dia.extend(
            f"🏃 {_atividade(atividade)}"
            for atividade in dados["atividades_por_dia"].get(dia, [])
        )
        blocos_por_dia.append("\n".join(linhas_dia))

    linhas_por_dia = "\n".join(blocos_por_dia)
    total = f"Total de {periodo} ({DIAS_DA_SEMANA} dias): {_macros(dados)}"
    return f"📊 {linhas_por_dia}\n\n{total}"


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    escolhido = sys.argv[1] if len(sys.argv) > 1 else HOJE
    print(formatar(resumir(escolhido)))
