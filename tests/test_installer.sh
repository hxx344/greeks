#!/usr/bin/env bash
# Pure filesystem/mocked-service checks; compatible with Linux and Git Bash.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
export GREEKS_INSTALL_SOURCE_ONLY=1
source ./install.sh
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
APP_DIR="$temporary/app"
mkdir -p "$APP_DIR/releases/old" "$APP_DIR/releases/new"
atomic_record "$APP_DIR/.record" 'exact value'
[[ "$(cat "$APP_DIR/.record")" == 'exact value' ]]
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) echo 'Windows: symlink checks run in Linux CI.' ;;
  *)
    atomic_link "$APP_DIR/releases/old" "$APP_DIR/current"
    atomic_link "$APP_DIR/releases/new" "$APP_DIR/current"
    [[ "$(readlink "$APP_DIR/current")" == "$APP_DIR/releases/new" ]]
    ln -s "$APP_DIR/releases" "$temporary/unsafe"
    if (real_directory "$temporary/unsafe") 2>/dev/null; then echo 'symlink directory accepted' >&2; exit 1; fi
    ln -s "$APP_DIR/.record" "$temporary/unsafe-file"
    if (regular_file "$temporary/unsafe-file") 2>/dev/null; then echo 'symlink file accepted' >&2; exit 1; fi
    ;;
esac
if (real_directory "$APP_DIR/.record") 2>/dev/null; then echo 'regular file accepted as directory' >&2; exit 1; fi
if (regular_file "$APP_DIR/releases") 2>/dev/null; then echo 'directory accepted as file' >&2; exit 1; fi
dependencies="$APP_DIR/dependencies"
mkdir -p "$dependencies/bin"
touch "$dependencies/.complete" "$dependencies/pyvenv.cfg"
if dependencies_ready; then echo 'missing interpreter accepted' >&2; exit 1; fi
printf '#!/bin/sh\nexit 0\n' > "$dependencies/bin/python"
chmod +x "$dependencies/bin/python"
dependencies_ready
work_dir=$(mktemp -d "$APP_DIR/.install.XXXXXXXX")
touch "$work_dir/scratch"
cleanup
[[ -z "$work_dir" ]]
[[ -f "$APP_DIR/.record" ]]
echo 'Installer filesystem checks passed.'
