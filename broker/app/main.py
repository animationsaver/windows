"""MCP broker for disposable GitHub Actions environments.

The broker is the only component exposed to the internet, and the only MCP
endpoint a client ever connects to. It:

  1. mints a high-entropy ``env_id`` per conversation/session,
  2. dispatches ``ephemeral-env.yml`` with a random hostname,
  3. runs commands on the environment that owns that ``env_id`` over
     Tailscale SSH,
  4. re-exposes the environment's Playwright MCP server through an SSH port
     forward, so the browser lives behind the same endpoint, the same bearer
     token and the same ``env_id`` as the shell.

Design notes
------------
* ``env_id`` is a capability and never leaves the broker. The workflow only
  receives the hostname, which is meaningless outside the tailnet, so a
  public repository leaking its workflow inputs leaks nothing useful.
* The environment listens on nothing at all on the tailnet. Tailscale SSH is
  terminated inside tailscaled, so the caller is authenticated by its tailnet
  identity and can be restricted with an ACL. An earlier revision proxied MCP
  to servers listening on the tailnet with no authentication whatsoever.
* Playwright is proxied generically (list + call) rather than mirrored tool by
  tool, so a new playwright-mcp release does not require a broker release.
* Capacity is capped by ``MAX_ENVS`` rather than by a fixed slot table.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import secrets
import shlex
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncssh
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

try:  # available since mcp 1.9.x
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # pragma: no cover - older SDKs have no Host validation
    TransportSecuritySettings = None  # type: ignore[assignment]


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# force=True: uvicorn configures the root logger before importing this module,
# so without it basicConfig is a no-op and nothing below ever prints.
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("broker")

# asyncssh is quiet by default and it is the component most likely to be at
# fault (ACLs, auth, DNS). At DEBUG it explains exactly why a connection died.
logging.getLogger("asyncssh").setLevel(
    logging.DEBUG if LOG_LEVEL == "DEBUG" else logging.WARNING
)

# sse_starlette emits a DEBUG line per keepalive per open stream, which at
# DEBUG drowns out everything else without saying anything useful.
logging.getLogger("sse_starlette.sse").setLevel(logging.INFO)


def short(value: Any) -> str:
    """Render a tool argument for the log without leaking a credential."""
    return str(value)[:200]


def traced(fn):
    """Log entry, duration and failures of an MCP tool.

    FastMCP converts an exception into a JSON-RPC error for the client and
    swallows it otherwise, so without this a broken tool leaves no trace in
    `docker compose logs` at all.

    functools.wraps keeps __wrapped__ and __annotations__ intact, which is what
    FastMCP introspects to build the tool schema.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        shown = {
            k: (short(v)[:6] + "..." if k == "env_id" else short(v))
            for k, v in kwargs.items()
        }
        log.info("call %s %s", fn.__name__, shown)
        started = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            log.exception(
                "%s failed after %.1fs: %s",
                fn.__name__,
                time.monotonic() - started,
                exc,
            )
            raise
        log.info("done %s in %.1fs", fn.__name__, time.monotonic() - started)
        return result

    return wrapper


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "animationsaver")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "windows")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "ephemeral-env.yml")
# macOS twin of the workflow above. Same contract, different runner and a
# different SSH story (see SSH_PORT_MACOS).
WORKFLOW_FILE_MACOS = os.environ.get(
    "WORKFLOW_FILE_MACOS", "ephemeral-env-macos.yml"
)
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "main")
TAILNET_DOMAIN = os.environ["TAILNET_DOMAIN"]  # e.g. "tail1234.ts.net"
MAX_ENVS = int(os.environ.get("MAX_ENVS", "6"))
DEFAULT_TTL = int(os.environ.get("DEFAULT_TTL_MINUTES", "350"))
BROKER_TOKEN = os.environ.get("BROKER_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "/data/broker.sqlite3")

# Transport shape. Defaults chosen for the simplest possible client:
#   JSON_RESPONSE=1  POST /mcp answers with one application/json body rather
#                    than an SSE-framed stream. We have no progress
#                    notifications and no server-initiated requests, so the
#                    stream carried exactly one message anyway.
#   STATELESS=1      no Mcp-Session-Id; each request stands alone.
#   GET_STREAM=0     GET /mcp is refused with 405 rather than answered with a
#                    server->client stream. In stateless mode the SDK opens
#                    that stream and tears it down again a moment later, and a
#                    client that expects it to stay open cannot distinguish
#                    that from the connection dropping: it abandons the
#                    handshake before ever sending tools/list. The spec lets a
#                    server that never pushes answer 405, which clients handle
#                    by simply not asking again.
# These are configurable rather than hardcoded because which combination a
# given client tolerates is only discoverable empirically.
JSON_RESPONSE = env_flag("BROKER_JSON_RESPONSE", True)
STATELESS = env_flag("BROKER_STATELESS", True)
GET_STREAM = env_flag("BROKER_GET_STREAM", False)

# Tailscale SSH: the runner's unprivileged account, port 22 handled by
# tailscaled itself. Authentication is the tailnet identity of this host, so
# there is no key to distribute.
SSH_USER = os.environ.get("SSH_USER", "runner")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))

