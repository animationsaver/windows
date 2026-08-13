#!/usr/bin/env pwsh
# Supervise the Playwright MCP server on the Windows environment.
#
#   pw-mcp.ps1 install    install a pinned @playwright/mcp and its browser
#   pw-mcp.ps1 start      start it unless it is already healthy (idempotent)
#   pw-mcp.ps1 stop       stop it and free the port
#   pw-mcp.ps1 health     probe it the way an MCP client does (exit 0 = healthy)
#   pw-mcp.ps1 watchdog   one supervision tick, for the keep-alive loop
#   pw-mcp.ps1 status     print what this script thinks is going on
#
# The PowerShell half of scripts/pw-mcp.sh, and it exists for the same reasons:
# "is the port open" is not a health check (it stayed green through a server
# that answered every real call with 403, and through a server whose browser
# had died), "@latest" re-resolves the package on every start, and the browser
# has to be the build the installed server actually asks for.

param([Parameter(Position = 0)][string]$Command = "status")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Port = if ($env:PW_MCP_PORT) { [int]$env:PW_MCP_PORT } else { 8931 }
$Version = if ($env:PW_MCP_VERSION) { $env:PW_MCP_VERSION } else { "0.0.79" }
$StateDir = if ($env:PW_MCP_STATE_DIR) { $env:PW_MCP_STATE_DIR } else { Join-Path ([IO.Path]::GetTempPath()) "pw-mcp" }
$Log = if ($env:PW_MCP_LOG) { $env:PW_MCP_LOG } else { Join-Path $StateDir "pw-mcp.log" }
$ErrLog = if ($env:PW_MCP_ERR_LOG) { $env:PW_MCP_ERR_LOG } else { Join-Path $StateDir "pw-mcp.err.log" }
$PidFile = Join-Path $StateDir "pw-mcp.pid"
$Browser = if ($env:PW_MCP_BROWSER) { $env:PW_MCP_BROWSER } else { "chromium" }
$HealthTimeout = if ($env:PW_MCP_HEALTH_TIMEOUT) { [int]$env:PW_MCP_HEALTH_TIMEOUT } else { 15 }
$StartTimeout = if ($env:PW_MCP_START_TIMEOUT) { [int]$env:PW_MCP_START_TIMEOUT } else { 120 }
$FailThreshold = if ($env:PW_MCP_FAIL_THRESHOLD) { [int]$env:PW_MCP_FAIL_THRESHOLD } else { 2 }
$RestartCooldown = if ($env:PW_MCP_RESTART_COOLDOWN) { [int]$env:PW_MCP_RESTART_COOLDOWN } else { 90 }
$MaxRestarts = if ($env:PW_MCP_MAX_RESTARTS) { [int]$env:PW_MCP_MAX_RESTARTS } else { 20 }
# --allowed-hosts '*' is required, not cosmetic: playwright-mcp only accepts
# the host it bound to, and the broker arrives through an SSH forward as
# 127.0.0.1:<ephemeral port>.
$ExtraArgs = if ($env:PW_MCP_EXTRA_ARGS) {
  $env:PW_MCP_EXTRA_ARGS -split '\s+' | Where-Object { $_ }
} else {
  @("--headless", "--isolated", "--allowed-hosts", "*")
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Say([string]$Message) {
  Write-Host "$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) pw-mcp: $Message"
}

function Get-Counter([string]$Name) {
  $path = Join-Path $StateDir $Name
  if (Test-Path $path) {
    $raw = (Get-Content $path -Raw).Trim()
    if ($raw -match '^\d+$') { return [int]$raw }
  }
  return 0
}

function Set-Counter([string]$Name, [int]$Value) {
  Set-Content -Path (Join-Path $StateDir $Name) -Value $Value -NoNewline
}

function Resolve-Server {
  # Never npx: that is a registry lookup on every start, and it can hand back
  # a different version than the one this environment was tested with.
  $root = (& npm root -g 2> $null | Select-Object -First 1)
  if ($root) {
    $cli = Join-Path ($root.Trim()) "@playwright/mcp/cli.js"
    if (Test-Path $cli) { return @{ Exe = "node"; Args = @($cli) } }
  }
  $cmd = Get-Command playwright-mcp -ErrorAction SilentlyContinue
  if ($cmd) { return @{ Exe = $cmd.Source; Args = @() } }
  return $null
}

function Invoke-Probe([string]$Target) {
  # A real MCP handshake. A bare GET is answered with 400 by a perfectly
  # healthy server, so anything simpler than this reports the wrong thing.
  $body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"pw-mcp-healthcheck","version":"1"}}}'
  $uri = "http://" + $Target + ":$Port/mcp"
  try {
    $resp = Invoke-WebRequest -Uri $uri -Method Post -Body $body `
      -ContentType "application/json" `
      -Headers @{ Accept = "application/json, text/event-stream" } `
      -TimeoutSec $HealthTimeout -SkipHttpErrorCheck -MaximumRedirection 0
  } catch {
    return @{ Code = 0; Session = $null; Error = $_.Exception.Message }
  }
  $session = $null
  if ($resp.Headers.ContainsKey("mcp-session-id")) {
    $session = @($resp.Headers["mcp-session-id"])[0]
  }
  if ($session) {
    # Probing should not leak a browser context on every tick.
    try {
      Invoke-WebRequest -Uri $uri -Method Delete -TimeoutSec $HealthTimeout `
        -Headers @{ "mcp-session-id" = $session } -SkipHttpErrorCheck | Out-Null
    } catch { }
  }
  return @{ Code = [int]$resp.StatusCode; Session = $session; Error = $null }
}

