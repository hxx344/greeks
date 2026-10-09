#!/usr/bin/env bash
# Run only on a disposable Ubuntu 24.04 CI runner. No exchange traffic is made.
set -Eeuo pipefail
[[ ${GREEKS_DISPOSABLE_CI:-} == 1 && $EUID == 0 ]] || { echo 'Disposable CI root runner required.' >&2; exit 1; }
cd "$(dirname "$0")/.."
[[ ! -e /opt/greeks && ! -e /etc/greeks && ! -e /var/lib/greeks ]] || { echo 'Existing install: refusing CI test.' >&2; exit 1; }
[[ ! -e /run/greeks-installer && ! -L /run/greeks-installer ]] || { echo 'Existing installer lock directory: refusing CI test.' >&2; exit 1; }
fixture=$(mktemp -d)
installer=$PWD/install.sh
finish() {
  systemctl stop greeks >/dev/null 2>&1 || true
  rm -rf -- "$fixture"
}
trap finish EXIT
git -c safe.directory="$PWD" archive HEAD | tar -xf - -C "$fixture"
cat > "$fixture/app/main.py" <<'PY'
"""CI-only isolated app: exercises systemd, credentials and degraded health."""
import base64
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get('/api/health')
def health(request: Request):
    expected = 'Basic ' + base64.b64encode((os.environ['DASHBOARD_USERNAME'] + ':' + os.environ['DASHBOARD_PASSWORD']).encode()).decode()
    if request.headers.get('authorization') != expected:
        return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
    return {'status': 'degraded', 'environment': os.environ['TRADING_MODE'],
            'live_enabled': False, 'trading_enabled': False, 'reconciliation': {}}
PY
git -C "$fixture" init -q -b main
git -C "$fixture" config user.email installer-test@example.invalid
git -C "$fixture" config user.name 'Installer CI'
git -C "$fixture" add .
git -C "$fixture" commit -qm fixture
run_install() {
  PROJECT_DEPLOY_MODE=source GREEKS_INSTALL_SOURCE_ONLY=1 INSTALLER_PATH="$installer" TEST_REPOSITORY="$fixture" bash -c '
    source "$INSTALLER_PATH"
    REPOSITORY=$TEST_REPOSITORY
    wait_healthy() { local i; for i in {1..8}; do if healthy; then return 0; fi; sleep 1; done; return 1; }
    main
  '
}
pid() { systemctl show greeks -p MainPID --value; }
assert_preserved() {
  cmp -s "$fixture/expected.env" /etc/greeks/greeks.env
  [[ $(cat /var/lib/greeks/engine_state.json) == 'state-must-survive' ]]
  [[ $(readlink /opt/greeks/current/.venv) == "$dependencies" ]]
}

