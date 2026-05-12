#!/usr/bin/env sh
set -eu

PORT="${PORT:-8000}"
BIND_HOST="${HOST:-0.0.0.0}"
ENABLE_DNS_REBINDING_PROTECTION="${ENABLE_DNS_REBINDING_PROTECTION:-0}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --host)
      BIND_HOST="$2"
      shift 2
      ;;
    --enable-dns-rebinding-protection)
      ENABLE_DNS_REBINDING_PROTECTION="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: ./start-server.sh [--port PORT] [--host HOST] [--enable-dns-rebinding-protection 0|1]
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [ "$ENABLE_DNS_REBINDING_PROTECTION" != "0" ] && [ "$ENABLE_DNS_REBINDING_PROTECTION" != "1" ]; then
  echo "ENABLE_DNS_REBINDING_PROTECTION must be 0 or 1" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS_PATH="$SCRIPT_DIR/requirements.txt"
REQUIREMENTS_HASH_PATH="$VENV_DIR/.requirements.sha256"

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    return 1
  fi
}

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtual environment..."
  PYTHON_BIN="$(find_python)" || { echo "Python is not installed or not in PATH." >&2; exit 1; }
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
[ -x "$VENV_PYTHON" ] || { echo "Virtual environment Python executable not found in: $VENV_DIR" >&2; exit 1; }

ensure_pip_available() {
  if "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    return 0
  fi

  echo "pip module not found in virtual environment, trying ensurepip..."
  if "$VENV_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1; then
    "$VENV_PYTHON" -m pip --version >/dev/null 2>&1 && return 0
  fi

  if [ -x "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" --version >/dev/null 2>&1 && return 0
  fi

  echo "Unable to initialize pip in virtual environment. Please install python3-venv / python3-pip and retry." >&2
  return 1
}

pip_install() {
  if "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$VENV_PYTHON" -m pip install "$@"
  elif [ -x "$VENV_DIR/bin/pip" ]; then
    "$VENV_DIR/bin/pip" install "$@"
  else
    echo "pip is unavailable in virtual environment." >&2
    return 1
  fi
}

requirements_hash="$(sha256sum "$REQUIREMENTS_PATH" | awk '{print $1}')"
installed_hash=""
if [ -f "$REQUIREMENTS_HASH_PATH" ]; then
  installed_hash="$(tr -d '[:space:]' < "$REQUIREMENTS_HASH_PATH")"
fi

check_required_modules() {
  output="$($VENV_PYTHON - <<'PY'
import importlib
import json

required_modules = ["duckdb", "matplotlib", "scipy", "mcp", "pypdf"]
missing = []
for module_name in required_modules:
    try:
        importlib.import_module(module_name)
    except Exception:
        missing.append(module_name)

print(json.dumps({"ok": len(missing) == 0, "missing": missing}))
raise SystemExit(0 if not missing else 1)
PY
  )"
  status=$?

  if [ -z "$output" ]; then
    echo "Dependency import check failed: no output from Python." >&2
    return 1
  fi

  if [ "$status" -ne 0 ]; then
    missing_modules="$(echo "$output" | "$VENV_PYTHON" -c 'import json,sys; d=json.loads(sys.stdin.read()); print(", ".join(d.get("missing", [])))' 2>/dev/null || true)"
    if [ -n "$missing_modules" ]; then
      echo "Dependency import check failed. Missing modules: $missing_modules" >&2
    else
      echo "Dependency import check failed." >&2
    fi
    return 1
  fi
  return 0
}

needs_install=false
if [ "$requirements_hash" != "$installed_hash" ]; then
  needs_install=true
else
  if ! check_required_modules; then
    echo "Detected missing/broken dependencies in virtual environment. Reinstalling..."
    needs_install=true
  fi
fi

if [ "$needs_install" = "true" ]; then
  echo "Installing dependencies from requirements.txt..."
  ensure_pip_available
  pip_install -r "$REQUIREMENTS_PATH"
  printf '%s' "$requirements_hash" > "$REQUIREMENTS_HASH_PATH"
else
  echo "Dependencies are up to date. Skipping install."
fi

export ENABLE_DNS_REBINDING_PROTECTION
export HOST="$BIND_HOST"
export PORT

echo "Starting server on ${BIND_HOST}:${PORT} (ENABLE_DNS_REBINDING_PROTECTION=$ENABLE_DNS_REBINDING_PROTECTION)..."
exec "$VENV_PYTHON" "$SCRIPT_DIR/server.py"
