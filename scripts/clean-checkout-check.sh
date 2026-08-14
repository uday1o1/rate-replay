#!/bin/sh
set -eu

checkout_dir=$(mktemp -d /private/tmp/rate-replay-clean-checkout.XXXXXX)
case "$checkout_dir" in
  /private/tmp/rate-replay-clean-checkout.*) ;;
  *) echo 'Unsafe temporary checkout path.' >&2; exit 1 ;;
esac
cleanup() {
  rm -rf -- "$checkout_dir"
}
trap cleanup EXIT INT TERM

tree=$(git write-tree)
git archive "$tree" | tar -x -C "$checkout_dir"
make -C "$checkout_dir" bootstrap
make -C "$checkout_dir" check
make -C "$checkout_dir" qualification-m3
make -C "$checkout_dir" qualification-m4