# macOS: the Tailscale SSH *server* is Linux-only (the macOS CLI build never
# starts it), so those environments run their own sshd on loopback and publish
# it with `tailscale serve --tcp`. Authentication is a throwaway keypair minted
# per environment instead of the tailnet identity, and the port is not 22.
SSH_PORT_MACOS = int(os.environ.get("SSH_PORT_MACOS", "2222"))
SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_CONNECT_TIMEOUT", "20"))

# Loopback port Playwright MCP binds on the runner. Never published; reached
# only through the SSH forward set up below.
PW_MCP_PORT = int(os.environ.get("PW_MCP_PORT", "8931"))

# Hostnames clients may use to reach this broker, comma separated, without a
# scheme (e.g. "broker.tail1234.ts.net"). See build_transport_security().
PUBLIC_HOSTS = [
    h.strip()
    for h in os.environ.get("BROKER_PUBLIC_HOSTS", "").split(",")
    if h.strip()
]

API_ROOT = "https://api.github.com"
ACTIVE_STATES = ("provisioning", "ready")
HOST_ALPHABET = string.ascii_lowercase + string.digits
PLATFORMS = ("linux", "macos")
# Which profiles each platform knows how to build. "xcode" only means anything
# on a macOS runner.
PROFILES = {
    "linux": ("base", "playwright"),
    "macos": ("base", "playwright", "xcode"),
}


class ExecTimeout(RuntimeError):
    """A command exceeded its timeout.

    Deliberately not an OSError: builtin TimeoutError is one, and the retry
    path below must not treat a slow command as a broken connection.
    """


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def build_transport_security():
    """Configure the SDK's DNS-rebinding protection.

    FastMCP defaults to host 127.0.0.1, and in that case ``streamable_http_app``
    switches Host/Origin validation on with only local addresses allowed. Every
    request forwarded by ``tailscale funnel`` carries the public ts.net Host
    header, so without this the middleware answers 421 Misdirected Request
    before any tool runs.

    With no BROKER_PUBLIC_HOSTS set we turn the check off instead of guessing:
    the bearer token is what actually guards this server, and DNS rebinding is
    a browser attack that does not apply to a server-side MCP client.
    """
    if TransportSecuritySettings is None:
        log.warning("mcp SDK has no transport_security module; Host check off")
        return None
    if not PUBLIC_HOSTS:
        log.warning(
            "BROKER_PUBLIC_HOSTS is empty: Host validation disabled, only the "
            "bearer token guards this server"
        )
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    allowed_hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    allowed_origins: list[str] = []
    for host in PUBLIC_HOSTS:
        allowed_hosts += [host, host + ":*"]
        allowed_origins += ["https://" + host, "https://" + host + ":*"]
    log.info("allowed Host headers: %s", allowed_hosts)
    return TransportSecuritySettings(
        allowed_hosts=allowed_hosts, allowed_origins=allowed_origins
    )


