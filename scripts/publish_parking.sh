#!/usr/bin/env bash
# Publish prepared compressed downloads, then their manifest. Run from the repository root.
set -euo pipefail

if ! command -v rclone >/dev/null 2>&1; then
  echo 'rclone is required. Install it with: brew install rclone' >&2
  exit 1
fi

: "${R2_ACCOUNT_ID:?Set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?Set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?Set R2_SECRET_ACCESS_KEY}"

artifacts_dir="${PARKING_ARTIFACTS_DIR:-data/public}"
monthly_dir="$artifacts_dir/monthly"
yearly_dir="$artifacts_dir/yearly"
manifest="${PARKING_MANIFEST:-manifest.json}"
bucket="${PARKING_PUBLIC_BUCKET:-victoria-parking-public}"

if [[ ! -d "$monthly_dir" || ! -d "$yearly_dir" || ! -f "$manifest" ]]; then
  echo 'Compressed downloads and manifest must exist. Run generate_manifest.py before publishing.' >&2
  exit 1
fi
if [[ ! "$bucket" =~ ^[a-z0-9][a-z0-9.-]*$ ]]; then
  echo 'Invalid bucket name' >&2
  exit 1
fi

file_lists="$(mktemp -d)"
trap 'rm -rf "$file_lists"' EXIT
: > "$file_lists/monthly"
: > "$file_lists/yearly"
shopt -s nullglob
for path in "$monthly_dir"/parking-*.csv.gz; do
  name="${path##*/}"
  if [[ -f "$path" && "$name" =~ ^parking-[0-9]{4}-(0[1-9]|1[0-2])\.csv\.gz$ ]]; then
    printf '%s\n' "$name" >> "$file_lists/monthly"
  fi
done
for path in "$yearly_dir"/parking-*.zip; do
  name="${path##*/}"
  if [[ -f "$path" && "$name" =~ ^parking-[0-9]{4}\.zip$ ]]; then
    printf '%s\n' "$name" >> "$file_lists/yearly"
  fi
done
if [[ ! -s "$file_lists/monthly" || ! -s "$file_lists/yearly" ]]; then
  echo 'No monthly gzip files or yearly ZIPs found. Run generate_manifest.py first.' >&2
  exit 1
fi

export RCLONE_CONFIG_PARKING_TYPE=s3
export RCLONE_CONFIG_PARKING_PROVIDER=Cloudflare
export RCLONE_CONFIG_PARKING_REGION=auto
export RCLONE_CONFIG_PARKING_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export RCLONE_CONFIG_PARKING_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_PARKING_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_PARKING_ENV_AUTH=false
export RCLONE_CONFIG_PARKING_NO_CHECK_BUCKET=true

# The trailing slash forces an S3 directory prefix, avoiding file detection.
rclone copy "$monthly_dir" "parking:$bucket/csv/monthly/" \
  --files-from-raw "$file_lists/monthly" \
  --transfers 4 \
  --ignore-times \
  --retries 3 \
  --metadata \
  --metadata-set 'content-type=application/gzip' \
  --metadata-set 'content-disposition=attachment' \
  --metadata-set 'cache-control=public, max-age=3600' \
  --progress \
  "$@"

rclone copy "$yearly_dir" "parking:$bucket/csv/yearly/" \
  --files-from-raw "$file_lists/yearly" \
  --transfers 4 \
  --ignore-times \
  --retries 3 \
  --metadata \
  --metadata-set 'content-type=application/zip' \
  --metadata-set 'content-disposition=attachment' \
  --metadata-set 'cache-control=public, max-age=3600' \
  --progress \
  "$@"

# Publish the inventory only after both compressed download transfers succeed.
rclone copyto "$manifest" "parking:$bucket/csv/manifest.json" \
  --ignore-times \
  --retries 3 \
  --metadata \
  --metadata-set 'content-type=application/json; charset=utf-8' \
  --metadata-set 'cache-control=public, max-age=3600' \
  "$@"