# Reject pre-created symlinks before any package, application or service mutation.
mkdir "$fixture/unsafe-lock-directory"
printf 'lock-target-must-survive' > "$fixture/unsafe-lock-directory/install.lock"
ln -s "$fixture/unsafe-lock-directory" /run/greeks-installer
if run_install; then echo 'Symlink lock directory unexpectedly accepted.' >&2; exit 1; fi
[[ $(cat "$fixture/unsafe-lock-directory/install.lock") == 'lock-target-must-survive' ]]
[[ ! -e /opt/greeks ]]
rm -- /run/greeks-installer
mkdir -m 0700 /run/greeks-installer
ln -s "$fixture/unsafe-lock-directory/install.lock" /run/greeks-installer/install.lock
if run_install; then echo 'Symlink lock file unexpectedly accepted.' >&2; exit 1; fi
[[ $(cat "$fixture/unsafe-lock-directory/install.lock") == 'lock-target-must-survive' ]]
[[ ! -e /opt/greeks ]]
rm -- /run/greeks-installer/install.lock
chmod 0777 /run/greeks-installer
if run_install; then echo 'Writable lock directory unexpectedly accepted.' >&2; exit 1; fi
[[ ! -e /opt/greeks ]]
chmod 0700 /run/greeks-installer
chown nobody /run/greeks-installer
if run_install; then echo 'Non-root lock directory unexpectedly accepted.' >&2; exit 1; fi
[[ ! -e /opt/greeks ]]
chown root /run/greeks-installer
run_install > "$fixture/first-install.log" 2>&1 || { cat "$fixture/first-install.log"; exit 1; }
cat "$fixture/first-install.log"
panel_password=$(sed -n 's/^DASHBOARD_PASSWORD=//p' /etc/greeks/greeks.env)
grep -Fxq '[greeks] 用户名：admin' "$fixture/first-install.log"
grep -Fxq "[greeks] 密码：$panel_password" "$fixture/first-install.log"
grep -Fq "sudo grep '^DASHBOARD_' /etc/greeks/greeks.env" "$fixture/first-install.log"
systemctl is-active --quiet greeks
[[ $(stat -c %a /etc/greeks/greeks.env) == 640 ]]
[[ $(stat -c %U /opt/greeks/current/app/main.py) == root ]]
[[ $(systemctl show greeks -p User --value) == greeks ]]
[[ $(stat -c %u:%a /run/greeks-installer) == 0:700 ]]
[[ $(stat -c %u:%a /run/greeks-installer/install.lock) == 0:600 ]]
grep -qx 'TRADING_MODE=dry-run' /etc/greeks/greeks.env
grep -qx 'AUTO_OPEN=false' /etc/greeks/greeks.env
grep -qx 'LIVE_TRADING=false' /etc/greeks/greeks.env
grep -qx 'BYBIT_TESTNET=false' /etc/greeks/greeks.env
grep -qx 'STRATEGY_MODE=iron_condor' /etc/greeks/greeks.env
grep -qx 'MAX_MARGIN_USD=2500' /etc/greeks/greeks.env
grep -qx 'OPEN_WINDOW_SECONDS=300' /etc/greeks/greeks.env
grep -qx 'MARGIN_MODE=PORTFOLIO_MARGIN' /etc/greeks/greeks.env
grep -qx 'BBO_ORDER_TIMEOUT_SECONDS=280' /etc/greeks/greeks.env
grep -qx 'ALLOW_MARKET_FALLBACK=true' /etc/greeks/greeks.env
dependencies=$(readlink /opt/greeks/current/.venv)
dependency_stamp=$(stat -c %Y "$dependencies/.complete")
first_pid=$(pid)

# Read the same literal dotenv semantics as runtime, including a bare optional key.
cat > "$fixture/access.env" <<'ENV'
DASHBOARD_USERNAME
DASHBOARD_PASSWORD='literal${PATH}$(touch forbidden)password'
BYBIT_API_SECRET=NEVER-PRINT-EXCHANGE-SECRET
ENV
access_hash=$(sha256sum "$fixture/access.env")
GREEKS_INSTALL_SOURCE_ONLY=1 INSTALLER_PATH="$installer" TEST_ENV="$fixture/access.env" bash -c '
  source "$INSTALLER_PATH"
  ENV_FILE=$TEST_ENV
  run_as_service() { "$@"; }
  show_access_info
' > "$fixture/access.log"
grep -Fxq '[greeks] 用户名：admin' "$fixture/access.log"
grep -Fxq '[greeks] 密码：literal${PATH}$(touch forbidden)password' "$fixture/access.log"
if grep -Fq 'NEVER-PRINT-EXCHANGE-SECRET' "$fixture/access.log"; then echo 'Exchange secret printed.' >&2; exit 1; fi
[[ ! -e forbidden && "$(sha256sum "$fixture/access.env")" == "$access_hash" ]]

run_install > "$fixture/noop-install.log" 2>&1 || { cat "$fixture/noop-install.log"; exit 1; }
cat "$fixture/noop-install.log"
grep -Fq "sudo grep '^DASHBOARD_' /etc/greeks/greeks.env" "$fixture/noop-install.log"
if grep -Fq "$panel_password" "$fixture/noop-install.log"; then echo 'Update repeated the panel password.' >&2; exit 1; fi
[[ $(pid) == "$first_pid" ]]
[[ $(stat -c %Y "$dependencies/.complete") == "$dependency_stamp" ]]

