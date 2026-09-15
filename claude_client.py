"""Wrapper do Claude Code CLI (`claude -p`) para extração nutricional.
"""

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import config

log = logging.getLogger(__name__)

CAMPOS_MACRO = ("calorias", "proteina_g", "carboidrato_g", "gordura_g")

SCHEMA_REFEICAO = {
    "type": "object",
    "properties": {
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string"},
                    "quantidade": {"type": "string"},
                    "calorias": {"type": "number"},
                    "proteina_g": {"type": "number"},
                    "carboidrato_g": {"type": "number"},
                    "gordura_g": {"type": "number"},
                },
                "required": [
                    "nome",
                    "quantidade",
                    "calorias",
                    "proteina_g",
                    "carboidrato_g",
                    "gordura_g",
                ],
            },
        },
        "calorias": {"type": "number"},
        "proteina_g": {"type": "number"},
        "carboidrato_g": {"type": "number"},
        "gordura_g": {"type": "number"},
    },
    "required": ["itens", "calorias", "proteina_g", "carboidrato_g", "gordura_g"],
}

SYSTEM_PROMPT_FOTO = """\
Você é um extrator de dados nutricionais para um diário alimentar brasileiro.
Recebe uma foto de comida e responde APENAS com o JSON do schema fornecido.

Regras:
- Quantidade não informada: estime a porção caseira brasileira típica e deixe a \
estimativa explícita no campo "quantidade" (ex: "~1 concha (80g)", "1 fatia (~110g)").
- Os valores são do alimento preparado como é comido (arroz cozido, não cru; \
frango grelhado, não peito cru), incluindo o óleo de preparo quando for o padrão.
- Use a tabela TACO / rótulos brasileiros como referência quando o alimento for \
tipicamente nacional.
- Os campos de topo (calorias, proteina_g, carboidrato_g, gordura_g) são a SOMA \
dos itens. Confira a soma antes de responder.
- Arredonde calorias para inteiro e macros para uma casa decimal.
- Se não houver nada identificável como comida, responda com "itens": [] e todos \
os totais em 0.
"""

SCHEMA_TEXTO = {
    "type": "object",
    "properties": {
        "tipo": {"type": "string", "enum": ["refeicao", "atividade", "peso", "nenhum"]},
        "refeicao": SCHEMA_REFEICAO,
        "atividade": {
            "type": "object",
            "properties": {
                "descricao": {"type": "string"},
                "duracao_min": {"type": "number"},
                "calorias": {"type": "number"},
            },
            "required": ["descricao", "duracao_min", "calorias"],
        },
        "peso": {
            "type": "object",
            "properties": {"peso_kg": {"type": "number"}},
            "required": ["peso_kg"],
        },
    },
    "required": ["tipo"],
}

SYSTEM_PROMPT_TEXTO = """\
Você é um extrator de dados para um diário alimentar e de atividades físicas \
brasileiro. Recebe um texto digitado ou transcrição de áudio e primeiro decide \
o campo "tipo":
- "refeicao": descreve comida ou bebida consumida.
- "atividade": descreve exercício físico realizado (corrida, caminhada, \
musculação, bike, natação etc).
- "peso": informa o peso corporal do usuário (ex: "me pesei hoje, estou com \
95kg", "89,4 na balança").
- "nenhum": não é nenhum dos anteriores.

Preencha SOMENTE o campo correspondente ao "tipo" escolhido ("refeicao", \
"atividade" ou "peso"); omita os outros.

Ignore vocativos e saudações ("Claude,", "ô Claude", "bom dia", "então") em \
qualquer um dos casos.

Regras para "refeicao":
- Quantidade não informada: estime a porção caseira brasileira típica e deixe a \
estimativa explícita no campo "quantidade" (ex: "~1 concha (80g)", "1 fatia (~110g)").
- Os valores são do alimento preparado como é comido (arroz cozido, não cru; \
frango grelhado, não peito cru), incluindo o óleo de preparo quando for o padrão.
- Use a tabela TACO / rótulos brasileiros como referência quando o alimento for \
tipicamente nacional.
- Os campos de topo da refeição (calorias, proteina_g, carboidrato_g, gordura_g) \
são a SOMA dos itens. Confira a soma antes de responder.
- Arredonde calorias para inteiro e macros para uma casa decimal.

Regras para "atividade":
- "duracao_min": duração em minutos. Se não for informada, estime um valor \
típico para o exercício descrito.
- "calorias": gasto calórico estimado, com base em duração, tipo de exercício e \
intensidade típica (MET aproximado) para um adulto de porte médio (~70kg), na \
ausência de mais informação.
- "descricao": resume o exercício e, quando relevante, grupo muscular ou \
distância (ex.: "Corrida de 5km", "Musculação (peito e ombro)", "Bike ergométrica").

Regras para "peso":
- "peso_kg": peso corporal em quilos. Converta se vier em outra unidade \
(libras, arrobas) e aceite vírgula como separador decimal ("89,4" = 89.4).

Responda APENAS com o JSON do schema fornecido.
"""


