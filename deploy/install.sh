#!/usr/bin/env bash
# Gera a unit systemd com o caminho deste clone e instala como serviço de usuário.
set -euo pipefail

PROJETO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$PROJETO/deploy/diet-bot.service.template"
DESTINO="$HOME/.config/systemd/user/diet-bot.service"
SERVICO="diet-bot"

erro() { echo "erro: $*" >&2; exit 1; }

command -v systemctl >/dev/null || erro "systemd não encontrado. Veja o README para Windows/WSL."
[ -f "$TEMPLATE" ] || erro "template não encontrado em $TEMPLATE"
[ -x "$PROJETO/.venv/bin/python" ] || erro "venv ausente. Rode: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
[ -f "$PROJETO/.env" ] || erro "arquivo .env ausente. Copie .env.example para .env e preencha."

# Falha agora, com mensagem clara, em vez de num loop de restart do systemd.
"$PROJETO/.venv/bin/python" -c 'import config' >/dev/null || erro "config inválida (veja a mensagem acima)"

mkdir -p "$(dirname "$DESTINO")"
[ -f "$DESTINO" ] && echo "substituindo unit existente em $DESTINO"
sed "s|__PROJETO__|$PROJETO|g" "$TEMPLATE" > "$DESTINO"

systemctl --user daemon-reload
systemctl --user enable "$SERVICO" >/dev/null
systemctl --user restart "$SERVICO"

# Sem linger o serviço só sobe depois de um login gráfico.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  loginctl enable-linger "$USER" 2>/dev/null \
    || echo "aviso: não consegui ativar o linger. Rode: sudo loginctl enable-linger $USER"
fi

sleep 2
systemctl --user --no-pager --lines=0 status "$SERVICO" || true
echo
echo "instalado em $DESTINO"
echo "logs: journalctl --user -u $SERVICO -f"
