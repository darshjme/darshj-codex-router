#!/usr/bin/env bash
# Mac-side install (apply phase). Copies the client + sync script to a stable path, links `memory-bus`
# on PATH, loads the launchd sync (every 30 min). Does NOT edit ~/.claude/settings.json, ~/.codex/hooks.json,
# ~/.grok or ~/.djcode: run ../apply.py for those.
set -euo pipefail
SRC=$(cd "$(dirname "$0")/.." && pwd)
DEST="$HOME/.local/share/memory-bus"
mkdir -p "$DEST/bin" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
install -m 755 "$SRC/client/memory-bus" "$DEST/bin/memory-bus"
install -m 755 "$SRC/mac/memory_bus_sync.py" "$DEST/bin/memory_bus_sync.py"
ln -sf "$DEST/bin/memory-bus" "$HOME/.local/bin/memory-bus"
LABEL=ai.darshj.memory-bus-sync
sed "s#__HOME__#$HOME#g; s#__LABEL__#$LABEL#g; s#__SSH__#${MEMORY_BUS_SSH:-}#g" "$SRC/mac/memory-bus-sync.plist.template" > "$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/ai.darsh.memory-bus-sync" 2>/dev/null || true   # pre-plugin label
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "installed: $(ls -l "$HOME/.local/bin/memory-bus")"
echo "launchd:   $(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E 'state|interval' | head -2 | tr '\n' ' ')"
echo "next: memory-bus tunnel && memory-bus health && python3 $DEST/bin/memory_bus_sync.py"
