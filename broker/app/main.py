"""MCP broker for disposable GitHub Actions environments.

The broker is the only component exposed to the internet. It:

  1. mints a high-entropy ``env_id`` per conversation/session,
  2. dispatches ``ephemeral-env.yml`` on a free *slot*,
  3. proxies ``exec`` / ``sudo_exec`` to the environment that owns that
     ``env_id`` over the tailnet.

Design notes
------------
* ``env_id`` is a capability. It never leaves the broker: the workflow only
  ever receives the (non-secret) slot number, so a public repository leaking
  its workflow inputs leaks nothing useful.
* Environments are published with ``tailscale serve``, i.e. tailnet-only.
  Even somebody who guesses a slot cannot reach the box from outside.
* Hostnames are per-slot rather than per-session so the Let's Encrypt
  certificate cache in the workflow stays effective.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "animationsaver")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "windows")
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "ephemeral-env.yml")
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "main")
TAILNET_DOMAIN = os.environ["TAILNET_DOMAIN"]  # e.g. "tail1234.ts.net"
SLOT_COUNT = int(os.environ.get("SLOT_COUNT", "6"))
DEFAULT_TTL = int(os.environ.get("DEFAULT_TTL_MINUTES", "350"))
BROKER_TOKEN = os.environ.get("BROKER_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "/data/broker.sqlite3")

ACTIVE_STATES = ("provisioning", "ready")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


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
                slot       TEXT NOT NULL,
                profile    TEXT NOT NULL,
                state      TEXT NOT NULL,
                label      TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ready_at   TEXT
            )
            """
        )
        # At most one live environment per slot.
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_live_slot
            ON envs(slot) WHERE state IN ('provisioning', 'ready')
            """
        )


def reap() -> None:
    """Expire environments whose TTL has passed."""
    with db() as conn:
        conn.execute(
            "UPDATE envs SET state='expired' WHERE state IN (?, ?) AND expires_at < ?",
            (*ACTIVE_STATES, iso(now())),
        )


def free_slot() -> str | None:
    with db() as conn:
        taken = {
            r["slot"]
            for r in conn.execute(
                "SELECT slot FROM envs WHERE state IN (?, ?)", ACTIVE_STATES
            )
        }
    for i in range(1, SLOT_COUNT + 1):
        slot = f"{i:02d}"
        if slot not in taken:
            return slot
    return None


def get_env(env_id: str) -> sqlite3.Row | None:
    with db() as conn:
        cur = conn.execute("SELECT * FROM envs WHERE env_id = ?", (env_id,))
        return cur.fetchone()


def set_state(env_id: str, state: str, ready: bool = False) -> None:
    with db() as conn:
        if ready:
            conn.execute(
                "UPDATE envs SET state=?, ready_at=COALESCE(ready_at, ?) WHERE env_id=?",
                (state, iso(now()), env_id),
            )
        else:
            conn.execute("UPDATE envs SET state=? WHERE env_id=?", (state, env_id))


def host_for(slot: str) -> str:
    return f"gha-env-{slot}.{TAILNET_DOMAIN}"


# --------------------------------------------------------------------------
# github + upstream MCP
# --------------------------------------------------------------------------


async def dispatch_workflow(slot: str, ttl_minutes: int, profile: str) -> None:
    url = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/actions/workflows/{WORKFLOW_FILE}/dispatches"
    )
    payload = {
        "ref": WORKFLOW_REF,
        # Nothing secret here on purpose: workflow inputs are readable by
        # anyone who can read the repository.
        "inputs": {
            "slot": slot,
            "ttl_minutes": str(ttl_minutes),
            "profile": profile,
        },
    }
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code not in (202, 204):
        raise RuntimeError(
            f"workflow_dispatch failed: {resp.status_code} {resp.text[:300]}"
        )


async def upstream_call(
    host: str, tool: str, arguments: dict[str, Any], timeout: int = 180
) -> str:
    """Call a tool on the environment's ssh-mcp (via mcp-proxy)."""
    url = f"https://{host}/mcp"
    async with streamablehttp_client(
        url,
        timeout=timedelta(seconds=timeout),
        sse_read_timeout=timedelta(seconds=timeout + 60),
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            chunks = [
                getattr(part, "text", "")
                for part in result.content
                if getattr(part, "type", "") == "text"
            ]
            return "\n".join(c for c in chunks if c)


async def probe(host: str, timeout: int = 15) -> bool:
    try:
        url = f"https://{host}/mcp"
        async with streamablehttp_client(
            url, timeout=timedelta(seconds=timeout)
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
        return True
    except Exception:
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
        raise ValueError(f"environment is {row['state']}; create a new one")
    return row


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------

mcp = FastMCP(
    name="gha-env-broker",
    instructions=(
        "Disposable Linux environments backed by GitHub Actions runners.\n"
        "Call create_env once per conversation, keep the returned env_id, and "
        "pass it to every exec call. Environments are wiped when they expire."
    ),
    stateless_http=True,
    streamable_http_path="/mcp",
)


@mcp.tool()
async def create_env(
    ttl_minutes: int = DEFAULT_TTL,
    profile: str = "base",
    label: str = "",
) -> dict[str, Any]:
    """Provision a fresh disposable environment and return its secret env_id.

    Returns immediately with state=provisioning; boot takes a few minutes.
    Poll wait_ready or env_status before running commands.

    profile: "base" (SSH only) or "playwright" (adds a Playwright MCP server).
    """
    if profile not in ("base", "playwright"):
        raise ValueError("profile must be 'base' or 'playwright'")
    ttl_minutes = max(1, min(int(ttl_minutes), 350))

    reap()
    slot = free_slot()
    if slot is None:
        raise RuntimeError(
            f"all {SLOT_COUNT} slots are in use; destroy_env one first "
            "or raise SLOT_COUNT"
        )

    env_id = secrets.token_urlsafe(32)
    created = now()
    expires = created + timedelta(minutes=ttl_minutes)
    with db() as conn:
        conn.execute(
            "INSERT INTO envs (env_id, slot, profile, state, label, created_at, expires_at)"
            " VALUES (?, ?, ?, 'provisioning', ?, ?, ?)",
            (env_id, slot, profile, label, iso(created), iso(expires)),
        )
    try:
        await dispatch_workflow(slot, ttl_minutes, profile)
    except Exception:
        with db() as conn:
            conn.execute("DELETE FROM envs WHERE env_id = ?", (env_id,))
        raise

    return {
        "env_id": env_id,
        "slot": slot,
        "profile": profile,
        "state": "provisioning",
        "expires_at": iso(expires),
        "note": "Keep env_id secret; it is the only credential for this box. "
        "Boot usually takes 2-5 minutes.",
    }


@mcp.tool()
async def env_status(env_id: str) -> dict[str, Any]:
    """Report whether the environment is still provisioning or ready to use."""
    row = await resolve(env_id)
    host = host_for(row["slot"])
    if row["state"] == "ready":
        return {"state": "ready", "slot": row["slot"], "expires_at": row["expires_at"]}
    if await probe(host):
        set_state(env_id, "ready", ready=True)
        return {"state": "ready", "slot": row["slot"], "expires_at": row["expires_at"]}
    return {
        "state": "provisioning",
        "slot": row["slot"],
        "expires_at": row["expires_at"],
    }


@mcp.tool()
async def wait_ready(env_id: str, max_wait_seconds: int = 120) -> dict[str, Any]:
    """Block until the environment answers, or until max_wait_seconds passes."""
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
    return await upstream_call(
        host_for(row["slot"]),
        "exec",
        {"command": command},
        timeout=max(10, min(int(timeout_seconds), 600)),
    )


@mcp.tool()
async def sudo_exec(env_id: str, command: str, timeout_seconds: int = 180) -> str:
    """Run a shell command as root inside the environment identified by env_id."""
    row = await resolve(env_id)
    return await upstream_call(
        host_for(row["slot"]),
        "sudo-exec",
        {"command": command},
        timeout=max(10, min(int(timeout_seconds), 600)),
    )


@mcp.tool()
async def destroy_env(env_id: str) -> dict[str, Any]:
    """Shut the environment down now and release its slot."""
    row = await resolve(env_id)
    stopped = False
    try:
        # The workflow's keep-alive loop watches for this file.
        await upstream_call(
            host_for(row["slot"]), "exec", {"command": "touch /tmp/stop.txt"}, timeout=30
        )
        stopped = True
    except Exception:
        pass
    set_state(env_id, "destroyed")
    return {
        "state": "destroyed",
        "slot_released": row["slot"],
        "graceful": stopped,
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
            "slot": r["slot"],
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
    with db() as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM envs WHERE state IN (?, ?)", ACTIVE_STATES
        ).fetchone()[0]
    return PlainTextResponse(f"ok live={live}/{SLOT_COUNT}\n")


init_db()

app = mcp.streamable_http_app()
app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))
app.add_middleware(BearerAuth)
