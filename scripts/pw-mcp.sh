#!/usr/bin/env bash
# Supervise the Playwright MCP server that backs browser_tools / browser_call.
#
#   pw-mcp.sh install    install a pinned @playwright/mcp and the browser it wants
#   pw-mcp.sh start      start it unless it is already running (idempotent)
#   pw-mcp.sh stop       stop it and free the port
#   pw-mcp.sh health     probe it the way an MCP client does (exit 0 = healthy)
#   pw-mcp.sh watchdog   one supervision tick, for the keep-alive loop
#   pw-mcp.sh status     print what this script thinks is going on
#
# Why this file exists. Each of these was a measured failure, not a theory:
#
#   * "curl -sf http://127.0.0.1:8931/mcp" is not a health check. A perfectly
#     healthy playwright-mcp answers a bare GET with 400 Invalid request,
#     because the Streamable HTTP transport wants an Accept of
#     text/event-stream and a session id. -f turns that into exit 22, so the
#     old keep-alive loop declared the server dead every 30 seconds for the
#     whole life of the job and spawned a replacement that immediately died on
#     EADDRINUSE. Meanwhile the boot loop probed with plain -s, so booting
#     looked fine. "health" speaks real MCP instead: initialize, expect 200,
#     then delete the session again so probing leaks nothing.
#
#   * "npx @playwright/mcp@latest" re-resolves the package from the registry on
#     every start, so a restart can change version mid-session and no start
#     works without the network. We install one pinned version and then run
#     the binary that install produced.
#
#   * "npx playwright install chromium" resolves its own playwright release,
#     which is not the playwright-core @playwright/mcp depends on, so the
#     browser build on disk can be one the server never asks for. We drive the
#     CLI that ships inside the installed @playwright/mcp instead.

set -uo pipefail

PORT="${PW_MCP_PORT:-8931}"
VERSION="${PW_MCP_VERSION:-0.0.79}"
LOG="${PW_MCP_LOG:-/tmp/pw-mcp.log}"
PID_FILE="${PW_MCP_PID_FILE:-/tmp/pw-mcp.pid}"
STATE_DIR="${PW_MCP_STATE_DIR:-/tmp/pw-mcp}"
BROWSER="${PW_MCP_BROWSER:-chromium}"
HEALTH_TIMEOUT="${PW_MCP_HEALTH_TIMEOUT:-15}"
START_TIMEOUT="${PW_MCP_START_TIMEOUT:-120}"
# Consecutive failed probes before the watchdog believes the server is gone.
FAIL_THRESHOLD="${PW_MCP_FAIL_THRESHOLD:-2}"
# Seconds to leave a fresh restart alone: a cold start takes longer than one
# probe interval, and restarting it again mid-boot is how you get a loop.
RESTART_COOLDOWN="${PW_MCP_RESTART_COOLDOWN:-90}"
MAX_RESTARTS="${PW_MCP_MAX_RESTARTS:-20}"
LOG_MAX_BYTES="${PW_MCP_LOG_MAX_BYTES:-10485760}"
# --allowed-hosts '*' is not decoration. playwright-mcp enforces a Host check
# that defaults to the single host it bound to (localhost:PORT), while the
# broker reaches it through an SSH forward and therefore arrives with a Host of
# 127.0.0.1:<ephemeral port>. Without this every browser_call is a 403, which
# is what the macOS environment shipped.
read -r -a EXTRA_ARGS <<< "${PW_MCP_EXTRA_ARGS:---headless --allowed-hosts *}"

# BSD date, i.e. macOS, has no -Is.
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(stamp) pw-mcp: $*"; }

npm_root() { npm root -g 2>/dev/null; }

# Absolute command for the installed server, as an array so no word splitting
# has to be trusted. Never npx: that is a registry lookup on every start.
PW_CMD=()
resolve_cmd() {
  local root cli
  if command -v playwright-mcp > /dev/null 2>&1; then
    PW_CMD=("$(command -v playwright-mcp)")
    return 0
  fi
  root="$(npm_root)/@playwright/mcp"
  for cli in "$root/cli.js" "$root/index.js" "$root/lib/cli.js"; do
    if [ -f "$cli" ]; then
      PW_CMD=("$(command -v node)" "$cli")
      return 0
    fi
  done
  return 1
}

port_pid() {
  local pid=""
  if command -v ss > /dev/null 2>&1; then
    pid="$(ss -lntpH "sport = :$PORT" 2>/dev/null \
      | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
  fi
  if [ -z "$pid" ] && command -v lsof > /dev/null 2>&1; then
    pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)"
  fi
  printf '%s' "$pid"
}

