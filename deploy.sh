#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Deploy hooks
HOOKS_SRC="$REPO_ROOT/hooks"
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

# Deploy skills
SKILLS_SRC="$REPO_ROOT/skills"
SKILLS_DEST="$HOME/.claude/skills"

if [ -d "$SKILLS_SRC" ]; then
  mkdir -p "$SKILLS_DEST"
  echo "Deploying skills from $SKILLS_SRC to $SKILLS_DEST"
  for skill_dir in "$SKILLS_SRC"/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name="$(basename "$skill_dir")"
    dest="$SKILLS_DEST/$skill_name"
    cp -r "$skill_dir" "$dest"
    echo "  copied skill: $skill_name"
  done
fi

echo "Done."
