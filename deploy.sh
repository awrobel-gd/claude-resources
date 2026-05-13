#!/usr/bin/env bash
set -euo pipefail

HOOKS_SRC="$(cd "$(dirname "$0")/hooks" && pwd)"
HOOKS_DEST="$HOME/.claude/hooks"

mkdir -p "$HOOKS_DEST"

echo "Deploying hooks from $HOOKS_SRC to $HOOKS_DEST"

for f in "$HOOKS_SRC"/*; do
  [ -f "$f" ] || continue
  dest="$HOOKS_DEST/$(basename "$f")"
  cp "$f" "$dest"
  chmod +x "$dest"
  echo "  copied $(basename "$f")"
done

echo "Done."
