#!/usr/bin/env bash
# Install a git pre-commit hook that auto-updates README.md before every commit.
# Run once after cloning: `bash scripts/install_hooks.sh`
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
  echo "Not a git repo (no .git directory). Aborting."
  exit 1
fi

HOOK=.git/hooks/pre-commit
cat > "$HOOK" <<'HOOK_EOF'
#!/usr/bin/env bash
set -e
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
python -m scripts.update_readme >/dev/null
git add README.md
HOOK_EOF
chmod +x "$HOOK"
echo "Installed git pre-commit hook → $HOOK"
echo "From now on, every commit auto-regenerates the README module map."
