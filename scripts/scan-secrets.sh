#!/bin/sh
set -eu

secret_pattern='AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'

if rg --hidden --glob '!.git/**' --glob '!node_modules/**' --glob '!.venv/**' \
  --glob '!third_party/licenses/**' --glob '!scripts/scan-secrets.sh' \
  --files-with-matches --regexp "$secret_pattern" .; then
  echo 'Potential credential material found.' >&2
  exit 1
fi

echo 'No credential patterns found.'