SECURITY = build_transport_security()


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS envs (
                env_id     TEXT PRIMARY KEY,
                host_id    TEXT NOT NULL,
                profile    TEXT NOT NULL,
                state      TEXT NOT NULL,
                label      TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ready_at   TEXT,
                platform   TEXT NOT NULL DEFAULT 'linux',
                ssh_port   INTEGER,
                ssh_key    TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_host_id ON envs(host_id)"
        )
        # Databases created before macOS support predate the last three
        # columns; add them in place rather than asking for a wipe.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(envs)")}
        for column, decl in (
            ("platform", "TEXT NOT NULL DEFAULT 'linux'"),
            ("ssh_port", "INTEGER"),
            ("ssh_key", "TEXT"),
        ):
            if column not in have:
                conn.execute("ALTER TABLE envs ADD COLUMN " + column + " " + decl)


def reap() -> None:
    """Expire environments whose TTL has passed."""
    with db() as conn:
        conn.execute(
            "UPDATE envs SET state='expired' WHERE state IN (?, ?) AND expires_at < ?",
            (*ACTIVE_STATES, iso(now())),
        )


def live_count() -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM envs WHERE state IN (?, ?)", ACTIVE_STATES
        ).fetchone()[0]


def new_host_id() -> str:
    return "".join(secrets.choice(HOST_ALPHABET) for _ in range(12))


def new_keypair() -> tuple[str, str]:
    """Mint a throwaway ed25519 keypair for one macOS environment.

    The private half stays in the broker database next to the env_id; only the
    public half is passed to the workflow, which is why it is safe that
    workflow inputs are readable by anyone who can read the repository.
    """
    key = asyncssh.generate_private_key("ssh-ed25519")
    return (
        key.export_private_key("openssh").decode(),
        key.export_public_key("openssh").decode().strip(),
    )


def get_env(env_id: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM envs WHERE env_id = ?", (env_id,)
        ).fetchone()


def set_state(env_id: str, state: str, ready: bool = False) -> None:
    with db() as conn:
        if ready:
            conn.execute(
                "UPDATE envs SET state=?, ready_at=COALESCE(ready_at, ?) WHERE env_id=?",
                (state, iso(now()), env_id),
            )
        else:
            conn.execute("UPDATE envs SET state=? WHERE env_id=?", (state, env_id))


def host_for(host_id: str) -> str:
    return "gha-env-" + host_id + "." + TAILNET_DOMAIN


def target_for(row: sqlite3.Row) -> str:
    """Return a row's hostname and record how to authenticate to that host.

    The SSH layer is keyed by hostname alone, so registering the per-platform
    port and key here keeps every call site a one-liner.
    """
    host = host_for(row["host_id"])
    if (row["platform"] or "linux") == "macos":
        _ssh_auth[host] = (int(row["ssh_port"] or SSH_PORT_MACOS), row["ssh_key"])
    else:
        _ssh_auth[host] = (SSH_PORT, None)
    return host


# --------------------------------------------------------------------------
# github
# --------------------------------------------------------------------------


