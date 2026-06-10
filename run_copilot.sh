#!/usr/bin/env bash
# Launches the GitHub Copilot CLI with traffic routed through the local Beans
# Proxy. Each invocation uses a different pseudo-API key so token usage is
# attributed per-task in token_usage/<pseudo_key>.json.
#
# Copilot CLI's "Bring Your Own Model Provider" feature is what we use here:
# it forwards chat requests to whatever Base URL we give it, with our key.
# The relevant env vars are COPILOT_PROVIDER_* (NOT OPENAI_API_BASE etc.).
#
# Usage:
#   ./run_copilot.sh sk-task-12345                          # interactive
#   ./run_copilot.sh sk-task-12345 openai/gpt-4o-mini       # custom model
#   ./run_copilot.sh sk-task-12345 openai/gpt-4o-mini "fix the bug" -p
#
# Arguments:
#   $1  pseudo-api-key           (required) - recorded against spend
#   $2  model id                 (optional, default: openai/gpt-4o-mini)
#   $3+  forwarded to `copilot`  (optional prompt and/or flags)

set -euo pipefail

PROXY_PORT="${BEANS_PROXY_PORT:-8000}"
PROXY_HOST="${BEANS_PROXY_HOST:-127.0.0.1}"
DEFAULT_MODEL="openai/gpt-4o-mini"

if [[ $# -lt 1 ]]; then
  cat >&2 <<EOF
usage:
  $0 <pseudo-api-key> [model-id] [copilot args...]

  pseudo-api-key  recorded against token spend (e.g. sk-task-12345)
  model-id        optional, default: ${DEFAULT_MODEL}
  copilot args    forwarded to the \`copilot\` binary
EOF
  exit 64
fi

if ! command -v copilot >/dev/null 2>&1; then
  echo "error: 'copilot' not found on PATH. Install with: brew install copilot-cli" >&2
  exit 127
fi

PSEUDO_KEY="$1"
shift
MODEL_ID="${DEFAULT_MODEL}"
# If the next arg looks like a model id (no leading dash, no whitespace),
# treat it as the model and shift it off the copilot-arg list.
if [[ $# -gt 0 && "$1" != -* && "$1" != *[[:space:]]* ]]; then
  MODEL_ID="$1"
  shift
fi

PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}"

echo "Beans Proxy:  ${PROXY_URL}"
echo "Pseudo key:   ${PSEUDO_KEY}"
echo "Model:        ${MODEL_ID}"
echo

# Point Copilot at the proxy's OpenAI-compatible endpoint. These env vars
# are picked up on every invocation — no sticky config to manage.
export COPILOT_PROVIDER_BASE_URL="${PROXY_URL}"
export COPILOT_PROVIDER_TYPE="openai"
export COPILOT_PROVIDER_API_KEY="${PSEUDO_KEY}"
export COPILOT_MODEL="${MODEL_ID}"

exec copilot "$@"