# The pid we started, or whoever holds the port: adopting the incumbent is how
# a restarted supervisor avoids fighting a healthy server for its own port.
server_pid() {
  local pid=""
  if [ -f "$PID_FILE" ]; then
    pid="$(tr -dc '0-9' < "$PID_FILE")"
    if [ -n "$pid" ] && kill -0 "$pid" 2> /dev/null; then
      printf '%s' "$pid"
      return 0
    fi
  fi
  pid="$(port_pid)"
  [ -n "$pid" ] || return 1
  printf '%s' "$pid"
}

counter() { cat "$STATE_DIR/$1" 2> /dev/null | tr -dc '0-9' || true; }
set_counter() { mkdir -p "$STATE_DIR"; printf '%s' "$2" > "$STATE_DIR/$1"; }
num() { local v="$1"; [ -n "$v" ] || v=0; printf '%s' "$v"; }

rotate_log() {
  local size=0
  [ -f "$LOG" ] || return 0
  size=$(wc -c < "$LOG" 2> /dev/null | tr -dc '0-9')
  size=$(num "$size")
  if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
    mv -f "$LOG" "$LOG.1" 2> /dev/null || true
    say "rotated $LOG at $size bytes"
  fi
}

# A real MCP handshake. Anything less cannot tell "listening" from "working",
# and a bare GET cannot even tell "working" from "broken": a healthy server
# answers one with 400 Invalid request. The session this opens is deleted
# again immediately, so probing every 30 seconds for six hours leaks nothing.
PROBE_CODE=""
probe() {
  local host="$1" hdr body code sid rc=1
  hdr="$(mktemp)"
  body="$(mktemp)"
  code="$(curl -s -o "$body" -D "$hdr" -w '%{http_code}' \
    --max-time "$HEALTH_TIMEOUT" \
    -X POST "http://$host:$PORT/mcp" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":"health","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"pw-mcp-health","version":"1.0"}}}' \
    2> /dev/null)"
  sid="$(tr -d '\r' < "$hdr" | awk 'tolower($1) == "mcp-session-id:" { print $2 }' | head -1)"
  if [ "$code" = "200" ] && grep -q '"protocolVersion"' "$body"; then
    rc=0
  fi
  if [ -n "$sid" ]; then
    curl -s -o /dev/null --max-time 5 -X DELETE "http://$host:$PORT/mcp" \
      -H "mcp-session-id: $sid" 2> /dev/null
  fi
  PROBE_CODE="$code"
  [ "${PW_MCP_HEALTH_VERBOSE:-0}" = "1" ] && say "probe $host http=$code session=${sid:-none} rc=$rc"
  rm -f "$hdr" "$body"
  return "$rc"
}

# Two probes, because there are two ways to be unusable. localhost is the one
# host the server allows by default, so it answers "is the process working".
# 127.0.0.1 is how the broker's SSH forward addresses it, so it answers "can a
# caller actually get in" -- a server whose host check is on passes the first
# and fails the second, which is exactly the 403 that made browser_call look
# broken while the process was perfectly healthy.
health() {
  probe localhost || return 1
  probe 127.0.0.1 && return 0
  say "WARNING answers on localhost but Host 127.0.0.1:$PORT gets $PROBE_CODE:"
  say "        the broker tunnels in as 127.0.0.1 and will get the same."
  say "        Start it with --allowed-hosts '*' (see PW_MCP_EXTRA_ARGS)."
  return 1
}