class ClaudeError(RuntimeError):
    """Falha na extração — a mensagem é curta o bastante pra ir pro Telegram."""


# Uma chamada ao Claude por vez: várias mensagens seguidas não devem virar
# N processos `claude` concorrentes disputando CPU e cota.
_semaforo = asyncio.Semaphore(1)


def _montar_comando(
    prompt: str, com_leitura_de_arquivo: bool, schema: dict, system_prompt: str
) -> list[str]:
    return [
        config.CLAUDE_BIN,
        "-p",
        prompt,
        "--model",
        config.CLAUDE_MODEL,
        "--system-prompt",
        system_prompt,
        # Foto precisa da tool Read pra abrir o arquivo; texto não precisa de nada.
        "--tools",
        "Read" if com_leitura_de_arquivo else "",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        # Processo automático, ninguém pra aprovar prompt: nada pode ficar travado.
        "--permission-prompts",
        "none",
        # Não herda MCP servers de outros contextos nem acumula sessão em disco
        # a cada refeição registrada.
        "--strict-mcp-config",
        "--no-session-persistence",
        # Confina as tools de arquivo ao working directory e ignora settings de
        # usuário/projeto. Segunda camada além do cwd isolado.
        "--restricted",
    ]


async def _chamar_claude(
    prompt: str,
    com_leitura_de_arquivo: bool,
    cwd: Path,
    schema: dict,
    system_prompt: str,
) -> dict:
    comando = _montar_comando(prompt, com_leitura_de_arquivo, schema, system_prompt)

    async with _semaforo:
        processo = await asyncio.create_subprocess_exec(
            *comando,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                processo.communicate(), timeout=config.CLAUDE_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            processo.kill()
            await processo.wait()
            raise ClaudeError(f"tempo esgotado ({config.CLAUDE_TIMEOUT_SEC}s)")

    if processo.returncode != 0:
        detalhe = stderr.decode(errors="replace").strip()[:300]
        log.error("claude saiu com código %s: %s", processo.returncode, detalhe)
        raise ClaudeError(f"o Claude CLI falhou (código {processo.returncode})")

    return _parsear_envelope(stdout.decode(errors="replace"))


def _parsear_envelope(saida: str) -> dict:
    """Extrai o objeto de dados do envelope de `--output-format json`."""
    try:
        envelope = json.loads(saida)
    except json.JSONDecodeError:
        log.error("saída não-JSON do claude: %s", saida[:500])
        raise ClaudeError("o Claude retornou uma saída ilegível")

    if envelope.get("is_error"):
        log.error("claude reportou erro: %s", str(envelope.get("result"))[:500])
        raise ClaudeError("o Claude reportou um erro na extração")

    dados = envelope.get("structured_output")
    if dados is None:
        # Fallback: em alguns modos o objeto vem só como string em "result".
        bruto = envelope.get("result")
        if isinstance(bruto, str):
            try:
                dados = json.loads(bruto)
            except json.JSONDecodeError:
                dados = None

    if not isinstance(dados, dict):
        log.error("envelope sem structured_output utilizável: %s", saida[:500])
        raise ClaudeError("o Claude retornou JSON inválido")

    uso = envelope.get("usage", {})
    log.info(
        "extração ok: contexto=%s tokens, saída=%s tokens, %sms",
        uso.get("cache_creation_input_tokens", 0) + uso.get("input_tokens", 0),
        uso.get("output_tokens", 0),
        envelope.get("duration_ms"),
    )
    return dados


def _validar_refeicao(dados: dict) -> dict:
    """O Python decide o que é aceitável — não o Claude."""
    itens = dados.get("itens")
    if not isinstance(itens, list):
        raise ClaudeError("o Claude retornou JSON inválido (itens ausente)")

    if not itens:
        raise ClaudeError("não identifiquei comida nessa mensagem")

    for item in itens:
        if not isinstance(item, dict) or not item.get("nome"):
            raise ClaudeError("o Claude retornou JSON inválido (item sem nome)")
        for campo in CAMPOS_MACRO:
            _numero(item, campo)

    for campo in CAMPOS_MACRO:
        _numero(dados, campo)

    return dados


def _validar_atividade(dados: dict) -> dict:
    descricao = dados.get("descricao")
    if not isinstance(descricao, str) or not descricao.strip():
        raise ClaudeError("o Claude retornou JSON inválido (atividade sem descrição)")

    return {
        "descricao": descricao.strip(),
        "duracao_min": _numero(dados, "duracao_min"),
        "calorias": _numero(dados, "calorias"),
    }


def _validar_peso(dados: dict) -> dict:
    peso = _numero(dados, "peso_kg")
    # Fora dessa faixa é erro de extração (transcrição trocando número, peso em
    # libras não convertido), não um peso real.
    if not 20 <= peso <= 400:
        raise ClaudeError(f"peso fora do esperado ({peso:g} kg)")
    return {"peso_kg": peso}


def _validar_texto(dados: dict) -> dict:
    tipo = dados.get("tipo")
    if tipo == "refeicao":
        return {"tipo": tipo, **_validar_refeicao(dados.get("refeicao") or {})}
    if tipo == "atividade":
        return {"tipo": tipo, **_validar_atividade(dados.get("atividade") or {})}
    if tipo == "peso":
        return {"tipo": tipo, **_validar_peso(dados.get("peso") or {})}
    raise ClaudeError(
        "não identifiquei refeição, atividade física nem peso nessa mensagem"
    )


def _numero(origem: dict, campo: str) -> float:
    valor = origem.get(campo)
    if not isinstance(valor, (int, float)) or isinstance(valor, bool) or valor < 0:
        raise ClaudeError(f"o Claude retornou JSON inválido ({campo}={valor!r})")
    return float(valor)


async def interpretar_texto(texto: str) -> dict:
    """Extrai refeição, atividade física ou peso corporal de um texto digitado ou
    transcrição de áudio. O resultado traz "tipo": "refeicao", "atividade" ou
    "peso" para o chamador decidir onde gravar."""
    with tempfile.TemporaryDirectory(dir=config.TMP_DIR) as vazio:
        dados = await _chamar_claude(
            texto,
            com_leitura_de_arquivo=False,
            cwd=Path(vazio),
            schema=SCHEMA_TEXTO,
            system_prompt=SYSTEM_PROMPT_TEXTO,
        )
    return _validar_texto(dados)


async def extrair_de_foto(caminho_imagem: str | Path, legenda: str | None = None) -> dict:
    """Extrai a refeição de uma foto.

    A imagem tem que estar sozinha num diretório descartável: esse diretório vira
    o working directory da chamada, e é o único lugar que o Claude consegue ler.
    """
    caminho = Path(caminho_imagem).resolve()
    prompt = (
        f"Analise a imagem da refeição em ./{caminho.name} "
        "e extraia os dados nutricionais."
    )
    if legenda:
        prompt += f"\nO usuário descreveu assim: {legenda}"

    dados = await _chamar_claude(
        prompt,
        com_leitura_de_arquivo=True,
        cwd=caminho.parent,
        schema=SCHEMA_REFEICAO,
        system_prompt=SYSTEM_PROMPT_FOTO,
    )
    return _validar_refeicao(dados)
