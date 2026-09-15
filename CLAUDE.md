# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandos

```bash
# setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # preencher antes de rodar

# rodar o bot
.venv/bin/python bot.py

# resumos via terminal (sem passar pelo Telegram)
.venv/bin/python resumo.py          # hoje
.venv/bin/python resumo.py semana

# validar config sem subir o bot
.venv/bin/python -c 'import config'

# serviço systemd (usuário, não root — ver por quê na seção Deploy)
./deploy/install.sh
journalctl --user -u diet-bot -f
```

Não há suíte de testes nem linter configurado neste repo.

## Arquitetura

Pipeline linear, sem framework: `bot.py` recebe a mensagem do Telegram, converte
pra texto se for áudio, manda pro Claude Code CLI extrair dados estruturados, e
grava no Supabase. Cada etapa é um módulo com uma responsabilidade:

- **bot.py** — handlers do `python-telegram-bot` e orquestração do fluxo. Todo
  handler é registrado com o filtro `SO_EU` (`TELEGRAM_USER_ID`); mensagens de
  outra conta não geram resposta nem log de erro, propositalmente.
- **claude_client.py** — único lugar que fala com o `claude` CLI. Monta o
  comando (`-p`, `--system-prompt`, `--json-schema`, `--restricted`), roda em
  subprocess com semáforo (uma chamada por vez), parseia o envelope
  `--output-format json` e valida os campos numéricos antes de devolver.
- **transcriber.py** — Whisper local (`faster-whisper`, CPU/int8), modelo
  carregado uma vez (lazy, módulo-level `_modelo`).
- **db.py** — único lugar que fala com o Supabase. Schema `dieta` isolado via
  `.schema()`, não o `public` default.
- **resumo.py** — janelas de tempo (`/hoje`, `/semana`) e soma dos macros já
  gravados. Não chama o Claude — os totais já estão no banco como colunas
  numéricas.
- **config.py** — único ponto de leitura do `.env`; toda variável obrigatória é
  validada no import (`_obrigatorio`), então falha cedo com mensagem clara em
  vez de quebrar na primeira refeição.

### Por que a extração passa pelo Claude Code CLI, não pela API

O bot chama `claude -p` como subprocess (`claude_client._montar_comando`) para
rodar contra a cota da assinatura Claude Pro/Max, não uma API key paga.
`--system-prompt` **substitui** o prompt default (~33k tokens) em vez de somar;
trocar por `--append-system-prompt` reintroduz esse custo — não fazer isso.

### Superfície de segurança em claude_client.py

Fluxo de foto: cada imagem baixada vai para um diretório descartável próprio
(`bot.py:_baixar`), que vira o `cwd` da chamada ao Claude. A tool `Read` só
enxerga esse diretório, então texto adversarial na imagem não tem `.env` nem
outro arquivo do repo pra ler. Reforçado por `--restricted`,
`--strict-mcp-config` e `--permission-prompts none`. Ao mexer nesse fluxo,
manter o isolamento de diretório — é a defesa real, as flags são a segunda
camada.

### Fuso horário

`criado_em` é gravado em UTC (`timestamptz`), mas as janelas de `/hoje` e
`/semana` (`resumo.janela`) são calculadas em `config.TIMEZONE`. Os totais
usam `update.message.date` (quando a mensagem foi mandada), não o horário de
processamento — importante para a fila de mensagens represada após o PC ficar
desligado (Telegram retém por 24h).

### Deploy

Unit systemd de **usuário**, não de sistema: o `claude` CLI lê credenciais
OAuth de `$HOME`, então rodar como root quebraria toda extração. `deploy/install.sh`
gera a unit a partir de `deploy/diet-bot.service.template` e ativa `linger` para
o serviço subir no boot sem login.
