# Ephemeral environment broker

A small always-on MCP server that hands out disposable Linux boxes running on
GitHub Actions runners, one per conversation. Shell and browser are both
reached through this single endpoint.

```
Notion ──HTTPS + Bearer──▶ broker (Docker, on your tailnet)
                              │  workflow_dispatch(env_host, ttl, profile)
                              ▼
                      GitHub Actions runner  "gha-env-<random>"
                              ▲
                              ├─ SSH over the tailnet → shell
                              └─ SSH port forward → 127.0.0.1:8931
                                                     Playwright MCP
```

## Why it is built this way

**The environment listens on nothing.** For both profiles. The shell is
reached through Tailscale SSH, which tailscaled handles in-process, so it
works under userspace-networking with no sshd and no open port. Playwright MCP
binds loopback only and the broker tunnels to it over that same SSH
connection, so it needs no `serve` rule either.

**One endpoint, one token.** Clients never talk to a runner directly. The
Playwright tools are re-exposed as `browser_tools` / `browser_call` on the
broker, scoped by `env_id` like everything else.

**The caller is authenticated by the tailnet.** Tailscale SSH checks the
identity of the connecting node against your ACLs, so execution can be
restricted to the broker host:

```json
"ssh": [{
  "action": "accept",
  "src":    ["tag:broker"],
  "dst":    ["tag:gha-env"],
  "users":  ["runner"]
}]
```

Note that the broker runs in a container on a bridge network, so its traffic
is NATed to the *host's* tailnet identity — write the rule against the broker
host, not the container. Do not set `checkPeriod`: check mode requires a
browser interaction and will hang a non-interactive connection.

**No TLS on the tailnet leg.** SSH inside WireGuard is already two layers.
Host-key checking is disabled because the runner is created fresh every time
and its key is therefore new every time; the tailnet is the trust anchor.

**`env_id` is the only credential clients hold.** It is 256 bits from
`secrets.token_urlsafe` and lives exclusively in the broker's SQLite database.
The workflow is dispatched with nothing but a random hostname, a TTL and a
profile name, so it does not matter that workflow inputs are world-readable on
a public repository.

**Ephemeral Tailscale nodes.** Devices deregister themselves when the job ends,
so there is no cleanup step that could delete a sibling environment by mistake.

## macOS environments

`create_env(platform="macos")` dispatches `.github/workflows/ephemeral-env-macos.yml`
on a `macos-15` runner instead of `ephemeral-env.yml`. Everything a client sees
is unchanged: same endpoint, same bearer token, same `env_id`, same `exec` /
`sudo_exec` / `destroy_env`.

What had to change under the hood is authentication. **The Tailscale SSH server
is Linux-only** -- the macOS CLI build of `tailscaled` never starts it -- so
there is nothing on macOS that can authenticate the broker by tailnet identity.
Instead:

1. the broker mints an ed25519 keypair per environment and stores the private
   half next to the `env_id`,
2. the **public** half is passed in as the `ssh_pubkey` workflow input, which is
   safe even though workflow inputs are world-readable,
3. the job writes it to `~/.ssh/authorized_keys`, starts its own `sshd` bound to
   `127.0.0.1:2222`, and publishes that with
   `tailscale serve --bg --tcp 2222 tcp://127.0.0.1:2222`.

So the runner still opens no port to the internet, still needs no TUN device,
and access is gated twice: the tailnet ACL decides who may reach port 2222, the
keypair decides who may log in.

The ACL therefore needs a port rule as well as (or instead of) the `ssh` block:

```json
"acls": [{
  "action": "accept",
  "src":    ["tag:broker"],
  "dst":    ["tag:gha-env:2222"]
}]
```

Profiles on macOS are `base`, `playwright` and `xcode`; the last one runs
`xcode-select` against the image default Xcode and installs XcodeGen, which is
what an iOS project generated from a `project.yml` needs.

Caveats worth knowing before you reach for it:

* macOS minutes bill at **10x** Linux minutes on GitHub-hosted runners.
* Boot is slower (4-8 minutes; Homebrew installs Tailscale from scratch).
* Runners are arm64 (Apple silicon).
* `serve` must be permitted for the `tag:gha-env` nodes in your tailnet.

## Setup

### 1. Tailscale auth key

Create a key at <https://login.tailscale.com/admin/settings/keys> with
**Ephemeral**, **Reusable** and **Pre-approved** enabled, tagged `tag:gha-env`
if you use the ACL above, and store it as the repository secret `TS_AUTHKEY`.

