#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${VERIFY_PYTHON:-}" ]]; then
  python_bin="$VERIFY_PYTHON"
elif [[ -x "$project_root/.venv/bin/python" ]]; then
  python_bin="$project_root/.venv/bin/python"
else
  python_bin="python3"
fi
exec "$python_bin" "$project_root/scripts/verify.py" "$@"