# Existing settings, keys and state survive both configuration and code updates.
sed -i 's/TRADING_MODE=dry-run/TRADING_MODE=testnet/; s/AUTO_OPEN=false/AUTO_OPEN=true/; s/STRATEGY_MODE=iron_condor/STRATEGY_MODE=short_strangle/; s/MAX_MARGIN_USD=2500/MAX_MARGIN_USD=1750/; s/OPEN_WINDOW_SECONDS=300/OPEN_WINDOW_SECONDS=125/' /etc/greeks/greeks.env
printf "BYBIT_API_SECRET='literal\044{PATH}\044(touch forbidden)'\n" >> /etc/greeks/greeks.env
printf 'state-must-survive' > /var/lib/greeks/engine_state.json
chown greeks:greeks /var/lib/greeks/engine_state.json
cp /etc/greeks/greeks.env "$fixture/expected.env"
run_install > "$fixture/config-install.log" 2>&1 || { cat "$fixture/config-install.log"; exit 1; }
cat "$fixture/config-install.log"
grep -Fq "sudo grep '^DASHBOARD_' /etc/greeks/greeks.env" "$fixture/config-install.log"
if grep -Fq "$panel_password" "$fixture/config-install.log" || grep -Fq 'literal${PATH}' "$fixture/config-install.log"; then
  echo 'Update printed configuration secrets.' >&2; exit 1
fi
assert_preserved
configured_pid=$(pid)
[[ "$configured_pid" != "$first_pid" ]]
[[ ! -e /opt/greeks/current/forbidden ]]

# Upgrading a partial configuration fills missing fields without replacing custom values.
sed -i '/^MAX_SPREAD_BPS=/d' /etc/greeks/greeks.env
run_install
grep -qx 'MAX_SPREAD_BPS=0' /etc/greeks/greeks.env
"/opt/greeks/current/.venv/bin/python" - "$fixture/expected.env" /etc/greeks/greeks.env <<'PY'
import sys
from dotenv import dotenv_values
assert dict(dotenv_values(sys.argv[1], interpolate=False)) == dict(dotenv_values(sys.argv[2], interpolate=False))
PY
cp /etc/greeks/greeks.env "$fixture/expected.env"
configured_pid=$(pid)
assert_preserved

# Documentation-only changes should not create a release or restart the service.
printf '\nCI documentation change\n' >> "$fixture/README.md"
git -C "$fixture" add README.md
git -C "$fixture" commit -qm docs
run_install > "$fixture/docs-install.log" 2>&1 || { cat "$fixture/docs-install.log"; exit 1; }
cat "$fixture/docs-install.log"
grep -Fq "sudo grep '^DASHBOARD_' /etc/greeks/greeks.env" "$fixture/docs-install.log"
if grep -Fq "$panel_password" "$fixture/docs-install.log"; then echo 'Documentation update repeated the panel password.' >&2; exit 1; fi
[[ $(pid) == "$configured_pid" ]]
assert_preserved

# Runtime changes get a fresh release while reusing the unchanged locked environment.
printf '\n# Runtime update\n' >> "$fixture/app/main.py"
git -C "$fixture" add app/main.py
git -C "$fixture" commit -qm runtime
old_release=$(readlink /opt/greeks/current)
run_install
assert_preserved
[[ $(readlink /opt/greeks/current) != "$old_release" ]]
[[ $(stat -c %Y "$dependencies/.complete") == "$dependency_stamp" ]]
good_release=$(readlink /opt/greeks/current)
good_state=$(cat /opt/greeks/.deployed-state)