wait_healthy() {
  local deadline=$(( $(date +%s) + START_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if health; then
      say "healthy on 127.0.0.1:$PORT"
      return 0
    fi
    sleep 2
  done
  say "ERROR not healthy within ${START_TIMEOUT}s; last 40 log lines follow"
  tail -n 40 "$LOG" >&2 2> /dev/null || true
  return 1
}

stop() {
  local pid
  pid="$(server_pid || true)"
  if [ -n "$pid" ]; then
    say "stopping pid $pid"
    kill -TERM "-$pid" 2> /dev/null || kill -TERM "$pid" 2> /dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2> /dev/null || break
      sleep 0.5
    done
    kill -0 "$pid" 2> /dev/null && { say "pid $pid ignored SIGTERM; killing"; kill -KILL "$pid" 2> /dev/null; }
  fi
  # Whatever still holds the port would make the next start die on EADDRINUSE.
  pid="$(port_pid)"
  if [ -n "$pid" ]; then
    say "port $PORT still held by pid $pid; killing"
    kill -KILL "$pid" 2> /dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
}

start() {
  local pid
  if pid="$(server_pid)"; then
    if health; then
      say "already running and healthy (pid $pid); nothing to do"
      return 0
    fi
    say "pid $pid is alive but not answering MCP; replacing it"
    stop
  fi
  resolve_cmd || {
    say "ERROR @playwright/mcp is not installed; run '$0 install' first"
    return 1
  }
  rotate_log
  mkdir -p "$STATE_DIR"
  say "starting ${PW_CMD[*]} --port $PORT --host 127.0.0.1 ${EXTRA_ARGS[*]}"
  # The inner shell records its own pid and then execs, so PID_FILE holds the
  # server itself rather than a wrapper that is gone a moment later.
  # setsid puts the server in its own session so finishing a workflow step
  # cannot take it down with the step's process group. macOS has no setsid,
  # where nohup plus the background job is as close as it gets.
  local launcher=()
  command -v setsid > /dev/null 2>&1 && launcher=(setsid)
  "${launcher[@]}" nohup bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$PID_FILE" \
    "${PW_CMD[@]}" --port "$PORT" --host 127.0.0.1 "${EXTRA_ARGS[@]}" \
    < /dev/null >> "$LOG" 2>&1 &
  disown 2> /dev/null || true
  wait_healthy
}

watchdog() {
  mkdir -p "$STATE_DIR"
  local fails now last restarts
  if health; then
    fails=$(num "$(counter fails)")
    [ "$fails" -gt 0 ] && say "healthy again after $fails failed probe(s)"
    set_counter fails 0
    return 0
  fi
  fails=$(( $(num "$(counter fails)") + 1 ))
  set_counter fails "$fails"
  say "health probe failed ($fails/$FAIL_THRESHOLD)"
  [ "$fails" -ge "$FAIL_THRESHOLD" ] || return 0
  now=$(date +%s)
  last=$(num "$(counter last-restart)")
  restarts=$(num "$(counter restarts)")
  if [ "$last" -gt 0 ] && [ $(( now - last )) -lt "$RESTART_COOLDOWN" ]; then
    say "restarted $(( now - last ))s ago; inside the ${RESTART_COOLDOWN}s cooldown"
    return 0
  fi
  if [ "$restarts" -ge "$MAX_RESTARTS" ]; then
    say "ERROR $restarts restarts did not help; leaving it down (see $LOG)"
    return 1
  fi
  say "restarting (attempt $(( restarts + 1 ))/$MAX_RESTARTS)"
  set_counter last-restart "$now"
  set_counter restarts $(( restarts + 1 ))
  set_counter fails 0
  stop
  start
}

install() {
  local root cli deps=0
  # install-deps shells out to apt, so it exists on Ubuntu and nowhere else.
  if [ "${PW_MCP_INSTALL_DEPS:-1}" = "1" ] && [ "$(uname -s)" = "Linux" ]; then
    deps=1
  fi
  say "installing @playwright/mcp@$VERSION"
  npm install -g "@playwright/mcp@$VERSION" || return 1
  root="$(npm_root)/@playwright/mcp"
  # Drive the playwright CLI that ships *inside* the server package, so the
  # browser revision on disk is the one this server will look for. "npx
  # playwright install" resolves an unrelated playwright and can leave a
  # mismatched build behind.
  for cli in "$root/node_modules/.bin/playwright" \
             "$root/node_modules/playwright-core/cli.js"; do
    [ -f "$cli" ] || continue
    say "installing browser '$BROWSER' with $cli"
    if [ "${cli##*.}" = "js" ]; then
      node "$cli" install "$BROWSER" || return 1
      [ "$deps" = 1 ] && sudo -n node "$cli" install-deps "$BROWSER"
    else
      "$cli" install "$BROWSER" || return 1
      [ "$deps" = 1 ] && sudo -n "$cli" install-deps "$BROWSER"
    fi
    resolve_cmd && "${PW_CMD[@]}" --version 2> /dev/null
    return 0
  done
  say "ERROR no playwright CLI inside $root"
  return 1
}

status() {
  local pid
  pid="$(server_pid || true)"
  echo "port         : $PORT"
  echo "pid          : ${pid:-none}"
  echo "version      : $VERSION (pinned)"
  echo "command      : $(resolve_cmd && echo "${PW_CMD[*]}" || echo 'not installed')"
  echo "log          : $LOG"
  echo "restarts     : $(num "$(counter restarts)")"
  echo "failed probes: $(num "$(counter fails)")"
  if health; then echo "health       : ok"; else echo "health       : FAILING"; fi
}

case "${1:-}" in
  install) install ;;
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  health) health && { say 'ok'; exit 0; } || { say 'FAILING'; exit 1; } ;;
  watchdog) watchdog ;;
  status) status ;;
  *)
    echo "usage: $0 {install|start|stop|restart|health|watchdog|status}" >&2
    exit 2
    ;;
esac
