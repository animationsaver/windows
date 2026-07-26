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
import os
import secrets
import shlex
import sqlite3
import string
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

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "animationsaver")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "windows")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "ephemeral-env.yml")
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "main")
TAILNET_DOMAIN = os.environ["TAILNET_DOMAIN"]  # e.g. "tail1234.ts.net"
MAX_ENVS = int(os.environ.get("MAX_ENVS", "6"))
DEFAULT_TTL = int(os.environ.get("DEFAULT_TTL_MINUTES", "350"))
BROKER_TOKEN = os.environ.get("BROKER_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "/data/broker.sqlite3")

# Tailscale SSH: the runner's unprivileged account, port 22 handled by
# tailscaled itself. Authentication is the tailnet identity of this host, so
# there is no key to distribute.
SSH_USER = os.environ.get("SSH_USER", "runner")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
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
        return None
    if not PUBLIC_HOSTS:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    allowed_hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    allowed_origins: list[str] = []
    for host in PUBLIC_HOSTS:
        allowed_hosts += [host, host + ":*"]
        allowed_origins += ["https://" + host, "https://" + host + ":*"]
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
                ready_at   TEXT
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_host_id ON envs(host_id)"
        )


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


# --------------------------------------------------------------------------
# github
# --------------------------------------------------------------------------


async def dispatch_workflow(host_id: str, ttl_minutes: int, profile: str) -> None:
    url = (
        API_ROOT
        + "/repos/"
        + GITHUB_OWNER
        + "/"
        + GITHUB_REPO
        + "/actions/workflows/"
        + WORKFLOW_FILE
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
        },
    }
    headers = {
        "Authorization": "Bearer " + GITHUB_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
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


async def open_conn(host: str) -> asyncssh.SSHClientConnection:
    # client_keys=[] disables public-key auth so asyncssh falls through to the
    # "none" method, which is what Tailscale SSH accepts once its ACL has
    # authorised this node. known_hosts is off because the runner's key is
    # generated fresh on every boot; the tailnet is the trust anchor here.
    return await asyncssh.connect(
        host,
        port=SSH_PORT,
        username=SSH_USER,
        known_hosts=None,
        client_keys=[],
        config=None,
        connect_timeout=SSH_CONNECT_TIMEOUT,
    )


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
            last = exc
            drop_conn(host)
    raise RuntimeError("ssh to " + host + " failed: " + str(last))


async def probe(host: str, timeout: int = 15) -> bool:
    try:
        await ssh_run(host, "true", timeout=timeout)
        return True
    except Exception:
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
    return host_for(row["host_id"])


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

MCP_KWARGS: dict[str, Any] = {
    "name": "gha-env-broker",
    "instructions": (
        "Disposable Linux environments backed by GitHub Actions runners.\n"
        "Call create_env once per conversation, keep the returned env_id, and "
        "pass it to every other call. Environments are wiped when they expire."
        "\n\n"
        "For browser automation, create the environment with "
        "profile='playwright', then call browser_tools(env_id) once to see the "
        "available Playwright tools and their arguments, and browser_call to "
        "invoke them. Everything runs through this one server."
    ),
    "stateless_http": True,
    "streamable_http_path": "/mcp",
}

# Depending on the SDK version, transport security is accepted by the
# constructor, by streamable_http_app(), or not at all.
try:
    mcp = FastMCP(transport_security=SECURITY, **MCP_KWARGS)
except TypeError:
    mcp = FastMCP(**MCP_KWARGS)


@mcp.tool()
async def create_env(
    ttl_minutes: int = DEFAULT_TTL,
    profile: str = "base",
    label: str = "",
) -> dict[str, Any]:
    """Provision a fresh disposable environment and return its secret env_id.

    Returns immediately with state=provisioning; boot takes a few minutes.
    Poll wait_ready or env_status before running commands.

    profile: "base" (shell only) or "playwright" (shell plus a headless
    Chromium driven through browser_call). Choose "playwright" up front: the
    profile cannot be changed afterwards.
    """
    if profile not in ("base", "playwright"):
        raise ValueError("profile must be 'base' or 'playwright'")
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
    with db() as conn:
        conn.execute(
            "INSERT INTO envs (env_id, host_id, profile, state, label, created_at, expires_at)"
            " VALUES (?, ?, ?, 'provisioning', ?, ?, ?)",
            (env_id, host_id, profile, label, iso(created), iso(expires)),
        )
    try:
        await dispatch_workflow(host_id, ttl_minutes, profile)
    except Exception:
        with db() as conn:
            conn.execute("DELETE FROM envs WHERE env_id = ?", (env_id,))
        raise

    return {
        "env_id": env_id,
        "host": host_for(host_id),
        "profile": profile,
        "state": "provisioning",
        "expires_at": iso(expires),
        "note": "Keep env_id secret; it is the only credential for this box. "
        "Boot usually takes 2-5 minutes"
        + (", longer for playwright (Chromium download)." if profile == "playwright" else "."),
    }


@mcp.tool()
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
async def exec(env_id: str, command: str, timeout_seconds: int = 180) -> str:
    """Run a shell command inside the environment identified by env_id.

    Each call gets its own SSH channel, so calls may run in parallel. State
    such as the working directory is NOT carried between calls; chain with
    `cd /path && ...` instead. For long jobs, detach with
    `nohup ... > /tmp/job.log 2>&1 &` and poll the log.
    """
    row = await resolve(env_id)
    return await ssh_run(
        host_for(row["host_id"]),
        command,
        timeout=max(10, min(int(timeout_seconds), 600)),
    )


@mcp.tool()
async def sudo_exec(env_id: str, command: str, timeout_seconds: int = 180) -> str:
    """Run a shell command as root inside the environment identified by env_id."""
    row = await resolve(env_id)
    return await ssh_run(
        host_for(row["host_id"]),
        command,
        timeout=max(10, min(int(timeout_seconds), 600)),
        sudo=True,
    )


@mcp.tool()
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
async def destroy_env(env_id: str) -> dict[str, Any]:
    """Shut the environment down now and free up capacity."""
    row = await resolve(env_id)
    host = host_for(row["host_id"])
    stopped = False
    try:
        # The workflow's keep-alive loop watches for this file.
        await ssh_run(host, "touch /tmp/stop.txt", timeout=30)
        stopped = True
    except Exception:
        pass
    drop_conn(host)
    set_state(env_id, "destroyed")
    return {
        "state": "destroyed",
        "graceful": stopped,
        "live": live_count(),
        "note": "the runner takes up to ~30s to notice and exit",
    }


@mcp.tool()
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
app.add_middleware(BearerAuth)
