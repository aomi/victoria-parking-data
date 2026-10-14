#!/usr/bin/env bash
# Copy original JSON snapshots from R2; credentials stay in the environment.
set -euo pipefail

if ! command -v rclone >/dev/null 2>&1; then
  echo 'rclone is required. Install it with: brew install rclone' >&2
  exit 1
fi

: "${R2_ACCOUNT_ID:?Set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?Set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?Set R2_SECRET_ACCESS_KEY}"

export RCLONE_CONFIG_PARKING_TYPE=s3
export RCLONE_CONFIG_PARKING_PROVIDER=Cloudflare
export RCLONE_CONFIG_PARKING_REGION=auto
export RCLONE_CONFIG_PARKING_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_PARKING_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_PARKING_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_PARKING_ENV_AUTH=false

# Run from the repository root; defaults match process_parking.py.
exec rclone copy "parking:${PARKING_BUCKET:-victoriaparking}" "${PARKING_RAW_DIR:-data/raw}" \
  --include '*.json' \
  --transfers 16 \
  --checkers 32 \
  --fast-list \
  --size-only \
  --s3-no-head-object \
  --progress \
  --stats 10s \
  "$@"
