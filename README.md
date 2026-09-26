# Warp outbound health check for PasarGuard

Python daemon for a [PasarGuard](https://github.com/PasarGuard/panel) panel. It probes each **Warp** (WireGuard) outbound through the panel's per-node latency API and restarts only the core that is down. It comes with a terminal menu for live status and pings, and it can send optional Telegram alerts.

Host ICMP ping of `engage.cloudflareclient.com` is not used, because that ping does not go through the tunnel. The checker calls:

```text
GET /api/node/{node_id}/outbounds_latency?name=warp
```

That reads Xray Observatory on the remote node (`alive` / `delay`). After consecutive failures it calls:

```text
POST /api/core/{core_id}/restart
```

which restarts every node that uses that core.

## Quick install

On the **panel** server, as root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/H-Alireza/pasarguard-warp-healthcheck/main/install.sh)
```

The installer does the following:

1. Asks for the panel URL and admin login, and optionally a Telegram bot for alerts.
2. Runs `doctor` to test them.
3. Offers to add Observatory to your Warp cores.
4. Starts `warp-healthcheck.service`.

Then open the menu:

```bash
sudo warp-healthcheck
```

```text
Warp health check v1.1.1   service: running
tag 'warp' · every 10s · restart after 3 fails · max 6/h · telegram on · updated 2s ago

Core 1 germany  [OK]  restarts 1/h · last 14m ago
  NODE        WARP     PING    FAILS  LAST 30                         DETAIL
  de-hetzner  UP       140ms   0      ▃▃▄▃▃▂▃▃▅▃▃▃▃▄▃▃▃▂▃▃▃▃▃▃▃▃▄▃▃▃
  de-ovh      UP       310ms   0      ▅▅▆▅▅▅▇▅▅▅▅▅▅▅▅▅▆▅▅▅▅▅▅▅█▅▅▅▅▅
  de-offline  OFFLINE  —       0                                      node status is error

Recent events
  14m ago  recovered      germany: Warp up again (de-hetzner 140ms, de-ovh 310ms)
  15m ago  restart        germany: Warp failures on: de-ovh#2

 1) Live status                8) Restart a core now
 2) Probe now (doctor)         9) Set up Observatory
 3) Service logs              10) Send Telegram test
 4) Restart service           11) Update from GitHub
 5) Stop service              12) Uninstall
 6) Start service              0) Exit
 7) Edit config
```

The menu reads the status file the service writes after each cycle (`/var/lib/warp-healthcheck/status.json`), so opening it adds no load on the panel. **LAST 30** shows the last 30 probes: bar height is the ping relative to that node's range, and `x` means no successful probe.

### Update

Update to the latest version. Your config is kept:

```bash
sudo warp-healthcheck update
```

or use menu option 11, or:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/H-Alireza/pasarguard-warp-healthcheck/main/install.sh) update
```

