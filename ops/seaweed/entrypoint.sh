#!/bin/sh
set -eu

fail() {
  printf '%s\n' "$1" >&2
  exit 64
}

read_credential() {
  credential_path=$1
  minimum_length=$2
  [ -r "$credential_path" ] || fail "OBJECT_STORE_SECRET_UNREADABLE"
  credential=$(cat "$credential_path")
  [ "${#credential}" -ge "$minimum_length" ] || fail "OBJECT_STORE_SECRET_INVALID"
  [ "${#credential}" -le 128 ] || fail "OBJECT_STORE_SECRET_INVALID"
  case "$credential" in
    *[!A-Za-z0-9._~-]*) fail "OBJECT_STORE_SECRET_INVALID" ;;
  esac
  printf '%s' "$credential"
}

access_key=$(read_credential "${RATEREPLAY_S3_ACCESS_KEY_FILE:?}" 3)
secret_key=$(read_credential "${RATEREPLAY_S3_SECRET_KEY_FILE:?}" 16)
bucket=${RATEREPLAY_S3_BUCKET:?}
[ "${#bucket}" -ge 3 ] || fail "OBJECT_STORE_BUCKET_INVALID"
[ "${#bucket}" -le 63 ] || fail "OBJECT_STORE_BUCKET_INVALID"
case "$bucket" in
  [a-z0-9]*[a-z0-9]) ;;
  *) fail "OBJECT_STORE_BUCKET_INVALID" ;;
esac
case "$bucket" in
  *[!a-z0-9.-]*|*..*|*.-*|*-.*) fail "OBJECT_STORE_BUCKET_INVALID" ;;
esac

umask 077
config_path=/tmp/ratereplay-s3-config.json
printf '{"identities":[{"name":"ratereplay","credentials":[{"accessKey":"%s","secretKey":"%s"}],"actions":["Admin","Read","List","Tagging","Write"]}]}\n' \
  "$access_key" "$secret_key" > "$config_path"

exec /usr/bin/weed mini \
  -dir=/data \
  -ip.bind=0.0.0.0 \
  -master.telemetry=false \
  -admin.ui=false \
  -filer.disableDirListing=true \
  -filer.exposeDirectoryData=false \
  -s3=true \
  -s3.port=9000 \
  -s3.config="$config_path" \
  -s3.iam=false \
  -s3.allowDeleteBucketNotEmpty=false \
  -s3.allowedOrigins=https://localhost \
  -bucket="$bucket"