function Test-Health {
  $local = Invoke-Probe "localhost"
  if ($local.Code -ne 200) {
    if ($env:PW_MCP_HEALTH_VERBOSE) { Say "probe localhost http=$($local.Code) $($local.Error)" }
    return $false
  }
  # The broker does not arrive as "localhost", it arrives as 127.0.0.1 through
  # an SSH forward. If only that probe fails, the Host check is the problem.
  $loop = Invoke-Probe "127.0.0.1"
  if ($loop.Code -ne 200) {
    Say "WARNING answers on localhost but Host 127.0.0.1:$Port gets $($loop.Code); start it with --allowed-hosts '*' or every browser_call is a 403"
    return $false
  }
  if ($env:PW_MCP_HEALTH_VERBOSE) { Say "healthy on 127.0.0.1:$Port (session $($loop.Session))" }
  return $true
}

function Get-PortPid {
  if ($IsWindows) {
    try {
      $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
      if ($conn) { return [int]$conn.OwningProcess }
    } catch { }
    return $null
  }
  $out = & lsof -tiTCP:$Port -sTCP:LISTEN 2> $null | Select-Object -First 1
  if ($out) { return [int]$out }
  return $null
}

function Get-ServerPid {
  if (Test-Path $PidFile) {
    $raw = (Get-Content $PidFile -Raw).Trim()
    if ($raw -match '^\d+$') {
      $proc = Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue
      if ($proc) { return [int]$raw }
    }
  }
  return Get-PortPid
}

function Stop-Server {
  $serverPid = Get-ServerPid
  if ($serverPid) {
    Say "stopping pid $serverPid"
    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
      if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) { break }
      Start-Sleep -Milliseconds 250
    }
  }
  # A leftover listener means the next start dies on EADDRINUSE, and the
  # replacement of the replacement after that.
  $held = Get-PortPid
  if ($held) {
    Say "port $Port still held by pid $held; killing"
    Stop-Process -Id $held -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
  }
  Remove-Item $PidFile -ErrorAction SilentlyContinue
}

function Wait-Healthy {
  $deadline = (Get-Date).AddSeconds($StartTimeout)
  while ((Get-Date) -lt $deadline) {
    if (Test-Health) { Say "healthy on 127.0.0.1:$Port"; return $true }
    Start-Sleep -Seconds 2
  }
  Say "ERROR not healthy within ${StartTimeout}s"
  if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 40 }
  if (Test-Path $Log) { Get-Content $Log -Tail 40 }
  return $false
}

