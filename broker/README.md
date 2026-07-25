# gha-env-broker

A small always-on MCP server that hands out **disposable Linux environments**,
one per conversation, backed by GitHub Actions runners.

```
Notion / any MCP client
        │  HTTPS + Bearer token
        ▼
   broker (Docker, on your tailnet)          <-- the only public surface
        │  workflow_dispatch (slot, ttl, profile)   -- nothing secret
        ▼
   GitHub Actions runner "gha-env-NN"
        │  tailscale serve (tailnet only, NEVER funnel)
        └─ mcp-proxy ── ssh-mcp ── sshd (localhost)
```

## Why it is built this way

| Risk in the older single-box setup | How this design removes it |
| --- | --- |
| MCP endpoint published with `tailscale funnel`, unauthenticated | Environments use `tailscale serve` (tailnet only). Only the broker is public, behind a bearer token. |
| Every client shared one box, one OS user, one SSH connection | One runner per session. No shared filesystem, no shared SSH connection, no `pkill -f` collateral damage. |
| `GH_PAT` written to `~/.git-credentials` on a world-reachable box | No PAT is injected into environments at all. |
| Stale-device cleanup deleted siblings by hostname prefix | Ephemeral auth keys; no cleanup step; one slot per concurrency group. |
| Nothing restarted a crashed MCP server | Watchdog loop in the keep-alive step. |
| Session ids in workflow inputs would be public | Inputs carry only the slot number. The secret `env_id` never leaves the broker. |

## Setup

### 1. Tailscale auth key

Create a key in the Tailscale admin console with **Reusable**, **Ephemeral**
and **Pre-approved** enabled (tag it, e.g. `tag:gha-env`, and restrict that tag
in your ACLs so environments can only reach what they need).

Add it to the repository as the secret `TS_AUTHKEY`. MagicDNS and HTTPS
certificates must be enabled for the tailnet.

### 2. GitHub token

Fine-grained PAT, this repository only, **Actions: Read and write**.

### 3. Run the broker

```bash
cd broker
cp .env.example .env   # fill in GITHUB_TOKEN, TAILNET_DOMAIN, BROKER_TOKEN
docker compose up -d --build
curl localhost:8080/healthz
```

The container uses host networking so it can reach `100.64.0.0/10` and resolve
MagicDNS names via the host's `tailscaled`.

### 4. Expose the broker (and only the broker)

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8080
```

Endpoint for MCP clients: `https://<broker-host>.<tailnet>.ts.net/mcp`
with header `Authorization: Bearer $BROKER_TOKEN`.

If your client lives inside the tailnet, use `tailscale serve` instead and skip
public exposure entirely.

## Tools

| Tool | Purpose |
| --- | --- |
| `create_env(ttl_minutes, profile, label)` | Provision a box, return the secret `env_id`. Returns immediately. |
| `wait_ready(env_id, max_wait_seconds)` | Poll until the box answers (boot is 2-5 min). |
| `env_status(env_id)` | `provisioning` / `ready` + expiry. |
| `exec(env_id, command, timeout_seconds)` | Run a command. |
| `sudo_exec(env_id, command, timeout_seconds)` | Run it as root. |
| `destroy_env(env_id)` | Stop the runner, free the slot. |
| `list_envs()` | Live environments, `env_id`s masked. |

Typical conversation:

```
create_env(profile="base")      -> env_id = "kQ8..."
wait_ready(env_id)              -> ready
exec(env_id, "uname -a")
...
destroy_env(env_id)
```

`profile="playwright"` additionally starts a Playwright MCP server on
`https://gha-env-NN.<tailnet>:8443/mcp` (reachable from the tailnet; the broker
does not proxy it).

## Operational notes

* **Slots.** `SLOT_COUNT` fixed hostnames (`gha-env-01` …) are recycled. Fixed
  names keep the cached TLS certificate usable — Let's Encrypt rate-limits new
  certificates per tailnet, so *don't* switch to a random hostname per session.
* **No shell state between calls.** Every `exec` opens a new SSH channel, so
  `cd` does not persist. Chain with `&&`.
* **Long jobs.** `ssh-mcp` runs with `--timeout=600000` (10 min) and kills
  overrunning commands with `pkill -f`. Detach anything longer:
  `nohup ... > /tmp/job.log 2>&1 &`.
* **Everything is lost at teardown.** Push results to git or copy them out
  before the TTL expires; the 6 h job limit is a hard ceiling.
* **Restart policy.** The broker's SQLite lives in the `broker-data` volume, so
  restarts keep the env registry. Environments that died meanwhile simply fail
  their next `exec` and can be recreated.
* GitHub caps concurrent Actions jobs per account, so keep `SLOT_COUNT` well
  below that limit and remember this is best treated as CI-adjacent scratch
  capacity, not a hosting platform.