async def dispatch_workflow(
    host_id: str,
    ttl_minutes: int,
    profile: str,
    workflow_file: str = WORKFLOW_FILE,
    extra_inputs: dict[str, str] | None = None,
) -> None:
    url = (
        API_ROOT
        + "/repos/"
        + GITHUB_OWNER
        + "/"
        + GITHUB_REPO
        + "/actions/workflows/"
        + workflow_file
        + "/dispatches"
    )
    payload = {
        "ref": WORKFLOW_REF,
        # Nothing secret here on purpose: workflow inputs are readable by
        # anyone who can read the repository. The hostname is only routable
        # inside the tailnet, and env_id never appears here.
        "inputs": {
            "env_host": host_id,
            "ttl_minutes": str(ttl_minutes),
            "profile": profile,
            **(extra_inputs or {}),
        },
    }
    headers = {
        "Authorization": "Bearer " + GITHUB_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    log.info("dispatching %s for gha-env-%s (%s)", workflow_file, host_id, profile)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code not in (202, 204):
        raise RuntimeError(
            "workflow_dispatch failed: "
            + str(resp.status_code)
            + " "
            + resp.text[:300]
        )


# --------------------------------------------------------------------------
# SSH transport
# --------------------------------------------------------------------------

# One connection per environment, reused across calls. Every exec opens its
# own channel on it, so calls still run in parallel; this only avoids paying
# the handshake on each command. The Playwright tunnel rides the same
# connection, keyed by it so a reconnect invalidates the stale listener.
_conns: dict[str, asyncssh.SSHClientConnection] = {}
_tunnels: dict[str, tuple[asyncssh.SSHClientConnection, Any, int]] = {}
_conn_lock = asyncio.Lock()
_tunnel_lock = asyncio.Lock()
# host -> (port, private key PEM or None). Filled in by target_for(); the
# default covers Linux (Tailscale SSH on 22, "none" auth).
_ssh_auth: dict[str, tuple[int, str | None]] = {}


async def open_conn(host: str) -> asyncssh.SSHClientConnection:
    # client_keys=[] disables public-key auth so asyncssh falls through to the
    # "none" method, which is what Tailscale SSH accepts once its ACL has
    # authorised this node. known_hosts is off because the runner's key is
    # generated fresh on every boot; the tailnet is the trust anchor here.
    port, key_pem = _ssh_auth.get(host, (SSH_PORT, None))
    # macOS environments run a real sshd, so present the key minted for them;
    # Linux ones keep client_keys empty and fall through to "none".
    client_keys = [asyncssh.import_private_key(key_pem)] if key_pem else []
    log.debug(
        "ssh connect %s@%s:%s key=%s", SSH_USER, host, port, bool(key_pem)
    )
    conn = await asyncssh.connect(
        host,
        port=port,
        username=SSH_USER,
        known_hosts=None,
        client_keys=client_keys,
        config=None,
        connect_timeout=SSH_CONNECT_TIMEOUT,
    )
    log.info("ssh connected to %s", host)
    return conn


async def get_conn(host: str) -> asyncssh.SSHClientConnection:
    async with _conn_lock:
        conn = _conns.get(host)
        if conn is not None and not conn.is_closed():
            return conn
        conn = await open_conn(host)
        _conns[host] = conn
        return conn


def drop_conn(host: str) -> None:
    entry = _tunnels.pop(host, None)
    if entry is not None:
        try:
            entry[1].close()
        except Exception:
            pass
    conn = _conns.pop(host, None)
    if conn is not None:
        log.debug("dropping ssh connection to %s", host)
        try:
            conn.abort()
        except Exception:
            pass


def wrap(command: str, sudo: bool) -> str:
    inner = "bash -lc " + shlex.quote(command)
    return "sudo -n " + inner if sudo else inner


async def ssh_run(
    host: str, command: str, timeout: int = 180, sudo: bool = False
) -> str:
    """Run one command in its own SSH channel and return its combined output."""
    line = wrap(command, sudo)
    last: Exception | None = None
    # A pooled connection may have died with the runner in between calls;
    # one silent reconnect is worth more than surfacing that to the caller.
    for attempt in (1, 2):
        try:
            conn = await get_conn(host)
            async with conn.create_process(line) as proc:
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    proc.terminate()
                    raise ExecTimeout(
                        "command timed out after "
                        + str(timeout)
                        + "s and was terminated; for long jobs detach with "
                        "`nohup ... > /tmp/job.log 2>&1 &` and poll the log"
                    )
                status = proc.exit_status
            out = (stdout or "") + (stderr or "")
            if status:
                out = out + "\n[exit status " + str(status) + "]"
            return out.strip()
        except (OSError, asyncssh.Error) as exc:
            log.warning(
                "ssh attempt %s/2 to %s failed: %s: %s",
                attempt,
                host,
                type(exc).__name__,
                exc,
            )
            last = exc
            drop_conn(host)
    raise RuntimeError(
        "ssh to " + host + " failed: " + type(last).__name__ + ": " + str(last)
    )


async def probe(host: str, timeout: int = 15) -> bool:
    try:
        await ssh_run(host, "true", timeout=timeout)
        return True
    except Exception as exc:
        log.info("probe of %s not ready yet: %s", host, exc)
        drop_conn(host)
        return False


async def resolve(env_id: str) -> sqlite3.Row:
    reap()
    row = get_env(env_id)
    if row is None:
        # Slow down brute force a little and never reveal whether the id shape
        # was right.
        await asyncio.sleep(0.5)
        raise ValueError("unknown or expired env_id")
    if row["state"] not in ACTIVE_STATES:
        raise ValueError("environment is " + row["state"] + "; create a new one")
    return row


# --------------------------------------------------------------------------
# Playwright MCP, tunnelled over the same SSH connection
# --------------------------------------------------------------------------


async def pw_endpoint(host: str) -> str:
    """Return a local URL that forwards to Playwright MCP on the runner.

    asyncssh binds an ephemeral loopback port here and tunnels it to
    127.0.0.1:PW_MCP_PORT on the environment, so nothing has to listen on the
    tailnet and no serve rule is needed.
    """
    conn = await get_conn(host)
    async with _tunnel_lock:
        entry = _tunnels.get(host)
        if entry is not None and entry[0] is conn:
            return "http://127.0.0.1:" + str(entry[2]) + "/mcp"
        listener = await conn.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", PW_MCP_PORT
        )
        port = listener.get_port()
        log.info(
            "tunnel 127.0.0.1:%s -> %s:127.0.0.1:%s", port, host, PW_MCP_PORT
        )
        _tunnels[host] = (conn, listener, port)
        return "http://127.0.0.1:" + str(port) + "/mcp"


