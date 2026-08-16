#!/usr/bin/env bash
# Supervise the opencode stack that backs platform="linux-opencode":
#
#   opencode serve      the agent itself, HTTP on loopback
#   opencode-mcp-bridge an MCP server in front of it, also on loopback
#
# Usage:
#   opencode-mcp.sh write-env   persist provider credentials and settings (0600)
#   opencode-mcp.sh install     install opencode and build the bridge
#   opencode-mcp.sh start       start both, idempotently, and prove MCP works
#   opencode-mcp.sh stop        stop both and free the ports
#   opencode-mcp.sh health      probe both (exit 0 = healthy)
#   opencode-mcp.sh mcp-check   one real MCP initialize against the bridge
#   opencode-mcp.sh watchdog    one supervision tick, for the keep-alive loop
#   opencode-mcp.sh status      print what this script thinks is going on
#
# Conventions are deliberately the same as scripts/pw-mcp.sh, and so are the
# reasons behind them:
#
#   * Never npx and never "| bash" at start time. install resolves a version
#     once; start runs what install produced, so a restart cannot silently
#     change the version and does not need the network.
#   * The inner shell records its own pid and then execs, so the pid file
#     holds the server rather than a wrapper that is gone a moment later, and
#     setsid detaches it from the workflow step's process group.
#   * A listening socket is not health. The bridge is probed on /healthz and,
#     at boot, with a real MCP initialize; opencode itself is probed on an
#     endpoint that only answers once its server is actually up.
#   * Both are probed on localhost AND on 127.0.0.1, because the broker
#     arrives through an SSH forward as 127.0.0.1:<ephemeral port>. A server
#     with a Host allowlist passes the first and fails the second, which looks
#     exactly like a healthy process to everything except the caller.

set -uo pipefail

ENV_FILE="${OPENCODE_ENV_FILE:-/tmp/opencode-mcp.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

SERVE_PORT="${OPENCODE_SERVE_PORT:-4096}"
MCP_PORT="${OPENCODE_MCP_PORT:-8788}"
WORKDIR="${OPENCODE_WORKDIR:-$HOME/workspace}"
BRIDGE_DIR="${OPENCODE_BRIDGE_DIR:-$HOME/.opencode-mcp-bridge}"
BRIDGE_REPO="${OPENCODE_BRIDGE_REPO:-https://github.com/nmt3325/opencode-mcp-bridge.git}"
BRIDGE_REF="${OPENCODE_BRIDGE_REF:-main}"
VERSION="${OPENCODE_VERSION:-latest}"
PERMISSION="${OPENCODE_PERMISSION:-allow}"
TOKEN_FILE="${OPENCODE_TOKEN_FILE:-$HOME/.opencode-mcp-token}"
SERVE_LOG="${OPENCODE_SERVE_LOG:-/tmp/opencode-serve.log}"
MCP_LOG="${OPENCODE_MCP_LOG:-/tmp/opencode-mcp.log}"
SERVE_PID_FILE="${OPENCODE_SERVE_PID_FILE:-/tmp/opencode-serve.pid}"
MCP_PID_FILE="${OPENCODE_MCP_PID_FILE:-/tmp/opencode-mcp.pid}"
STATE_DIR="${OPENCODE_STATE_DIR:-/tmp/opencode-mcp}"
HEALTH_TIMEOUT="${OPENCODE_HEALTH_TIMEOUT:-15}"
START_TIMEOUT="${OPENCODE_START_TIMEOUT:-180}"
FAIL_THRESHOLD="${OPENCODE_FAIL_THRESHOLD:-2}"
RESTART_COOLDOWN="${OPENCODE_RESTART_COOLDOWN:-90}"
MAX_RESTARTS="${OPENCODE_MAX_RESTARTS:-20}"
LOG_MAX_BYTES="${OPENCODE_LOG_MAX_BYTES:-10485760}"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(stamp) opencode-mcp: $*"; }
num() { local v="${1:-}"; [ -n "$v" ] || v=0; printf '%s' "$v"; }
# Reading a counter must not depend on the state dir existing yet: the first
# status call of a boot runs before any tick has created it.
counter() { [ -f "$STATE_DIR/$1" ] && tr -dc '0-9' < "$STATE_DIR/$1" 2> /dev/null; return 0; }
set_counter() { mkdir -p "$STATE_DIR"; printf '%s' "$2" > "$STATE_DIR/$1"; }