function Start-Server {
  if (Get-ServerPid) {
    if (Test-Health) { Say "already running and healthy; nothing to do"; return $true }
    Say "something holds the port but does not answer MCP; replacing it"
    Stop-Server
  }
  $server = Resolve-Server
  if (-not $server) {
    Say "ERROR @playwright/mcp is not installed; run '$PSCommandPath install' first"
    return $false
  }
  $argv = @($server.Args) + @("--port", "$Port", "--host", "127.0.0.1") + $ExtraArgs
  Say "starting $($server.Exe) $($argv -join ' ')"
  $opts = @{
    FilePath               = $server.Exe
    ArgumentList           = $argv
    PassThru               = $true
    RedirectStandardOutput = $Log
    RedirectStandardError  = $ErrLog
  }
  if ($IsWindows) { $opts.WindowStyle = "Hidden" }
  $proc = Start-Process @opts
  Set-Content -Path $PidFile -Value $proc.Id -NoNewline
  return Wait-Healthy
}

function Invoke-Watchdog {
  if (Test-Health) {
    $fails = Get-Counter "fails"
    if ($fails -gt 0) { Say "healthy again after $fails failed probe(s)" }
    Set-Counter "fails" 0
    return $true
  }
  $fails = (Get-Counter "fails") + 1
  Set-Counter "fails" $fails
  Say "health probe failed ($fails/$FailThreshold)"
  if ($fails -lt $FailThreshold) { return $false }
  $now = [int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $last = Get-Counter "last-restart"
  $restarts = Get-Counter "restarts"
  # A cold start takes longer than one probe interval. Restarting on top of a
  # server that is still booting is how a restart loop begins.
  if ($last -gt 0 -and ($now - $last) -lt $RestartCooldown) {
    Say "restarted $($now - $last)s ago; inside the ${RestartCooldown}s cooldown"
    return $false
  }
  if ($restarts -ge $MaxRestarts) {
    Say "ERROR giving up after $restarts restarts; see $Log"
    return $false
  }
  Say "restarting (attempt $($restarts + 1)/$MaxRestarts)"
  Set-Counter "restarts" ($restarts + 1)
  Set-Counter "last-restart" $now
  Set-Counter "fails" 0
  Stop-Server
  return (Start-Server)
}

function Install-Server {
  Say "installing @playwright/mcp@$Version"
  & npm install -g "@playwright/mcp@$Version"
  if ($LASTEXITCODE -ne 0) { return $false }
  $root = (& npm root -g | Select-Object -First 1).Trim()
  # The playwright CLI that ships *inside* the server package, so the browser
  # build on disk is the revision this server will look for. A standalone
  # "npx playwright install" resolves an unrelated playwright release.
  $cli = Join-Path $root "@playwright/mcp/node_modules/playwright-core/cli.js"
  if (Test-Path $cli) {
    Say "installing browser '$Browser' with $cli"
    & node $cli install $Browser
  } else {
    Say "WARNING no bundled playwright CLI under $root; falling back to npx"
    & npx playwright install $Browser
  }
  if ($LASTEXITCODE -ne 0) { return $false }
  $server = Resolve-Server
  if ($server) { & $server.Exe @($server.Args + "--version") }
  return $true
}

function Show-Status {
  $serverPid = Get-ServerPid
  Say "version=$Version port=$Port pid=$(if ($serverPid) { $serverPid } else { 'none' })"
  Say "restarts=$(Get-Counter 'restarts') fails=$(Get-Counter 'fails') log=$Log"
  if (Test-Health) { Say "health: ok" } else { Say "health: FAILING" }
}

switch ($Command.ToLowerInvariant()) {
  "install"  { if (Install-Server) { exit 0 } else { exit 1 } }
  "start"    { if (Start-Server) { exit 0 } else { exit 1 } }
  "stop"     { Stop-Server; exit 0 }
  "restart"  { Stop-Server; if (Start-Server) { exit 0 } else { exit 1 } }
  "health"   { if (Test-Health) { exit 0 } else { exit 1 } }
  "watchdog" { if (Invoke-Watchdog) { exit 0 } else { exit 1 } }
  "status"   { Show-Status; exit 0 }
  default    { Write-Error "usage: pw-mcp.ps1 {install|start|stop|restart|health|watchdog|status}"; exit 2 }
}