async def pw_session(host: str, timeout: int):
    """Async context manager yielding an initialised upstream ClientSession."""
    url = await pw_endpoint(host)
    return streamablehttp_client(
        url,
        timeout=timedelta(seconds=timeout),
        sse_read_timeout=timedelta(seconds=timeout + 60),
    )


async def pw_request(host: str, action, timeout: int):
    """Run `action(session)` against Playwright MCP, reconnecting once."""
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            client = await pw_session(host, timeout)
            async with client as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await action(session)
        except (OSError, asyncssh.Error) as exc:
            log.warning(
                "playwright attempt %s/2 on %s failed: %s: %s",
                attempt,
                host,
                type(exc).__name__,
                exc,
            )
            last = exc
            drop_conn(host)
    raise RuntimeError(
        "could not reach Playwright MCP on "
        + host
        + ": "
        + str(last)
        + " (is the environment on profile=playwright, and finished booting?)"
    )


async def require_playwright(env_id: str) -> str:
    row = await resolve(env_id)
    if row["profile"] != "playwright":
        raise ValueError(
            "this environment was created with profile='base'; create one with "
            "profile='playwright' to use the browser"
        )
    return target_for(row)


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

MCP_KWARGS: dict[str, Any] = {
    "name": "gha-env-broker",
    "instructions": (
        "Disposable Linux and macOS environments backed by GitHub Actions "
        "runners.\n"
        "Call create_env once per conversation, keep the returned env_id, and "
        "pass it to every other call. Environments are wiped when they expire."
        "\n\n"
        "For Xcode, Swift or anything else that needs Apple tooling, create the "
        "environment with platform='macos' (and profile='xcode' to get Xcode "
        "selected plus XcodeGen). exec, sudo_exec and destroy_env behave "
        "identically on both platforms.\n\n"
        "For browser automation, create the environment with "
        "profile='playwright', then call browser_tools(env_id) once to see the "
        "available Playwright tools and their arguments, and browser_call to "
        "invoke them. Everything runs through this one server."
    ),
    "stateless_http": STATELESS,
    "json_response": JSON_RESPONSE,
    "streamable_http_path": "/mcp",
}