resolve_opencode() {
  if command -v opencode > /dev/null 2>&1; then
    OPENCODE_BIN="$(command -v opencode)"
    return 0
  fi
  if [ -x "$HOME/.opencode/bin/opencode" ]; then
    OPENCODE_BIN="$HOME/.opencode/bin/opencode"
    return 0
  fi
  return 1
}

port_pid() {
  local port="$1" pid=""
  if command -v ss > /dev/null 2>&1; then
    pid="$(ss -lntpH "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  fi
  if [ -z "$pid" ] && command -v lsof > /dev/null 2>&1; then
    pid="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1)"
  fi
  printf '%s' "$pid"
}

# The pid we started, or whoever holds the port: adopting the incumbent is how
# a restarted supervisor avoids fighting a healthy server for its own port.
server_pid() {
  local pid_file="$1" port="$2" pid=""
  if [ -f "$pid_file" ]; then
    pid="$(tr -dc '0-9' < "$pid_file")"
    if [ -n "$pid" ] && kill -0 "$pid" 2> /dev/null; then
      printf '%s' "$pid"
      return 0
    fi
  fi
  pid="$(port_pid "$port")"
  [ -n "$pid" ] || return 1
  printf '%s' "$pid"
}

rotate_log() {
  local log="$1" size=0
  [ -f "$log" ] || return 0
  size=$(num "$(wc -c < "$log" 2> /dev/null | tr -dc '0-9')")
  if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
    mv -f "$log" "$log.1" 2> /dev/null || true
    say "rotated $log at $size bytes"
  fi
}

ensure_token() {
  if [ ! -s "$TOKEN_FILE" ]; then
    ( umask 077; openssl rand -hex 24 > "$TOKEN_FILE" )
    say "minted a bridge token in $TOKEN_FILE"
  fi
  chmod 600 "$TOKEN_FILE" 2> /dev/null || true
  TOKEN="$(cat "$TOKEN_FILE")"
}

# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

PROBE_CODE=""
http_code() {
  local url="$1"
  shift
  PROBE_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$HEALTH_TIMEOUT" "$@" "$url" 2> /dev/null)"
  printf '%s' "$PROBE_CODE"
}

# opencode answers /session once its HTTP server is really up; a bare TCP
# connect succeeds several seconds earlier, while every request still 404s.
serve_health() {
  local host code
  for host in localhost 127.0.0.1; do
    code="$(http_code "http://$host:$SERVE_PORT/session")"
    [ "$code" = "200" ] || return 1
  done
  return 0
}

bridge_health() {
  local host code
  ensure_token
  for host in localhost 127.0.0.1; do
    code="$(http_code "http://$host:$MCP_PORT/healthz" -H "authorization: Bearer $TOKEN")"
    if [ "$code" = "404" ]; then
      # A build without /healthz is still fine if it speaks MCP.
      mcp_check "$host" || return 1
      continue
    fi
    if [ "$code" != "200" ]; then
      [ "$host" = "127.0.0.1" ] && say "WARNING localhost answers but Host 127.0.0.1:$MCP_PORT gets $code; the broker tunnels in as 127.0.0.1 and will get the same"
      return 1
    fi
  done
  return 0
}

# A real MCP handshake. "Listening" and "working" are different states, and
# only this tells them apart.
mcp_check() {
  local host="${1:-127.0.0.1}" body code
  ensure_token
  body="$(mktemp)"
  code="$(curl -s -o "$body" -w '%{http_code}' --max-time "$HEALTH_TIMEOUT" \
    -X POST "http://$host:$MCP_PORT/mcp" \
    -H "authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -H 'mcp-protocol-version: 2025-06-18' \
    --data '{"jsonrpc":"2.0","id":"health","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"opencode-mcp-health","version":"1.0"}}}' \
    2> /dev/null)"
  if [ "$code" = "200" ] && grep -q '"protocolVersion"' "$body"; then
    rm -f "$body"
    return 0
  fi
  say "mcp initialize on $host:$MCP_PORT returned $code: $(head -c 200 "$body" 2> /dev/null)"
  rm -f "$body"
  return 1
}

