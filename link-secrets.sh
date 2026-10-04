#!/usr/bin/env bash
# Links this project's confidential files (.env, credentials, keys) from the owner's
# private secrets vault. Nothing secret is stored in this repository.
#   Vault: ${DEMO_SECRETS_DIR:-$HOME/Documents/RWork/secrets}/Trading-Kavach
#   (private repo github.com/Rahulnmt1/secrets)
set -euo pipefail
VAULT="${DEMO_SECRETS_DIR:-$HOME/Documents/RWork/secrets}/Trading-Kavach"
cd "$(dirname "$0")"
FILES=(
  ".env"
)
for f in "${FILES[@]}"; do
  src="$VAULT/$f"
  if [ ! -e "$src" ]; then echo "missing in vault: $f" >&2; continue; fi
  if [ -e "$f" ] && [ ! -L "$f" ] && [ ! "$f" -ef "$src" ]; then echo "skipped (real file present): $f" >&2; continue; fi
  mkdir -p "$(dirname "$f")"
  ln -sfn "$src" "$f"
  echo "linked $f"
done