# Depending on the SDK version, transport security is accepted by the
# constructor, by streamable_http_app(), or not at all.
try:
    mcp = FastMCP(transport_security=SECURITY, **MCP_KWARGS)
except TypeError:
    mcp = FastMCP(**MCP_KWARGS)


@mcp.tool()
@traced
async def create_env(
    ttl_minutes: int = DEFAULT_TTL,
    profile: str = "base",
    label: str = "",
    platform: str = "linux",
) -> dict[str, Any]:
    """Provision a fresh disposable environment and return its secret env_id.

    Returns immediately with state=provisioning; boot takes a few minutes.
    Poll wait_ready or env_status before running commands.

    platform: "linux" (Ubuntu runner, the default) or "macos" (macOS runner,
    for Xcode, Swift and anything else that needs Apple tooling). macOS boots
    slower and burns Actions minutes at ~10x the Linux rate, so only ask for it
    when you actually need it.

    profile: "base" (shell only), "playwright" (shell plus a headless Chromium
    driven through browser_call) or, on macOS only, "xcode" (Xcode selected and
    XcodeGen installed). Choose it up front: neither platform nor profile can
    be changed afterwards.
    """
    if platform not in PLATFORMS:
        raise ValueError("platform must be 'linux' or 'macos'")
    if profile not in PROFILES[platform]:
        raise ValueError(
            "profile for platform '"
            + platform
            + "' must be one of "
            + ", ".join(PROFILES[platform])
        )
    ttl_minutes = max(1, min(int(ttl_minutes), 350))

    reap()
    if live_count() >= MAX_ENVS:
        raise RuntimeError(
            "already running "
            + str(MAX_ENVS)
            + " environments; destroy_env one first or raise MAX_ENVS"
        )

    env_id = secrets.token_urlsafe(32)
    host_id = new_host_id()
    created = now()
    expires = created + timedelta(minutes=ttl_minutes)
    # macOS has no Tailscale SSH server, so that environment runs its own sshd
    # and has to be told which key to trust. Only the public half is handed to
    # the workflow.
    key_pem: str | None = None
    pubkey: str | None = None
    ssh_port = SSH_PORT
    if platform == "macos":
        key_pem, pubkey = new_keypair()
        ssh_port = SSH_PORT_MACOS
    with db() as conn:
        conn.execute(
            "INSERT INTO envs (env_id, host_id, profile, state, label,"
            " created_at, expires_at, platform, ssh_port, ssh_key)"
            " VALUES (?, ?, ?, 'provisioning', ?, ?, ?, ?, ?, ?)",
            (
                env_id,
                host_id,
                profile,
                label,
                iso(created),
                iso(expires),
                platform,
                ssh_port,
                key_pem,
            ),
        )
    try:
        if platform == "macos":
            await dispatch_workflow(
                host_id,
                ttl_minutes,
                profile,
                workflow_file=WORKFLOW_FILE_MACOS,
                extra_inputs={"ssh_pubkey": pubkey or ""},
            )
        else:
            await dispatch_workflow(host_id, ttl_minutes, profile)
    except Exception:
        with db() as conn:
            conn.execute("DELETE FROM envs WHERE env_id = ?", (env_id,))
        raise

    return {
        "env_id": env_id,
        "host": host_for(host_id),
        "platform": platform,
        "profile": profile,
        "state": "provisioning",
        "expires_at": iso(expires),
        "note": "Keep env_id secret; it is the only credential for this box. "
        + (
            "Boot usually takes 4-8 minutes on macOS"
            if platform == "macos"
            else "Boot usually takes 2-5 minutes"
        )
        + (
            ", longer for playwright (Chromium download)."
            if profile == "playwright"
            else "."
        ),
    }