health() { serve_health && bridge_health; }

wait_for() {
  local what="$1" fn="$2" deadline
  deadline=$(( $(date +%s) + START_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if "$fn"; then
      say "$what is healthy"
      return 0
    fi
    sleep 2
  done
  say "ERROR $what not healthy within ${START_TIMEOUT}s"
  return 1
}

# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

stop_one() {
  local name="$1" pid_file="$2" port="$3" pid
  pid="$(server_pid "$pid_file" "$port" || true)"
  if [ -n "$pid" ]; then
    say "stopping $name (pid $pid)"
    kill -TERM "-$pid" 2> /dev/null || kill -TERM "$pid" 2> /dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2> /dev/null || break
      sleep 0.5
    done
    kill -0 "$pid" 2> /dev/null && kill -KILL "$pid" 2> /dev/null
  fi
  pid="$(port_pid "$port")"
  if [ -n "$pid" ]; then
    say "port $port still held by pid $pid; killing"
    kill -KILL "$pid" 2> /dev/null || true
    sleep 1
  fi
  rm -f "$pid_file"
}

spawn() {
  local pid_file="$1" log="$2"
  shift 2
  local launcher=()
  command -v setsid > /dev/null 2>&1 && launcher=(setsid)
  "${launcher[@]}" nohup bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$pid_file" "$@" \
    < /dev/null >> "$log" 2>&1 &
  disown 2> /dev/null || true
}

write_config() {
  local cfg="$HOME/.config/opencode/opencode.json"
  mkdir -p "$(dirname "$cfg")" "$WORKDIR"
  [ -f "$cfg" ] && return 0
  cat > "$cfg" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "permission": {
    "bash": "$PERMISSION",
    "edit": "$PERMISSION",
    "webfetch": "$PERMISSION"
  }
}
JSON
  say "wrote $cfg with permission=$PERMISSION"
}

start_serve() {
  local pid
  if pid="$(server_pid "$SERVE_PID_FILE" "$SERVE_PORT")" && serve_health; then
    say "opencode serve already healthy (pid $pid)"
    return 0
  fi
  [ -n "${pid:-}" ] && stop_one "opencode serve" "$SERVE_PID_FILE" "$SERVE_PORT"
  resolve_opencode || { say "ERROR opencode is not installed; run '$0 install'"; return 1; }
  write_config
  rotate_log "$SERVE_LOG"
  say "starting $OPENCODE_BIN serve --port $SERVE_PORT --hostname 127.0.0.1 (cwd $WORKDIR)"
  # The experimental flag is what exposes the HTTP surface the bridge talks
  # to; on a build where it is already the default it is simply ignored.
  ( cd "$WORKDIR" && OPENCODE_EXPERIMENTAL_HTTPAPI=1 \
    spawn "$SERVE_PID_FILE" "$SERVE_LOG" "$OPENCODE_BIN" serve --port "$SERVE_PORT" --hostname 127.0.0.1 )
  wait_for "opencode serve" serve_health
}

start_bridge() {
  local pid
  if pid="$(server_pid "$MCP_PID_FILE" "$MCP_PORT")" && bridge_health; then
    say "bridge already healthy (pid $pid)"
    return 0
  fi
  [ -n "${pid:-}" ] && stop_one "bridge" "$MCP_PID_FILE" "$MCP_PORT"
  [ -f "$BRIDGE_DIR/dist/index.js" ] || { say "ERROR bridge is not built; run '$0 install'"; return 1; }
  ensure_token
  rotate_log "$MCP_LOG"
  say "starting the MCP bridge on 127.0.0.1:$MCP_PORT"
  OPENCODE_BASE_URL="http://127.0.0.1:$SERVE_PORT" \
  OPENCODE_MCP_TOKEN="$TOKEN" \
  OPENCODE_MCP_DEFAULT_DIRECTORY="$WORKDIR" \
  OPENCODE_MCP_DEFAULT_MODEL="${OPENCODE_MODEL:-}" \
  OPENCODE_MCP_DEFAULT_AGENT="${OPENCODE_AGENT:-}" \
    spawn "$MCP_PID_FILE" "$MCP_LOG" node "$BRIDGE_DIR/dist/index.js" \
      --http --host 127.0.0.1 --port "$MCP_PORT"
  wait_for "the MCP bridge" bridge_health || return 1
  # Prove the protocol works, not just the port, before the job says ready.
  mcp_check 127.0.0.1
}

start() { start_serve && start_bridge; }

stop() {
  stop_one "bridge" "$MCP_PID_FILE" "$MCP_PORT"
  stop_one "opencode serve" "$SERVE_PID_FILE" "$SERVE_PORT"
}

# One supervision tick per component, each with its own counters, so a sick
# bridge is never treated as a reason to restart a perfectly healthy agent.
tick() {
  local name="$1" check="$2" starter="$3" fails now last restarts
  if "$check"; then
    fails=$(num "$(counter "fails-$name")")
    [ "$fails" -gt 0 ] && say "$name healthy again after $fails failed probe(s)"
    set_counter "fails-$name" 0
    return 0
  fi
  fails=$(( $(num "$(counter "fails-$name")") + 1 ))
  set_counter "fails-$name" "$fails"
  say "$name health probe failed ($fails/$FAIL_THRESHOLD)"
  [ "$fails" -ge "$FAIL_THRESHOLD" ] || return 0
  now=$(date +%s)
  last=$(num "$(counter "last-restart-$name")")
  restarts=$(num "$(counter "restarts-$name")")
  if [ "$last" -gt 0 ] && [ $(( now - last )) -lt "$RESTART_COOLDOWN" ]; then
    say "$name restarted $(( now - last ))s ago; inside the ${RESTART_COOLDOWN}s cooldown"
    return 0
  fi
  if [ "$restarts" -ge "$MAX_RESTARTS" ]; then
    say "ERROR $restarts restarts of $name did not help; leaving it down"
    return 1
  fi
  say "restarting $name (attempt $(( restarts + 1 ))/$MAX_RESTARTS)"
  set_counter "last-restart-$name" "$now"
  set_counter "restarts-$name" $(( restarts + 1 ))
  set_counter "fails-$name" 0
  "$starter"
}

watchdog() {
  mkdir -p "$STATE_DIR"
  local rc=0
  # Order matters: the bridge cannot be healthy while the agent behind it is
  # down, so the agent is repaired first and the bridge is judged after.
  tick serve serve_health start_serve || rc=1
  tick bridge bridge_health start_bridge || rc=1
  return "$rc"
}

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

install_opencode() {
  if resolve_opencode && [ "$VERSION" = "latest" ]; then
    say "opencode already present: $($OPENCODE_BIN --version 2>/dev/null || echo unknown)"
    return 0
  fi
  say "installing opencode ($VERSION)"
  if [ "$VERSION" = "latest" ]; then
    curl -fsSL https://opencode.ai/install | bash || return 1
  else
    curl -fsSL https://opencode.ai/install | VERSION="$VERSION" bash || return 1
  fi
  resolve_opencode || { say "ERROR opencode is not on PATH after install"; return 1; }
  # bash -lc, which is how the broker runs every command, reads /etc/profile
  # but not ~/.bashrc, so the PATH entry has to live somewhere a login shell
  # actually sources or "opencode" is missing from every SSH session.
  if ! printf 'export PATH="$HOME/.opencode/bin:$PATH"\n' | sudo -n tee /etc/profile.d/opencode.sh > /dev/null 2>&1; then
    printf 'export PATH="$HOME/.opencode/bin:$PATH"\n' >> "$HOME/.profile"
  fi
  say "installed $($OPENCODE_BIN --version 2>/dev/null || echo unknown)"
}

install_bridge() {
  command -v node > /dev/null 2>&1 || { say "ERROR node is required for the bridge"; return 1; }
  if [ -d "$BRIDGE_DIR/.git" ]; then
    say "updating the bridge checkout to $BRIDGE_REF"
    git -C "$BRIDGE_DIR" fetch --depth=1 origin "$BRIDGE_REF" || return 1
    git -C "$BRIDGE_DIR" reset --hard FETCH_HEAD || return 1
  else
    say "cloning $BRIDGE_REPO@$BRIDGE_REF"
    rm -rf "$BRIDGE_DIR"
    git clone --depth=1 --branch "$BRIDGE_REF" "$BRIDGE_REPO" "$BRIDGE_DIR" || return 1
  fi
  ( cd "$BRIDGE_DIR" && npm install --no-audit --no-fund && npm run build ) || return 1
  [ -f "$BRIDGE_DIR/dist/index.js" ] || { say "ERROR build produced no dist/index.js"; return 1; }
  say "bridge built at $BRIDGE_DIR (commit $(git -C "$BRIDGE_DIR" rev-parse --short HEAD))"
}

install() { install_opencode && install_bridge && ensure_token; }

# Persist everything the detached processes need. The watchdog restarts them
# from a step that cannot see this job's secrets, so "in the environment right
# now" is not good enough.
write_env() {
  local key
  ( umask 077; : > "$ENV_FILE" )
  for key in ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY \
             GROQ_API_KEY OPENCODE_MODEL OPENCODE_AGENT OPENCODE_PERMISSION \
             OPENCODE_SERVE_PORT OPENCODE_MCP_PORT OPENCODE_WORKDIR \
             OPENCODE_BRIDGE_REPO OPENCODE_BRIDGE_REF OPENCODE_VERSION; do
    local value="${!key:-}"
    [ -n "$value" ] || continue
    printf '%s=%q\n' "$key" "$value" >> "$ENV_FILE"
  done
  chmod 600 "$ENV_FILE"
  # opencode's own credential store, for anyone who would rather paste an
  # auth.json than a per-provider key.
  if [ -n "${OPENCODE_AUTH_JSON:-}" ]; then
    mkdir -p "$HOME/.local/share/opencode"
    ( umask 077; printf '%s' "$OPENCODE_AUTH_JSON" > "$HOME/.local/share/opencode/auth.json" )
    say "wrote opencode auth.json"
  fi
  say "wrote $ENV_FILE ($(wc -l < "$ENV_FILE") entries, credentials not shown)"
}

status() {
  ensure_token
  echo "opencode serve : port $SERVE_PORT pid $(server_pid "$SERVE_PID_FILE" "$SERVE_PORT" || echo none)"
  echo "mcp bridge     : port $MCP_PORT pid $(server_pid "$MCP_PID_FILE" "$MCP_PORT" || echo none)"
  echo "binary         : $(resolve_opencode && echo "$OPENCODE_BIN" || echo 'not installed')"
  echo "bridge         : $BRIDGE_DIR ($BRIDGE_REF)"
  echo "workdir        : $WORKDIR"
  echo "token file     : $TOKEN_FILE"
  echo "logs           : $SERVE_LOG $MCP_LOG"
  echo "restarts       : serve=$(num "$(counter restarts-serve)") bridge=$(num "$(counter restarts-bridge)")"
  if serve_health; then echo "serve health   : ok"; else echo "serve health   : FAILING"; fi
  if bridge_health; then echo "bridge health  : ok"; else echo "bridge health  : FAILING"; fi
}

case "${1:-}" in
  write-env) write_env ;;
  install) install ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  health) health && { say 'ok'; exit 0; } || { say 'FAILING'; exit 1; } ;;
  mcp-check) mcp_check 127.0.0.1 && say 'ok' ;;
  watchdog) watchdog ;;
  status) status ;;
  *)
    echo "usage: $0 {write-env|install|start|stop|restart|health|mcp-check|watchdog|status}" >&2
    exit 2
    ;;
esac