### 2. GitHub token

A fine-grained PAT limited to this repository with **Actions: Read and write**.

### 3. Run the broker

On a host that has already joined your tailnet **in kernel mode** (verify with
`ip a | grep tailscale0`):

```bash
cd broker
cp .env.example .env && $EDITOR .env
docker compose up -d          # pulls ghcr.io/animationsaver/windows/broker
curl localhost:8080/healthz
```

The image is built and published by `.github/workflows/broker-image.yml`
(linux/amd64 + linux/arm64), so nothing is compiled on the broker host. Update
with `docker compose pull && docker compose up -d`, or build locally with
`docker compose up -d --build`.

The container runs on a normal bridge network and is published on
`127.0.0.1:8080` only. Routing to the tailnet works through the host; the one
thing that needs pinning is DNS, because Docker discards the loopback
nameserver that systemd-resolved advertises MagicDNS on. The compose file
therefore points the container at Tailscale's resolver (`100.100.100.100`)
directly. If your broker host runs tailscaled with
`--tun=userspace-networking`, that resolver does not exist and you will need
`network_mode: host` instead.

### 4. Expose the broker

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8080
```

This is the one and only public entry point. Connect Notion to
`https://<broker-host>.<tailnet>.ts.net/mcp` with the `BROKER_TOKEN` as a
bearer token, and list that same hostname in `BROKER_PUBLIC_HOSTS`.

## Tools

| Tool | Purpose |
| --- | --- |
| `create_env(ttl_minutes, profile, label)` | Provision a box; returns the secret `env_id`. Returns immediately — boot takes 2–5 minutes. |
| `wait_ready(env_id, max_wait_seconds)` | Poll until the box answers. |
| `env_status(env_id)` | `provisioning` / `ready` plus expiry. |
| `exec(env_id, command, timeout_seconds)` | Run a command as `runner`. |
| `sudo_exec(env_id, command, timeout_seconds)` | Run a command as root. |
| `browser_tools(env_id)` | List the Playwright tools this environment offers, with their schemas. |
| `browser_call(env_id, tool, arguments)` | Invoke one of them. Screenshots come back as images. |
| `destroy_env(env_id)` | Stop the runner and free capacity. |
| `list_envs()` | Live environments, with `env_id` masked. |

Profiles: `base` (shell only) or `playwright` (adds headless Chromium). The
profile is fixed at creation, so ask for `playwright` up front if the browser
might be needed.

Playwright is proxied generically rather than mirrored tool by tool, so a new
playwright-mcp release changes what `browser_tools` reports without needing a
broker release. The cost is one extra call at the start of a browsing session.

## Operational notes

- The broker keeps one SSH connection per environment and opens a fresh
  channel per command, so calls run genuinely in parallel. Working directory
  and shell variables do **not** persist between calls — chain with
  `cd /path && ...`.
- Browser state does persist: `browser_call` talks to one long-lived Chromium
  for the life of the environment.
- On timeout the broker closes that channel only. Nothing is `pkill`ed, so
  unrelated processes are never caught in the blast radius. For anything long,
  detach anyway: `nohup ... > /tmp/job.log 2>&1 &`.
- A dead pooled connection, and the Playwright tunnel riding on it, are
  retried once transparently.
- `wait_ready` reports readiness of the shell. On `playwright` the browser may
  need another minute after that while Chromium finishes installing.
- Jobs are hard-stopped by GitHub after 6 hours regardless of TTL.

### Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `421 Misdirected Request` | `BROKER_PUBLIC_HOSTS` does not list the hostname the client uses |
| `401 unauthorized` from the broker | `BROKER_TOKEN` mismatch between `.env` and the MCP client |
| `ssh to ... failed: Permission denied` | Tailscale ACL does not grant `ssh` from the broker host to the environment, or `users` omits `runner` |
| `ssh to ... failed` hangs then times out | ACL uses check mode (`checkPeriod`), which cannot work non-interactively |
| `could not reach Playwright MCP` | Environment is on `profile='base'`, or Chromium is still installing — check `env-logs-*` artifact for `pw-mcp.log` |
| `create_env` works, `wait_ready` never turns ready | The container cannot resolve MagicDNS. `docker compose exec broker python -c "import socket;print(socket.gethostbyname('<host>'))"` |
| Name resolution fails but `100.x` IPs ping | `dns:` is not taking effect, or tailscaled is in userspace mode on this host |
| `workflow_dispatch failed: 404` | The PAT lacks Actions write, or `ephemeral-env.yml` is not on the default branch |