@mcp.tool()
@traced
async def env_status(env_id: str) -> dict[str, Any]:
    """Report whether the environment is still provisioning or ready to use."""
    row = await resolve(env_id)
    host = host_for(row["host_id"])
    if row["state"] == "ready":
        return {"state": "ready", "host": host, "expires_at": row["expires_at"]}
    if await probe(host):
        set_state(env_id, "ready", ready=True)
        return {"state": "ready", "host": host, "expires_at": row["expires_at"]}
    return {"state": "provisioning", "host": host, "expires_at": row["expires_at"]}


@mcp.tool()
@traced
async def wait_ready(env_id: str, max_wait_seconds: int = 120) -> dict[str, Any]:
    """Block until the environment answers, or until max_wait_seconds passes.

    Readiness means the shell answers. On profile='playwright' the browser may
    need another minute or two after that; browser_call will say so.
    """
    max_wait_seconds = max(5, min(int(max_wait_seconds), 240))
    deadline = now() + timedelta(seconds=max_wait_seconds)
    while now() < deadline:
        status = await env_status(env_id)
        if status["state"] == "ready":
            return status
        await asyncio.sleep(10)
    return {"state": "provisioning", "hint": "call wait_ready again"}


@mcp.tool()
@traced
async def exec(env_id: str, command: str, timeout_seconds: int = 180) -> str:
    """Run a shell command inside the environment identified by env_id.

    Each call gets its own SSH channel, so calls may run in parallel. State
    such as the working directory is NOT carried between calls; chain with
    `cd /path && ...` instead. For long jobs, detach with
    `nohup ... > /tmp/job.log 2>&1 &` and poll the log.
    """
    row = await resolve(env_id)
    return await ssh_run(
        target_for(row),
        command,
        timeout=max(10, min(int(timeout_seconds), 600)),
    )


@mcp.tool()
@traced
async def sudo_exec(env_id: str, command: str, timeout_seconds: int = 180) -> str:
    """Run a shell command as root inside the environment identified by env_id."""
    row = await resolve(env_id)
    return await ssh_run(
        target_for(row),
        command,
        timeout=max(10, min(int(timeout_seconds), 600)),
        sudo=True,
    )


@mcp.tool()
@traced
async def browser_tools(env_id: str) -> list[dict[str, Any]]:
    """List the Playwright tools available in this environment.

    Call this once before using browser_call. The set comes from the
    playwright-mcp release installed on the runner, so it is authoritative for
    that environment rather than baked into this broker.
    """
    host = await require_playwright(env_id)

    async def action(session: ClientSession):
        listed = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema,
            }
            for t in listed.tools
        ]

    return await pw_request(host, action, timeout=60)


@mcp.tool()
@traced
async def browser_call(
    env_id: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    timeout_seconds: int = 120,
):
    """Invoke a Playwright tool in this environment's headless Chromium.

    `tool` and `arguments` come from browser_tools, e.g.
    tool="browser_navigate", arguments={"url": "https://example.com"}.
    Browser state (pages, cookies, session) persists between calls for the
    lifetime of the environment. Screenshots are returned as images.
    """
    host = await require_playwright(env_id)
    timeout = max(10, min(int(timeout_seconds), 600))
    args = arguments or {}

    async def action(session: ClientSession):
        result = await session.call_tool(tool, args)
        # Content blocks are handed back untouched so image results survive
        # the extra hop instead of being flattened to text.
        return list(result.content)

    return await pw_request(host, action, timeout=timeout)


@mcp.tool()
@traced
async def destroy_env(env_id: str) -> dict[str, Any]:
    """Shut the environment down now and free up capacity."""
    row = await resolve(env_id)
    host = host_for(row["host_id"])
    stopped = False
    try:
        # The workflow's keep-alive loop watches for this file.
        await ssh_run(host, "touch /tmp/stop.txt", timeout=30)
        stopped = True
    except Exception as exc:
        log.warning("graceful stop of %s failed: %s", host, exc)
    drop_conn(host)
    set_state(env_id, "destroyed")
    return {
        "state": "destroyed",
        "graceful": stopped,
        "live": live_count(),
        "note": "the runner takes up to ~30s to notice and exit",
    }


