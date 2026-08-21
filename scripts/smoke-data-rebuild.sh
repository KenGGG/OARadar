#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_root="$(cd -- "$script_dir/.." && pwd -P)"
smoke_root="$(mktemp -d /tmp/oaradar-rebuild-smoke.XXXXXXXX)"

cleanup() {
  case "$smoke_root" in
    /tmp/oaradar-rebuild-smoke.*)
      rm -rf -- "$smoke_root"
      ;;
    *)
      printf 'refusing to remove unexpected smoke directory\n' >&2
      return 1
      ;;
  esac
}
trap cleanup EXIT

export TMPDIR="$smoke_root"
export OARADAR_SYNTHETIC_REBUILD_SMOKE=1

cd -- "$project_root"
uv run pytest tests/test_rebuild_e2e.py -v --basetemp "$smoke_root/pytest"