# Failure after activation restores code, configuration and service, never state.
printf '\nraise RuntimeError("deliberate CI startup failure")\n' >> "$fixture/app/main.py"
git -C "$fixture" add app/main.py
git -C "$fixture" commit -qm broken
sed -i 's/PORT=8000/PORT=8001/' /etc/greeks/greeks.env
if run_install; then echo 'Broken release unexpectedly installed.' >&2; exit 1; fi
[[ $(readlink /opt/greeks/current) == "$good_release" ]]
[[ $(cat /opt/greeks/.deployed-state) == "$good_state" ]]
systemctl is-active --quiet greeks
assert_preserved
compgen -G '/etc/greeks/greeks.env.failed-*' >/dev/null
[[ -z $(find /opt/greeks -maxdepth 1 -name '.install.*' -print -quit) ]]

# A second process must stop before package operations or service changes.
before_lock=$(pid)
(
  flock -n 8
  if run_install; then echo 'Concurrent installer unexpectedly ran.' >&2; exit 1; fi
) 8>>/run/greeks-installer/install.lock
[[ $(pid) == "$before_lock" ]]
# Migrate the same installation to verified CI archives; Python environments and
# literal user settings survive download rejection and activation failure.
git -C "$fixture" revert --no-edit HEAD >/dev/null
build_ci_package() {
  python3 "$fixture/deploy/package-release.py" --root "$fixture" --output "$fixture/ci-release"
}
run_ci_install() {
  env -u PROJECT_DEPLOY_MODE GREEKS_INSTALL_SOURCE_ONLY=1 INSTALLER_PATH="$installer" CI_FIXTURE="$fixture" bash -c '
    source "$INSTALLER_PATH"
    curl() {
      local argument url="" target="" name
      for argument in "$@"; do [[ $argument != https://* ]] || url=$argument; done
      while (( $# )); do if [[ $1 == -o ]]; then target=$2; break; fi; shift; done
      [[ $url == https://github.com/hxx344/greeks/releases/* && -n $target ]] || return 1
      name=${url##*/}
      [[ $name == release-manifest.json ]] || printf "archive\\n" >> "$CI_FIXTURE/ci-downloads"
      cp "$CI_FIXTURE/ci-release/$name" "$target"
    }
    wait_healthy() { local i; for i in {1..8}; do if healthy; then return 0; fi; sleep 1; done; return 1; }
    main
  '
}
build_ci_package
run_ci_install
assert_preserved
[[ $(stat -c %Y "$dependencies/.complete") == "$dependency_stamp" ]]
[[ -f /opt/greeks/current/.release-application-key ]]
ci_pid=$(pid)
ci_release=$(readlink /opt/greeks/current)
run_ci_install
[[ $(pid) == "$ci_pid" && $(readlink /opt/greeks/current) == "$ci_release" ]]
[[ $(wc -l < "$fixture/ci-downloads") == 1 ]]
printf '\nCI docs-only update\n' >> "$fixture/README.md"
git -C "$fixture" add README.md
git -C "$fixture" commit -qm ci-docs
build_ci_package
run_ci_install
[[ $(pid) == "$ci_pid" && $(readlink /opt/greeks/current) == "$ci_release" ]]
[[ $(wc -l < "$fixture/ci-downloads") == 1 ]]
printf '\nraise RuntimeError("CI package startup failure")\n' >> "$fixture/app/main.py"
git -C "$fixture" add app/main.py
git -C "$fixture" commit -qm ci-startup-failure
build_ci_package
cp "$fixture/ci-release/greeks-linux.tar.gz" "$fixture/good-archive"
printf 'corrupt archive\n' > "$fixture/ci-release/greeks-linux.tar.gz"
if run_ci_install; then echo 'Corrupt CI package unexpectedly installed.' >&2; exit 1; fi
[[ $(pid) == "$ci_pid" && $(readlink /opt/greeks/current) == "$ci_release" ]]
assert_preserved
cp "$fixture/good-archive" "$fixture/ci-release/greeks-linux.tar.gz"
if run_ci_install; then echo 'Broken CI runtime unexpectedly installed.' >&2; exit 1; fi
[[ $(readlink /opt/greeks/current) == "$ci_release" ]]
systemctl is-active --quiet greeks
assert_preserved
echo 'Source/CI install, no-op, dependency reuse, corrupt package rejection and rollback passed.'
