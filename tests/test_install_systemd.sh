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
  GREEKS_INSTALL_SOURCE_ONLY=1 INSTALLER_PATH="$installer" TEST_REPOSITORY="$fixture" bash -c '
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
run_install
systemctl is-active --quiet greeks
[[ $(stat -c %a /etc/greeks/greeks.env) == 640 ]]
[[ $(stat -c %U /opt/greeks/current/app/main.py) == root ]]
[[ $(systemctl show greeks -p User --value) == greeks ]]
[[ $(stat -c %u:%a /run/greeks-installer) == 0:700 ]]
[[ $(stat -c %u:%a /run/greeks-installer/install.lock) == 0:600 ]]
grep -qx 'TRADING_MODE=dry-run' /etc/greeks/greeks.env
grep -qx 'AUTO_OPEN=false' /etc/greeks/greeks.env
grep -qx 'LIVE_TRADING=false' /etc/greeks/greeks.env
dependencies=$(readlink /opt/greeks/current/.venv)
dependency_stamp=$(stat -c %Y "$dependencies/.complete")
first_pid=$(pid)
run_install
[[ $(pid) == "$first_pid" ]]
[[ $(stat -c %Y "$dependencies/.complete") == "$dependency_stamp" ]]

# Existing settings, keys and state survive both configuration and code updates.
sed -i 's/TRADING_MODE=dry-run/TRADING_MODE=testnet/; s/AUTO_OPEN=false/AUTO_OPEN=true/' /etc/greeks/greeks.env
printf "BYBIT_API_SECRET='literal\044{PATH}\044(touch forbidden)'\n" >> /etc/greeks/greeks.env
printf 'state-must-survive' > /var/lib/greeks/engine_state.json
chown greeks:greeks /var/lib/greeks/engine_state.json
cp /etc/greeks/greeks.env "$fixture/expected.env"
run_install
assert_preserved
configured_pid=$(pid)
[[ "$configured_pid" != "$first_pid" ]]
[[ ! -e /opt/greeks/current/forbidden ]]

# Documentation-only changes should not create a release or restart the service.
printf '\nCI documentation change\n' >> "$fixture/README.md"
git -C "$fixture" add README.md
git -C "$fixture" commit -qm docs
run_install
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
echo 'Systemd install, no-op, incremental update, credential preservation and rollback passed.'