### Uninstall

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/H-Alireza/pasarguard-warp-healthcheck/main/install.sh) uninstall          # keeps /etc/warp-healthcheck
bash <(curl -fsSL https://raw.githubusercontent.com/H-Alireza/pasarguard-warp-healthcheck/main/install.sh) uninstall --purge  # deletes it too
```

## Requirements

- Python 3.11+ (the installer installs it with apt/dnf/yum if missing)
- A sudo (or cores+nodes) admin on the panel
- Xray Observatory covering the Warp outbound tag (default `warp`)

Newer PasarGuard Node builds may inject Observatory automatically. If `warp-healthcheck doctor` reports Observatory unavailable, run `setup` once.

## Commands

| Command | What it does |
| --- | --- |
| *(none)* / `menu` | Interactive menu |
| `status` | Print the status table once. `-w` refreshes every 2s, `--json` prints the raw status |
| `doctor` | Auth, list Warp cores/nodes, one live latency probe, config warnings. No restart. Exit code 0 = all good, 1 = can't monitor (login, panel, no Warp cores), 3 = panel OK but some nodes aren't UP or Observatory needs setup |
| `setup` | Add Observatory for the Warp tag via `PUT /api/core/{id}?restart_nodes=true` |
| `restart-core ID` | Restart one core now |
| `update` | Update from GitHub, keeping config |
| `logs` | `journalctl` for the service (`-n 200`, `--no-follow`) |
| `test-telegram` | Send a test alert |
| `run` | Health-check loop (what systemd runs) |

`setup --dry-run` prints cores that would change. `setup -y` applies without a prompt.

## How a cycle works

1. List cores (`GET /api/cores`) and keep those with a `wireguard` outbound tagged `warp` (or `core_ids` if set).
2. For each core, list **connected** nodes (`GET /api/nodes?core_id=`). Disconnected nodes are skipped.
3. Probe Warp on each connected node in parallel.
4. `alive: true` with a fresh Observatory timestamp resets that node's fail counter.
5. `alive: false` increments the counter. After `fail_threshold` consecutive failures on **any** node, restart that core.
6. After a restart, wait `restart_cooldown_seconds` and ignore failures while Xray re-handshakes.
7. At most `max_restarts_per_hour` restarts per core. When the cap is hit, you get one alert, not one every cycle.

Panel auth or connectivity errors skip the cycle and do not count as Warp down. The following count as **unknown** rather than down, so a config problem never causes restart loops:

- stale Observatory results (`last_try_time` older than `stale_after_seconds`)
- no Observatory result for the tag
- a timed-out latency call for one node

The journal only logs status changes (up → down, down → up), failures, and restarts, plus a one-line summary every 5 minutes.

## Telegram alerts

Optional. The installer asks for them, or you can add them to `/etc/warp-healthcheck/config.yaml` later (menu option 7):

```yaml
telegram:
  bot_token: "123456:ABC..."   # from @BotFather
  chat_id: "-1001234567890"    # your user id or a group/channel id
  proxy: ""                    # e.g. socks5://127.0.0.1:1080 if api.telegram.org is blocked
```

You get a message when a core is restarted, when it recovers, when a restart call fails, and when the hourly restart cap is reached. Run `warp-healthcheck test-telegram` to check the setup.

## Config

See [config.example.yaml](config.example.yaml).

```yaml
panel:
  base_url: https://panel.example.com
  username: admin
  password: "..."
  verify_tls: true

check:
  interval_seconds: 10
  timeout_seconds: 8
  fail_threshold: 3
  restart_cooldown_seconds: 90
  max_restarts_per_hour: 6
  stale_after_seconds: 20
  outbound_tag: warp

core_ids: []   # empty = auto-discover Warp outbounds
```

`stale_after_seconds` defaults to `2 * interval_seconds` when omitted. It must be longer than the core's Observatory `probeInterval`, or every result looks stale. `doctor` warns about this.

Environment overrides: `PASARGUARD_BASE_URL`, `PASARGUARD_USERNAME`, `PASARGUARD_PASSWORD`, `WARP_HEALTHCHECK_TELEGRAM_TOKEN`, `WARP_HEALTHCHECK_TELEGRAM_CHAT_ID`, `WARP_HEALTHCHECK_STATUS_FILE`.

## Installer options

```bash
sudo bash install.sh \
  --non-interactive \
  --base-url https://panel.example.com \
  --username admin \
  --password 'secret' \
  --telegram-token '123:abc' --telegram-chat-id '-100123' \
  --setup
```

`--setup` adds Xray Observatory for the Warp tag and restarts those cores. Without it, run `warp-healthcheck setup` yourself after `doctor`. Other actions: `update`, `upgrade` (install from a local checkout), `reconfigure` (rewrite the config interactively), `uninstall [--purge]`. See `bash install.sh --help`.

The installer puts a venv in `/opt/warp-healthcheck`, writes `/etc/warp-healthcheck/config.yaml` (mode 600), links `warp-healthcheck` into `/usr/local/bin`, and enables a sandboxed `warp-healthcheck.service`. The service runs with no capabilities, a read-only filesystem apart from `/var/lib/warp-healthcheck`, and network access only. An existing config is left alone unless you pass `--force-config`.

For a manual install, create `/opt/warp-healthcheck/.venv`, run `pip install .`, copy `config.example.yaml` to `/etc/warp-healthcheck/config.yaml`, then install `systemd/warp-healthcheck.service`.

## Observatory snippet

`setup` adds this when the core has no Observatory (or appends `warp` to `subjectSelector` if it already exists):

```json
{
  "observatory": {
    "subjectSelector": ["warp"],
    "probeURL": "https://www.cloudflare.com/cdn-cgi/trace",
    "probeInterval": "10s",
    "enableConcurrency": true
  }
}
```

Versions before 1.1.1 wrote `probeUrl` here. Xray ignores that key (the field is `probeURL`), so those cores probed Xray's default URL instead. `doctor` flags them, and `setup` renames the key.

If the core already uses `burstObservatory`, `setup` adds the Warp tag there instead of creating a second observer.

The outbound itself is unchanged. A typical Warp outbound looks like:

```json
{
  "protocol": "wireguard",
  "tag": "warp",
  "settings": {
    "mtu": 1280,
    "noKernelTun": true,
    "peers": [
      {
        "endpoint": "engage.cloudflareclient.com:2408",
        "keepAlive": 5
      }
    ]
  }
}
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE)
