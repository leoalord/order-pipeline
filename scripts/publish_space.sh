#!/bin/sh
# Flatten this repo into a Docker Space (root Dockerfile + Space README).
# Usage: ./scripts/publish_space.sh [SPACE_ID]
# Requires: hf auth login  (https://huggingface.co/settings/tokens — write)
set -eu

SPACE_ID="${1:-LeoWalker/order-pipeline}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/space"
cp "$ROOT/pyproject.toml" "$ROOT/uv.lock" "$ROOT/alembic.ini" "$STAGING/"
cp -R "$ROOT/src" "$ROOT/alembic" "$ROOT/dashboard" "$STAGING/"
rm -rf "$STAGING/dashboard/node_modules" "$STAGING/dashboard/dist"
# HF builds ./Dockerfile; Compose keeps using the repo-root Dockerfile.
cp "$ROOT/space/Dockerfile" "$STAGING/Dockerfile"
cp "$ROOT/space/README.md" "$STAGING/README.md"
cp "$ROOT/space/start.sh" "$ROOT/space/nginx.conf" "$STAGING/space/"
cp "$ROOT/space/.dockerignore" "$STAGING/.dockerignore"

if ! command -v hf >/dev/null 2>&1; then
  echo "hf CLI not found. Install: curl -LsSf https://hf.co/cli/install.sh | bash -s" >&2
  exit 1
fi

hf upload "$SPACE_ID" "$STAGING" --type space --commit-message "Deploy single-container Space image"
echo "Uploaded to https://huggingface.co/spaces/${SPACE_ID}"