@mcp.tool()
@traced
async def list_envs() -> list[dict[str, Any]]:
    """List live environments (env_ids are masked; only the owner has them)."""
    reap()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM envs WHERE state IN (?, ?) ORDER BY created_at",
            ACTIVE_STATES,
        ).fetchall()
    return [
        {
            "host": host_for(r["host_id"]),
            "state": r["state"],
            "platform": r["platform"],
            "profile": r["profile"],
            "label": r["label"],
            "env_id_prefix": r["env_id"][:6] + "...",
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------
# HTTP app
# --------------------------------------------------------------------------

MAX_PEEK = 4096


class RequestLog(BaseHTTPMiddleware):
    """One line per request, including what the client asked for.

    Without the JSON-RPC method and the MCP headers there is no way to tell a
    client that is failing from one that is merely reconnecting, since both
    look like a stream of 200s.
    """

    async def dispatch(self, request: Request, call_next):
        method = "-"
        if request.method == "POST" and request.url.path.startswith("/mcp"):
            body = await request.body()
            # request.body() drains the receive channel, so hand the app a
            # replacement that replays what we just read.
            request._receive = self._replay(body)  # type: ignore[attr-defined]
            if len(body) <= MAX_PEEK:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        method = str(parsed.get("method", "-"))
                except Exception:
                    method = "?"

        started = time.monotonic()
        response = await call_next(request)
        headers = request.headers
        log.info(
            "%s %s %s -> %s (%.2fs) accept=%r proto=%s session=%s ua=%r",
            request.method,
            request.url.path,
            method,
            response.status_code,
            time.monotonic() - started,
            headers.get("accept", "-"),
            headers.get("mcp-protocol-version", "-"),
            headers.get("mcp-session-id", "-"),
            headers.get("user-agent", "-")[:60],
        )
        return response

    @staticmethod
    def _replay(body: bytes):
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive


class NoStandaloneStream(BaseHTTPMiddleware):
    """Refuse GET /mcp unless the server->client stream is wanted.

    This broker only ever answers requests, so the standalone stream carries
    nothing. Worse, in stateless mode the SDK closes it again immediately,
    which a client cannot tell apart from the connection failing underneath
    it. 405 is what the streamable-http spec prescribes for a server that does
    not offer the stream at all, and clients treat it as a fact about the
    server rather than as an error.
    """

    async def dispatch(self, request: Request, call_next):
        if (
            not GET_STREAM
            and request.method == "GET"
            and request.url.path.startswith("/mcp")
        ):
            return JSONResponse(
                {"error": "this server does not offer a server-to-client stream"},
                status_code=405,
                headers={"Allow": "POST"},
            )
        return await call_next(request)


class BearerAuth(BaseHTTPMiddleware):
    """Single shared token in front of the MCP endpoint.

    This guards *reaching* the broker. Access to an individual environment is
    additionally gated by its env_id.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp") and BROKER_TOKEN:
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not secrets.compare_digest(token, BROKER_TOKEN):
                log.warning(
                    "rejected unauthorised request from %s",
                    request.client.host if request.client else "?",
                )
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def healthz(_: Request) -> PlainTextResponse:
    reap()
    return PlainTextResponse("ok live=" + str(live_count()) + "/" + str(MAX_ENVS) + "\n")


init_db()

try:
    app = mcp.streamable_http_app(transport_security=SECURITY)
except TypeError:
    app = mcp.streamable_http_app()

app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
# Last added is outermost, so requests pass RequestLog -> NoStandaloneStream
# -> BearerAuth -> the MCP app.
app.add_middleware(BearerAuth)
app.add_middleware(NoStandaloneStream)
app.add_middleware(RequestLog)

log.info(
    "broker ready: repo=%s/%s tailnet=%s max_envs=%s log=%s json_response=%s "
    "stateless=%s get_stream=%s",
    GITHUB_OWNER,
    GITHUB_REPO,
    TAILNET_DOMAIN,
    MAX_ENVS,
    LOG_LEVEL,
    JSON_RESPONSE,
    STATELESS,
    GET_STREAM,
)
