# Ephemeral environment broker

A small always-on MCP server that hands out disposable Linux boxes running on
GitHub Actions runners, one per conversation.

```
Notion ──HTTPS + Bearer──▶ broker (Docker, on your tailnet)
                              │  workflow_dispatch(env_host, ttl, profile)
                              ▼
                      GitHub Actions runner  "gha-env-<random>"
                              │  tailscale serve --http=8932  (tailnet only)
                              ▼
                      mcp-proxy ──▶ ssh-mcp ──▶ sshd
```

## Why it is built this way

**Only the broker is public.** Environments are published with
`tailscale serve`, not `tailscale funnel`, so they are reachable from inside
your tailnet and nowhere else. `serve` is required even for that: tailscaled
runs in userspace-networking mode on the runner, so without a serve rule there
is no path from the tailnet into a localhost port.

**No TLS on the tailnet leg.** Broker-to-environment traffic is already
encrypted by WireGuard. Terminating TLS there would add a Let's Encrypt
dependency plus its per-tailnet rate limits (`ts.net` is on the Public Suffix
List, so every environment would draw from the same quota) in exchange for a
second, redundant layer of encryption.

**`env_id` is the only credential.** It is 256 bits from `secrets.token_urlsafe`
and lives exclusively in the broker's SQLite database. The workflow is
dispatched with nothing but a random hostname, a TTL and a profile name, so it
does not matter that workflow inputs are world-readable on a public repository.
Every tool call must carry `env_id`; it is a required parameter rather than a
string prefix, so it cannot be forgotten or mangled by quoting.

**Ephemeral Tailscale nodes.** Devices deregister themselves when the job ends,
so there is no cleanup step that could delete a sibling environment by mistake.

## Setup

### 1. Tailscale auth key

Create a key at <https://login.tailscale.com/admin/settings/keys> with
**Ephemeral**, **Reusable** and **Pre-approved** enabled (tag it if your ACLs
require one), and store it as the repository secret `TS_AUTHKEY`.

### 2. GitHub token

A fine-grained PAT limited to this repository with **Actions: Read and write**.

### 3. Run the broker

On a host that has already joined your tailnet:

```bash
cd broker
cp .env.example .env && $EDITOR .env
docker compose up -d          # pulls ghcr.io/animationsaver/windows/broker
curl localhost:8080/healthz
```

The image is built and published by
`.github/workflows/broker-image.yml` (linux/amd64 + linux/arm64), so nothing is
compiled on the broker host. Update with `docker compose pull && docker compose
up -d`, or build locally with `docker compose up -d --build`.

The container uses `network_mode: host` so it can resolve MagicDNS names
through the host's tailscaled.

### 4. Expose the broker

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8080
```

This is the one and only public entry point. Connect Notion to
`https://<broker-host>.<tailnet>.ts.net/mcp` with the `BROKER_TOKEN` as a
bearer token.

## Tools

| Tool | Purpose |
| --- | --- |
| `create_env(ttl_minutes, profile, label)` | Provision a box; returns the secret `env_id`. Returns immediately — boot takes 2–5 minutes. |
| `wait_ready(env_id, max_wait_seconds)` | Poll until the box answers. |
| `env_status(env_id)` | `provisioning` / `ready` plus expiry. |
| `exec(env_id, command, timeout_seconds)` | Run a command as `runner`. |
| `sudo_exec(env_id, command, timeout_seconds)` | Run a command as root. |
| `destroy_env(env_id)` | Stop the runner and free capacity. |
| `list_envs()` | Live environments, with `env_id` masked. |

Profiles: `base` (SSH only) or `playwright` (also starts a Playwright MCP
server on port 8931).

## Operational notes

- Each `exec` opens its own SSH channel, so calls run genuinely in parallel.
  Working directory and shell variables do **not** persist between calls —
  chain with `cd /path && ...`.
- ssh-mcp is started with `--timeout=600000`. Its default of 60 s makes it kill
  the remote process with `pkill -f`, which can match unrelated commands.
  For anything long, detach: `nohup ... > /tmp/job.log 2>&1 &`.
- `sshd` is configured with `MaxSessions 50`, since the default of 10 caps how
  many tool calls can run at once over the single SSH connection.
- Jobs are hard-stopped by GitHub after 6 hours regardless of TTL.
- A watchdog restarts the MCP servers if they crash mid-session; logs are
  uploaded as a run artifact.
