#!/bin/sh
# DJcode CLI — offline self-contained install shim (token-diet 2026-09-16).
# Isolates DJcode from ~/.claude (skills/hooks/MCP/CLAUDE.md) and caps context; local Ollama only.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.djcode-config}"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-32000}"
export BASH_MAX_OUTPUT_LENGTH="${BASH_MAX_OUTPUT_LENGTH:-12000}"
export MAX_MCP_OUTPUT_TOKENS="${MAX_MCP_OUTPUT_TOKENS:-8000}"
export DO_NOT_TRACK=1
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://localhost:11434}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-gemma4}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-local}"
exec /usr/bin/env node "$HOME/.djcode-cli/bin/cli.mjs" "$@"
