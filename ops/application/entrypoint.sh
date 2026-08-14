#!/bin/sh
set -eu

source_path=${RATEREPLAY_POSTGRES_PGPASS_SOURCE_FILE:-}
if [ -n "$source_path" ]; then
  if [ ! -r "$source_path" ]; then
    printf '%s\n' "POSTGRES_PGPASS_SOURCE_UNREADABLE" >&2
    exit 64
  fi
  source_size=$(wc -c < "$source_path")
  source_lines=$(wc -l < "$source_path")
  if [ "$source_size" -lt 1 ] || [ "$source_size" -gt 4096 ] || [ "$source_lines" -ne 1 ]; then
    printf '%s\n' "POSTGRES_PGPASS_SOURCE_INVALID" >&2
    exit 64
  fi
  target_path=/tmp/ratereplay-postgres.pgpass
  umask 077
  cp "$source_path" "$target_path"
  chmod 0600 "$target_path"
  export PGPASSFILE="$target_path"
  export RATEREPLAY_BACKUP_PGPASSFILE="$target_path"
fi

exec "$@"
