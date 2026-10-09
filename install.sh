#!/usr/bin/env bash
# Idempotent Debian 12/13 and Ubuntu 24.04 deployment. Never source an .env file.
set -Eeuo pipefail

APP_DIR=/opt/greeks
DATA_DIR=/var/lib/greeks
CONFIG_DIR=/etc/greeks
ENV_FILE=$CONFIG_DIR/greeks.env
UNIT_FILE=/etc/systemd/system/greeks.service
LOCK_DIR=/run/greeks-installer
SERVICE=greeks
SERVICE_USER=greeks
REPOSITORY=https://github.com/hxx344/greeks.git
BRANCH=main
UV_VERSION=0.12.10
INSTALL_REVISION=1
PYTHON_BIN=/usr/bin/python3
work_dir=
old_release=
old_unit_backup=
old_environment=
activation_started=0

log() { printf '[greeks] %s\n' "$*"; }
fail() { log "错误：$*" >&2; return 1; }
hash() { sha256sum | cut -d ' ' -f 1; }
atomic_record() {
  local file=$1 value=$2 temporary="${1}.new.$$"
  printf '%s\n' "$value" > "$temporary"
  mv -f -- "$temporary" "$file"
}
atomic_link() {
  local target=$1 link=$2
  ln -s "$target" "${link}.new.$$"
  mv -Tf -- "${link}.new.$$" "$link"
}
real_directory() {
  local path=$1
  [[ ! -L "$path" && (! -e "$path" || -d "$path") ]] || { fail "目录不是安全的普通目录：$path"; return 1; }
  [[ "$(realpath -m "$path")" == "$path" ]] || fail "目录包含符号链接：$path"
}
regular_file() {
  [[ ! -L "$1" && (! -e "$1" || -f "$1") ]] || fail "配置或记录不是普通文件：$1"
}
acquire_lock() {
  local lock_file="$LOCK_DIR/install.lock" parent_mode
  real_directory /run || return 1
  parent_mode=$(stat -c %a /run)
  [[ "$(stat -c %u /run)" == 0 ]] && (( (8#$parent_mode & 022) == 0 )) || {
    fail '/run 必须由 root 所有且禁止组或其他用户写入。'; return 1;
  }
  # Atomic creation under a trusted parent; never chown/chmod an unknown directory.
  mkdir -m 0700 -- "$LOCK_DIR" 2>/dev/null || true
  real_directory "$LOCK_DIR" || return 1
  [[ -d "$LOCK_DIR" && "$(stat -c %u "$LOCK_DIR")" == 0 && "$(stat -c %a "$LOCK_DIR")" == 700 ]] || {
    fail '安装锁目录必须为 root 所有的 0700 普通目录。'; return 1;
  }
  regular_file "$lock_file" || return 1
  if [[ ! -e "$lock_file" ]]; then
    # Concurrent installers may both reach creation; neither may truncate a file.
    (umask 077; set -o noclobber; : > "$lock_file") 2>/dev/null || true
  fi
  [[ -f "$lock_file" && ! -L "$lock_file" && "$(stat -c %u "$lock_file")" == 0 &&
     "$(stat -c %a "$lock_file")" == 600 && "$(stat -c %h "$lock_file")" == 1 ]] || {
    fail '安装锁必须为 root 所有的 0600 普通单链接文件。'; return 1;
  }
  # The verified 0700 root-owned directory prevents unprivileged replacement.
  exec 9>>"$lock_file"
  flock -n 9 || fail '另一个 Greeks 安装进程正在运行。'
}
cleanup() {
  [[ -n "$work_dir" && -d "$work_dir" && ! -L "$work_dir" && "$work_dir" == "$APP_DIR"/.install.* ]] || return 0
  rm -rf -- "$work_dir"
  work_dir=
}
ensure_os() {
  local os version
  os=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
  version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')
  case "$os:$version" in debian:12|debian:13|ubuntu:24.04) ;; *) fail '支持 Debian 12/13 和 Ubuntu 24.04；不自动替换旧系统的 Python。' ;; esac
}
ensure_tools() {
  local missing=() tool package
  for tool in curl git tar runuser useradd; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      case "$tool" in runuser) package=util-linux ;; useradd) package=passwd ;; *) package=$tool ;; esac
      missing+=("$package")
    fi
  done
  [[ -x "$PYTHON_BIN" ]] || missing+=(python3)
  dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed' || missing+=(python3-venv)
  [[ -s /etc/ssl/certs/ca-certificates.crt ]] || missing+=(ca-certificates)
  if ((${#missing[@]})); then
    log "安装缺少的系统依赖：${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
  else log '系统依赖齐全，跳过安装。'; fi
  "$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11)' || fail '需要系统 Python 3.11 或更新版本。'
}
uv_supported() {
  local output program version metadata
  output=$("$1" --version) || return 1
  read -r program version metadata <<< "$output"
  [[ "$program" == uv && "$version" == "$UV_VERSION" ]]
}
ensure_uv() {
  UV_BIN="$APP_DIR/tools/bin/uv"
  if [[ -x "$UV_BIN" ]] && uv_supported "$UV_BIN"; then return; fi
  log "安装固定版本 uv $UV_VERSION。"
  "$PYTHON_BIN" -m venv "$APP_DIR/tools"
  "$APP_DIR/tools/bin/python" -m pip install --disable-pip-version-check --only-binary=:all: "uv==$UV_VERSION"
  uv_supported "$UV_BIN" || fail 'uv 版本验证失败。'
}
run_as_service() {
  runuser -u "$SERVICE_USER" -- env -i "HOME=$DATA_DIR" PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 "$@"
}
default_environment() {
  [[ ! -e "$ENV_FILE" ]] || return 0
  "$PYTHON_BIN" - "$ENV_FILE" "$DATA_DIR" <<'PY'
import os, secrets, sys
path, data = sys.argv[1:]
with open(path, "x", encoding="utf-8") as file:
    os.chmod(path, 0o640)
    file.write("HOST=127.0.0.1\nPORT=8000\nDASHBOARD_USERNAME=admin\n")
    file.write(f"DASHBOARD_PASSWORD={secrets.token_urlsafe(32)}\n")
    file.write("TRADING_MODE=dry-run\nLIVE_TRADING=false\nAUTO_OPEN=false\nBYBIT_TESTNET=false\n")
    file.write(f"STATE_FILE={data}/engine_state.json\nBYBIT_API_KEY=\nBYBIT_API_SECRET=\n")
PY
  chown "root:$SERVICE_USER" "$ENV_FILE"
  log '已生成独立面板密码，安装成功后显示登录凭据。'
}
show_access_info() {
  if [[ -z "$old_release" ]]; then
    log '首次安装成功，Greeks 面板登录凭据：'
    run_as_service "$APP_DIR/current/.venv/bin/python" - "$ENV_FILE" <<'PY'
import sys
from dotenv import dotenv_values

values = {key: value for key, value in dotenv_values(sys.argv[1], interpolate=False).items() if value is not None}
print(f"[greeks] 用户名：{values.get('DASHBOARD_USERNAME', 'admin')}")
print(f"[greeks] 密码：{values.get('DASHBOARD_PASSWORD', '')}")
PY
  fi
  log "查看当前用户名和密码：sudo grep '^DASHBOARD_' $ENV_FILE"
  log '在工作台“项目管理 → Greeks · BTC 期权”中保存面板用户名和密码。'
  log '服务日志：sudo journalctl -u greeks -n 50 --no-pager'
}
tree_hash() { git --git-dir="$APP_DIR/repository.git" ls-tree -r "$commit" -- "$@" | hash; }
input_keys() {
  local dependency_tree application_tree check_tree
  dependency_tree=$(tree_hash pyproject.toml uv.lock)
  application_tree=$(tree_hash app deploy)
  check_tree=$(tree_hash app deploy tests/test_installer.py .env.example)
  dependency_key=$(printf 'dependencies-v1\n%s\n%s' "$dependency_tree" "$runtime_key" | hash)
  application_key=$(printf 'application-v1\n%s\n%s' "$application_tree" "$dependency_key" | hash)
  validation_key=$(printf 'validation-v1\n%s\n%s' "$check_tree" "$dependency_key" | hash)
  dependencies="$APP_DIR/dependencies/$dependency_key"
}
dependencies_ready() {
  [[ -f "$dependencies/.complete" && -x "$dependencies/bin/python" && -f "$dependencies/pyvenv.cfg" ]]
}
prepare_source() {
  [[ ! -d "$work_dir/source" ]] || return 0
  mkdir "$work_dir/source"
  git --git-dir="$APP_DIR/repository.git" archive "$commit" | tar -xf - -C "$work_dir/source"
  chmod 0755 "$work_dir"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$work_dir/source"
}
prepare_dependencies() {
  if dependencies_ready; then log '锁定依赖与 Python 环境未变化，复用依赖缓存。'; return; fi
  real_directory "$dependencies"
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$dependencies"
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR/download-cache"
  # An incomplete directory is exclusively installer-owned and may be repaired.
  chown -R "$SERVICE_USER:$SERVICE_USER" "$dependencies"
  prepare_source
  log '同步发生变化或缺失的锁定依赖。'
  (cd "$work_dir/source" && run_as_service env UV_PROJECT_ENVIRONMENT="$dependencies" \
    UV_CACHE_DIR="$APP_DIR/download-cache" UV_PYTHON_DOWNLOADS=never \
    "$UV_BIN" sync --locked --no-dev --no-install-project --python "$PYTHON_BIN")
  chown -R root:root "$dependencies"
  chmod -R go-w "$dependencies"
  atomic_record "$dependencies/.complete" "$dependency_key"
}
prepare_application() {
  prepare_dependencies
  if [[ ! -f "$APP_DIR/cache/validated-$validation_key" ]]; then
    prepare_source
    log '校验发生变化的源码与部署边界。'
    (cd "$work_dir/source" && run_as_service "$dependencies/bin/python" -m compileall -q app deploy &&
      run_as_service "$dependencies/bin/python" tests/test_installer.py)
    atomic_record "$APP_DIR/cache/validated-$validation_key" "$validation_key"
  else log '复用相同源码和环境的验证结果。'; fi
  if [[ -n "$old_release" && -f "$old_release/.application-key" &&
        "$(cat "$old_release/.application-key")" == "$application_key" &&
        -f "$old_release/app/main.py" && -f "$old_release/deploy/runtime.py" &&
        -L "$old_release/.venv" && "$(readlink "$old_release/.venv")" == "$dependencies" ]]; then
    release=$old_release
    return
  fi
  release="$APP_DIR/releases/${commit:0:12}-${application_key:0:16}"
  real_directory "$release"
  if [[ ! -e "$release" ]]; then
    mkdir "$work_dir/publish"
    # Re-extract as root so validation cannot modify the runtime code.
    git --git-dir="$APP_DIR/repository.git" archive "$commit" app deploy | tar -xf - -C "$work_dir/publish"
    ln -s "$dependencies" "$work_dir/publish/.venv"
    atomic_record "$work_dir/publish/.application-key" "$application_key"
    atomic_record "$work_dir/publish/.managed-release" "$commit"
    chmod -R go-w "$work_dir/publish"
    mv -- "$work_dir/publish" "$release"
  fi
  [[ -f "$release/.managed-release" && -f "$release/.application-key" &&
     "$(cat "$release/.application-key")" == "$application_key" &&
     -f "$release/deploy/runtime.py" && -f "$release/app/main.py" &&
     -L "$release/.venv" && "$(readlink "$release/.venv")" == "$dependencies" ]] || fail '候选版本目录不完整。'
}
write_unit() {
  cat > "$work_dir/service" <<EOF
# Managed by greeks installer
[Unit]
Description=Greeks BTC options dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR/current
ExecStart=$APP_DIR/current/.venv/bin/python $APP_DIR/current/deploy/runtime.py serve --env $ENV_FILE
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
}
healthy() {
  systemctl is-active --quiet "$SERVICE" &&
    run_as_service "$APP_DIR/current/.venv/bin/python" "$APP_DIR/current/deploy/runtime.py" health --env "$ENV_FILE"
}
wait_healthy() {
  local attempt
  for attempt in {1..30}; do if healthy; then return 0; fi; sleep 1; done
  return 1
}
remember_environment() {
  install -m 0600 "$ENV_FILE" "$APP_DIR/.last-successful.env.new.$$"
  mv -f -- "$APP_DIR/.last-successful.env.new.$$" "$APP_DIR/.last-successful.env"
}
publish_environment() {
  if ! cmp -s "$1" "$ENV_FILE"; then
    install -m 0640 -o root -g "$SERVICE_USER" "$1" "$ENV_FILE.new.$$"
    mv -f -- "$ENV_FILE.new.$$" "$ENV_FILE"
  fi
}
rollback() {
  local code=${1:-$?}
  trap - ERR INT TERM
  set +e
  if [[ "$activation_started" == 1 ]]; then
    log '启动失败，正在恢复上次程序和配置；交易状态保持原样。'
    systemctl stop "$SERVICE" >/dev/null 2>&1
    if [[ -n "$old_unit_backup" ]]; then
      install -m 0644 "$old_unit_backup" "$UNIT_FILE"
    else
      systemctl disable "$SERVICE" >/dev/null 2>&1
      rm -f -- "$UNIT_FILE"
    fi
    if [[ -n "$old_release" ]]; then
      if ! cmp -s "$ENV_FILE" "$old_environment"; then
        local failed="$CONFIG_DIR/greeks.env.failed-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        install -m 0600 "$ENV_FILE" "$failed" || { log '候选配置保存失败，服务保持停止。'; cleanup; exit "$code"; }
        install -m 0640 -o root -g "$SERVICE_USER" "$old_environment" "$ENV_FILE.new.$$" || { cleanup; exit "$code"; }
        mv -f -- "$ENV_FILE.new.$$" "$ENV_FILE" || { cleanup; exit "$code"; }
        log "未生效配置已保留：$failed"
      fi
      atomic_link "$old_release" "$APP_DIR/current"
      install -m 0600 "$old_environment" "$APP_DIR/.last-successful.env"
      atomic_record "$APP_DIR/.deployed-state" "$deployed_state"
      systemctl daemon-reload
      systemctl restart "$SERVICE"
      if wait_healthy; then log '已恢复上次成功的程序和配置。'; else log '旧服务未恢复健康，请检查 journalctl -u greeks。'; fi
    else
      [[ ! -L "$APP_DIR/current" ]] || rm -f -- "$APP_DIR/current"
      systemctl daemon-reload
      log '首次安装未成功；保留配置和数据供重试。'
    fi
  fi
  cleanup
  exit "$code"
}
main() {
  [[ $# == 0 ]] || fail '用法：sudo bash install.sh'
  [[ "$(uname -s)" == Linux && "$EUID" == 0 ]] || fail '请在支持的 Linux 上使用 sudo bash install.sh。'
  ensure_os
  command -v systemctl >/dev/null && [[ -d /run/systemd/system ]] || fail '需要正在运行 systemd 的主机。'
  command -v flock >/dev/null || fail '请安装 util-linux 后重试。'
  acquire_lock
  umask 022
  real_directory "$APP_DIR"
  if [[ -d "$APP_DIR" && ! -f "$APP_DIR/.managed-install" ]]; then
    [[ -z "$(ls -A "$APP_DIR")" ]] || fail '安装目录已有非本脚本管理的文件。'
  fi
  local directory file
  for directory in "$APP_DIR/releases" "$APP_DIR/cache" "$APP_DIR/dependencies" "$APP_DIR/tools" "$APP_DIR/download-cache" "$APP_DIR/repository.git" "$DATA_DIR" "$CONFIG_DIR"; do real_directory "$directory"; done
  for file in "$ENV_FILE" "$UNIT_FILE" "$APP_DIR/.last-successful.env" "$APP_DIR/.deployed-state" "$APP_DIR/.managed-install"; do regular_file "$file"; done
  install -d -m 0755 "$APP_DIR" "$APP_DIR/releases" "$APP_DIR/cache" "$APP_DIR/dependencies"
  touch "$APP_DIR/.managed-install"
  work_dir=$(mktemp -d "$APP_DIR/.install.XXXXXXXX")
  trap rollback ERR
  trap 'rollback 130' INT
  trap 'rollback 143' TERM
  if [[ -L "$APP_DIR/current" ]]; then
    old_release=$(realpath -e "$APP_DIR/current")
    [[ "$old_release" == "$APP_DIR/releases/"* && -f "$old_release/.managed-release" ]] || fail 'current 指向未知版本。'
  elif [[ -e "$APP_DIR/current" ]]; then fail 'current 不是受管理的版本链接。'; fi
  if [[ -f "$UNIT_FILE" ]]; then
    grep -qx '# Managed by greeks installer' "$UNIT_FILE" || fail '已有非本脚本管理的 greeks.service。'
    old_unit_backup="$work_dir/previous.service"
    cp -- "$UNIT_FILE" "$old_unit_backup"
  fi
  [[ -z "$old_release" || (-n "$old_unit_backup" && -f "$APP_DIR/.last-successful.env") ]] || fail '缺少上次服务或配置快照，停止切换。'
  ensure_tools
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  [[ "$(id -u "$SERVICE_USER")" != 0 && "$(getent passwd "$SERVICE_USER" | cut -d: -f6)" == "$DATA_DIR" &&
     "$(getent passwd "$SERVICE_USER" | cut -d: -f7)" == /usr/sbin/nologin ]] || fail 'greeks 用户不是安装器管理的专用服务用户。'
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
  install -d -m 0750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
  default_environment
  chown "root:$SERVICE_USER" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  ensure_uv
  runtime_key=$(printf '%s\n%s\n%s\n%s' "$("$PYTHON_BIN" -VV)" "$(sha256sum "$PYTHON_BIN")" "$(uname -m)" "$UV_VERSION" | hash)
  write_unit
  deployment_key=$({ cat "$ENV_FILE" "$work_dir/service"; printf '%s\n%s' "$runtime_key" "$INSTALL_REVISION"; } | hash)
  log '检查远端版本。'
  commit=$(git ls-remote --exit-code "$REPOSITORY" "refs/heads/$BRANCH" | cut -f1)
  [[ "$commit" =~ ^[a-f0-9]{40}$ ]] || fail '无法确定远端提交。'
  deployed_state=
  [[ ! -f "$APP_DIR/.deployed-state" ]] || deployed_state=$(cat "$APP_DIR/.deployed-state")
  if [[ -n "$old_release" && -f "$old_release/.install-ready" &&
        "$deployed_state" == "$commit $deployment_key" && -x "$old_release/.venv/bin/python" &&
        -f "$old_release/.venv/.complete" &&
        -f "$UNIT_FILE" ]] && cmp -s "$work_dir/service" "$UNIT_FILE" && healthy; then
    log "提交 ${commit:0:12}、配置和运行环境未变化；跳过源码下载、依赖同步、验证及重启。"
    cleanup
    trap - ERR INT TERM
    show_access_info
    return
  fi
  [[ -d "$APP_DIR/repository.git" ]] || git init --bare -q "$APP_DIR/repository.git"
  if ! git --git-dir="$APP_DIR/repository.git" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git --git-dir="$APP_DIR/repository.git" fetch --quiet --depth=1 "$REPOSITORY" "$commit"
  else log '源码已缓存，跳过下载。'; fi
  input_keys
  prepare_application
  local added_settings candidate_environment="$work_dir/candidate.env"
  added_settings=$("$dependencies/bin/python" "$release/deploy/configure.py" "$ENV_FILE" "$candidate_environment")
  chown "root:$SERVICE_USER" "$work_dir"
  chmod 0750 "$work_dir"
  chown "root:$SERVICE_USER" "$candidate_environment"
  chmod 0640 "$candidate_environment"
  if [[ "$added_settings" == none ]]; then log '策略参数齐全，保留已有配置。'
  else log "补齐缺少的策略参数：$added_settings；已有参数、运行模式与凭据保持不变。"; fi
  # Validate as the service user without starting the app or loading trading state.
  run_as_service "$dependencies/bin/python" "$release/deploy/runtime.py" validate --env "$candidate_environment" >/dev/null
  deployment_key=$({ cat "$candidate_environment" "$work_dir/service"; printf '%s\n%s' "$runtime_key" "$INSTALL_REVISION"; } | hash)
  if [[ "$release" == "$old_release" && "${deployed_state#* }" == "$deployment_key" ]] &&
      cmp -s "$work_dir/service" "$UNIT_FILE" && healthy; then
    publish_environment "$candidate_environment"
    atomic_record "$APP_DIR/.deployed-state" "$commit $deployment_key"
    log '运行代码未变化，复用当前版本，跳过服务重启。'
    cleanup
    trap - ERR INT TERM
    show_access_info
    return
  fi
  if [[ -n "$old_release" ]]; then
    old_environment="$work_dir/previous.env"
    install -m 0600 "$APP_DIR/.last-successful.env" "$old_environment"
  fi
  activation_started=1
  publish_environment "$candidate_environment"
  atomic_link "$release" "$APP_DIR/current"
  if [[ ! -f "$UNIT_FILE" ]] || ! cmp -s "$work_dir/service" "$UNIT_FILE"; then
    install -m 0644 "$work_dir/service" "$UNIT_FILE"
    systemctl daemon-reload
  fi
  systemctl enable "$SERVICE" >/dev/null
  systemctl restart "$SERVICE"
  wait_healthy || fail '服务未能通过带认证的 /api/health 检查。'
  remember_environment
  atomic_record "$release/.install-ready" "$application_key"
  atomic_record "$APP_DIR/.deployed-state" "$commit $deployment_key"
  activation_started=0
  cleanup
  trap - ERR INT TERM
  log "安装完成：${commit:0:12}，greeks.service；配置与交易状态已保留。"
  show_access_info
}

# Tests source helpers without running any system changes.
if [[ "${GREEKS_INSTALL_SOURCE_ONLY:-0}" != 1 ]]; then main "$@"; fi
