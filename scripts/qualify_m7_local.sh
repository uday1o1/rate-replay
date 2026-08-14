#!/bin/sh
set -eu

umask 077
repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
artifact_file=${1:-"$repository_root/evidence/reliability/m7-local-restore-rollback.json"}
runtime_parent=${RATEREPLAY_M7_TEMP_ROOT:-/tmp}
test -d "$runtime_parent"
runtime_root=$(mktemp -d "$runtime_parent/ratereplay-m7.XXXXXX")
source_project="ratereplay-m7-source-$$"
quarantine_project="ratereplay-m7-quarantine-$$"
source_postgres_port=${RATEREPLAY_M7_SOURCE_POSTGRES_PORT:-56432}
source_minio_port=${RATEREPLAY_M7_SOURCE_MINIO_PORT:-60000}
backup_minio_port=${RATEREPLAY_M7_BACKUP_MINIO_PORT:-60001}
quarantine_postgres_port=${RATEREPLAY_M7_QUARANTINE_POSTGRES_PORT:-56433}
quarantine_minio_port=${RATEREPLAY_M7_QUARANTINE_MINIO_PORT:-60002}
quarantine_backup_port=${RATEREPLAY_M7_QUARANTINE_BACKUP_PORT:-60003}
source_compose_env="$runtime_root/source-compose.env"
quarantine_compose_env="$runtime_root/quarantine-compose.env"

cleanup() {
  docker-compose --project-name "$quarantine_project" --env-file "$quarantine_compose_env" -f "$repository_root/compose.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker-compose --project-name "$source_project" --env-file "$source_compose_env" -f "$repository_root/compose.yaml" down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$runtime_root"
}
trap cleanup EXIT HUP INT TERM

mkdir -p \
  "$runtime_root/source-secrets" \
  "$runtime_root/quarantine-secrets" \
  "$runtime_root/object-keys" \
  "$runtime_root/backup-keys" \
  "$runtime_root/ledger-keys" \
  "$runtime_root/restore-keys"

openssl rand -hex 24 >"$runtime_root/source-secrets/postgres_password"
openssl rand -hex 12 >"$runtime_root/source-secrets/minio_user"
openssl rand -hex 24 >"$runtime_root/source-secrets/minio_password"
openssl rand -hex 12 >"$runtime_root/source-secrets/backup_minio_user"
openssl rand -hex 24 >"$runtime_root/source-secrets/backup_minio_password"
openssl rand -hex 24 >"$runtime_root/quarantine-secrets/postgres_password"
openssl rand -hex 12 >"$runtime_root/quarantine-secrets/minio_user"
openssl rand -hex 24 >"$runtime_root/quarantine-secrets/minio_password"
openssl rand -hex 12 >"$runtime_root/quarantine-secrets/backup_minio_user"
openssl rand -hex 24 >"$runtime_root/quarantine-secrets/backup_minio_password"
printf 'pppppppppppppppppppppppppppppppp' >"$runtime_root/object-keys/object-key-v1"
printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' >"$runtime_root/backup-keys/backup-key-v1"
printf 'llllllllllllllllllllllllllllllll' >"$runtime_root/ledger-keys/ledger-v1"
printf 'rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr' >"$runtime_root/restore-keys/restore-v1"
printf 'oooooooooooooooooooooooooooooooo' >"$runtime_root/outcome.key"

