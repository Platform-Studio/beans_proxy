#!/usr/bin/env bash
# Launches the Cline CLI (npm i -g cline) with traffic routed through the local
# Beans Proxy. Each invocation uses a different pseudo-API key, so token usage
# is attributed per-task in token_usage/<pseudo_key>.json.
#
# Cline's "openai-compatible" provider is what we use here: it forwards chat
# requests to whatever Base URL we give it, with our key. The settings are
# persisted in ~/.cline/data/settings/providers.json by `cline auth`.
#
# Usage:
#   ./run_cline.sh sk-task-12345                       # interactive
#   ./run_cline.sh sk-task-12345 "fix the login bug"   # one-shot task
#   ./run_cline.sh sk-task-12345 openai/gpt-4o-mini    # custom model
#   ./run_cline.sh sk-task-12345 openai/gpt-4o-mini "refactor this" -p
#
# Arguments:
#   $1  pseudo-api-key           (required) - recorded against spend
#   $2  model id                 (optional, default: openai/gpt-4o-mini)
#   $3+  forwarded to `cline`    (optional prompt and/or flags)

set -euo pipefail

PROXY_PORT="${BEANS_PROXY_PORT:-8000}"
PROXY_HOST="${BEANS_PROXY_HOST:-127.0.0.1}"
PROVIDER_ID="openai-compatible"
DEFAULT_MODEL="openai/gpt-4o-mini"

if [[ $# -lt 1 ]]; then
  cat >&2 <<EOF
usage:
  $0 <pseudo-api-key> [model-id] [cline args...]

  pseudo-api-key  recorded against token spend (e.g. sk-task-12345)
  model-id        optional, default: ${DEFAULT_MODEL}
  cline args      forwarded to the \`cline\` binary
EOF
  exit 64
fi

if ! command -v cline >/dev/null 2>&1; then
  echo "error: 'cline' not found on PATH. Install with: npm i -g cline" >&2
  exit 127
fi

PSEUDO_KEY="$1"
shift
MODEL_ID="${DEFAULT_MODEL}"
# If the next arg looks like a model id (no leading dash, no whitespace),
# treat it as the model and shift it off the cline-arg list.
if [[ $# -gt 0 && "$1" != -* && "$1" != *[[:space:]]* ]]; then
  MODEL_ID="$1"
  shift
fi

PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}"

echo "Beans Proxy:  ${PROXY_URL}"
echo "Pseudo key:   ${PSEUDO_KEY}"
echo "Provider:     ${PROVIDER_ID}"
echo "Model:        ${MODEL_ID}"
echo

# Configure Cline's openai-compatible provider to point at the proxy.
# `cline auth` persists the setting in ~/.cline/data/settings/providers.json.
cline auth \
  --provider "${PROVIDER_ID}" \
  --apikey "${PSEUDO_KEY}" \
  --modelid "${MODEL_ID}" \
  --baseurl "${PROXY_URL}" >/dev/null

# Forward the rest. `-P` selects the provider; `-m` sets the model for this
# run. The user can append a prompt, flags (e.g. -p for plan mode, --json
# for headless), or nothing for the interactive TUI.
exec cline -P "${PROVIDER_ID}" -m "${MODEL_ID}" "$@"