{
  printf 'RATEREPLAY_POSTGRES_PORT=%s\n' "$source_postgres_port"
  printf 'RATEREPLAY_MINIO_PORT=%s\n' "$source_minio_port"
  printf 'RATEREPLAY_BACKUP_MINIO_PORT=%s\n' "$backup_minio_port"
  printf 'RATEREPLAY_POSTGRES_PASSWORD_FILE=%s\n' "$runtime_root/source-secrets/postgres_password"
  printf 'RATEREPLAY_MINIO_USER_FILE=%s\n' "$runtime_root/source-secrets/minio_user"
  printf 'RATEREPLAY_MINIO_PASSWORD_FILE=%s\n' "$runtime_root/source-secrets/minio_password"
  printf 'RATEREPLAY_BACKUP_MINIO_USER_FILE=%s\n' "$runtime_root/source-secrets/backup_minio_user"
  printf 'RATEREPLAY_BACKUP_MINIO_PASSWORD_FILE=%s\n' "$runtime_root/source-secrets/backup_minio_password"
} >"$source_compose_env"
{
  printf 'RATEREPLAY_POSTGRES_PORT=%s\n' "$quarantine_postgres_port"
  printf 'RATEREPLAY_MINIO_PORT=%s\n' "$quarantine_minio_port"
  printf 'RATEREPLAY_BACKUP_MINIO_PORT=%s\n' "$quarantine_backup_port"
  printf 'RATEREPLAY_POSTGRES_PASSWORD_FILE=%s\n' "$runtime_root/quarantine-secrets/postgres_password"
  printf 'RATEREPLAY_MINIO_USER_FILE=%s\n' "$runtime_root/quarantine-secrets/minio_user"
  printf 'RATEREPLAY_MINIO_PASSWORD_FILE=%s\n' "$runtime_root/quarantine-secrets/minio_password"
  printf 'RATEREPLAY_BACKUP_MINIO_USER_FILE=%s\n' "$runtime_root/quarantine-secrets/backup_minio_user"
  printf 'RATEREPLAY_BACKUP_MINIO_PASSWORD_FILE=%s\n' "$runtime_root/quarantine-secrets/backup_minio_password"
} >"$quarantine_compose_env"

source_postgres_password=$(tr -d '\n' <"$runtime_root/source-secrets/postgres_password")
quarantine_postgres_password=$(tr -d '\n' <"$runtime_root/quarantine-secrets/postgres_password")

docker-compose --project-name "$source_project" --env-file "$source_compose_env" -f "$repository_root/compose.yaml" up -d --wait postgres object-store backup-store

docker-compose --project-name "$quarantine_project" --env-file "$quarantine_compose_env" -f "$repository_root/compose.yaml" up -d --wait postgres object-store

source_container=$(docker-compose --project-name "$source_project" --env-file "$source_compose_env" -f "$repository_root/compose.yaml" ps -q postgres)
quarantine_container=$(docker-compose --project-name "$quarantine_project" --env-file "$quarantine_compose_env" -f "$repository_root/compose.yaml" ps -q postgres)
test -n "$source_container"
test -n "$quarantine_container"

RATEREPLAY_M7_RUNTIME_DIR="$runtime_root" \
RATEREPLAY_M7_ARTIFACT_FILE="$artifact_file" \
RATEREPLAY_M7_SOURCE_DATABASE_URL="postgresql+psycopg://ratereplay:$source_postgres_password@127.0.0.1:$source_postgres_port/ratereplay" \
RATEREPLAY_M7_QUARANTINE_DATABASE_URL="postgresql+psycopg://ratereplay:$quarantine_postgres_password@127.0.0.1:$quarantine_postgres_port/ratereplay" \
RATEREPLAY_M7_SOURCE_POSTGRES_CONTAINER="$source_container" \
RATEREPLAY_M7_QUARANTINE_POSTGRES_CONTAINER="$quarantine_container" \
RATEREPLAY_M7_SOURCE_MINIO_ENDPOINT="127.0.0.1:$source_minio_port" \
RATEREPLAY_M7_SOURCE_MINIO_ACCESS_KEY_FILE="$runtime_root/source-secrets/minio_user" \
RATEREPLAY_M7_SOURCE_MINIO_SECRET_KEY_FILE="$runtime_root/source-secrets/minio_password" \
RATEREPLAY_M7_BACKUP_MINIO_ENDPOINT="127.0.0.1:$backup_minio_port" \
RATEREPLAY_M7_BACKUP_MINIO_ACCESS_KEY_FILE="$runtime_root/source-secrets/backup_minio_user" \
RATEREPLAY_M7_BACKUP_MINIO_SECRET_KEY_FILE="$runtime_root/source-secrets/backup_minio_password" \
RATEREPLAY_M7_QUARANTINE_MINIO_ENDPOINT="127.0.0.1:$quarantine_minio_port" \
RATEREPLAY_M7_QUARANTINE_MINIO_ACCESS_KEY_FILE="$runtime_root/quarantine-secrets/minio_user" \
RATEREPLAY_M7_QUARANTINE_MINIO_SECRET_KEY_FILE="$runtime_root/quarantine-secrets/minio_password" \
UV_CACHE_DIR=${UV_CACHE_DIR:-/private/tmp/rate-replay-uv-cache} \
uv run python "$repository_root/scripts/qualify_m7_restore.py"
