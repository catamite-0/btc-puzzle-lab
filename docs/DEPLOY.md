# Deploying the control VPS

The lab runs on two machines with very different jobs.

| | Control VPS | Hunt box |
|---|---|---|
| Lifetime | always on | rented by the hour, thrown away |
| Hardware | 1 vCPU, 1 GB is plenty | GPU (RTX 4090 / 5090) |
| Holds | `config/relay-secret`, the payout address | nothing worth stealing |
| Runs | `hub` | `auto <id> --relay …` |
| Setup | `scripts/control-install.sh` | `scripts/machine-bootstrap.sh` |

A hunt box finds a key, seals it to the control host's public key, and POSTs it.
It cannot open what it sent, has no payout address, and on most rented networks
cannot reach Discord or Telegram at all. Everything that touches money happens
on the control VPS.

## 1. The box

Any always-on Linux host with outbound HTTPS. It is a webhook receiver: it
idles, and on a hit it does one unseal, one notification, and a few explorer
calls. GCP `e2-micro`, Hetzner CX22, a $5 Vultr instance — all oversized.

Ubuntu 24.04 keeps this shortest: it ships Python 3.12, which the package
requires.

**Do not run `machine-bootstrap.sh` here.** That script installs a compiler and
builds keyhunt and kangaroo. A control host never searches, and building
cryptocurrency solvers is exactly what free-tier acceptable-use clauses are
written about. `control-install.sh` installs the Python package and nothing
else — no gcc, no libgmp, no CUDA.

```bash
git clone https://github.com/catamite-0/btc-puzzle-lab.git
cd btc-puzzle-lab
bash scripts/control-install.sh
```

## 2. Back up the seal secret before anything else

`relay-keygen` wrote `config/relay-secret`. It is the only unrecoverable thing
in this deployment.

Machines are replaceable: reinstall, restore, carry on. That secret is not. Lose
it and every sealed hit already sitting in a hunt box's outbox becomes
permanently undecryptable — including one that arrives while you are rebuilding.

Copy it somewhere offline now — password manager, encrypted drive, paper. It is
64 hex characters.

```bash
cat config/relay-secret
```

## 3. Settings

```bash
./.venv/bin/btc-puzzle-lab config \
    --dest bc1qyour-payout-address \
    --notify https://ntfy.sh/your-topic \
    --new-relay-token
```

The token prints once — it is what hunt boxes authenticate with. Sweeps stay
**dry-run** until you deliberately enable live broadcast; read
[TRANSFER.md](TRANSFER.md) first.

## 4. Do not expose the hub directly

`hub` is a plain `ThreadingHTTPServer`. There is no TLS, and authentication is a
single bearer token. It also holds the key that unseals private keys. Binding it
to a public interface puts a key-unsealing endpoint on the internet in
cleartext, so it always binds to localhost and something else terminates TLS.

Two ways. The tunnel is the better one.

### Option A — Cloudflare Tunnel (no public IP, no certificates, no open port)

`cloudflared` dials *out* to Cloudflare, so the box needs no inbound firewall
rule and no static IP. On a cloud provider that bills for external addresses,
that is also the one recurring cost removed. Cloudflare's edge stays reachable
from networks where the origin would not be.

```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

cloudflared tunnel login
cloudflared tunnel create btc-lab-hub
cloudflared tunnel route dns btc-lab-hub relay.example.com
```

`/etc/cloudflared/config.yml`:

```yaml
tunnel: btc-lab-hub
credentials-file: /root/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: relay.example.com
    service: http://127.0.0.1:8787
  - service: http_status:404
```

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

### Option B — Caddy on a public IP

```bash
sudo apt-get install -y caddy
```

`/etc/caddy/Caddyfile`:

```
relay.example.com {
    reverse_proxy 127.0.0.1:8787
}
```

Caddy obtains a certificate automatically. Open 80 and 443 in the provider
firewall and **nothing else** — in particular not 8787.

## 5. Keep the hub running

`/etc/systemd/system/btc-lab-hub.service`, with `User=` and the paths matching
your install:

```ini
[Unit]
Description=btc-puzzle-lab control hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=btclab
WorkingDirectory=/home/btclab/btc-puzzle-lab
Environment=BTC_PUZZLE_LAB_HOME=/home/btclab/btc-puzzle-lab
ExecStart=/home/btclab/btc-puzzle-lab/.venv/bin/btc-puzzle-lab hub --host 127.0.0.1 --port 8787
Restart=always
RestartSec=5

# The unit reads config/relay-secret and can sign transactions. Give it as
# little of the host as systemd will let you.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/btclab/btc-puzzle-lab/state /home/btclab/btc-puzzle-lab/config

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now btc-lab-hub
curl -s https://relay.example.com/health     # {"ok": true}
```

`/health` needs no token. `/hit` returns 401 without the bearer token — worth
confirming from outside the box:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://relay.example.com/hit -d '{}'
```

## 6. Prove the whole chain before you need it

Do not let a real hit be the first time this path runs. Point a hunt box at an
**already-solved** puzzle so it produces a hit within seconds, and watch it
travel:

```bash
# on the hunt box
btc-puzzle-lab auto 40 \
    --relay https://relay.example.com/hit \
    --relay-seal-pubkey <pubkey from relay-keygen> \
    --relay-token <token from config --new-relay-token>
```

You are checking four things, in order:

1. the hunt box seals and POSTs (no 401, no connection refused)
2. the hub unseals it — `state/runs.jsonl` on the control VPS records the event
3. the notification actually arrives on your phone
4. `btc-puzzle-lab audit` on the control VPS verifies the recorded hit

If the notification does not arrive, you have found that out on a puzzle whose
answer was already public, which is the entire point of doing this now.

Only after all four pass is it worth pointing anything at an unsolved target,
and only then is it worth reading [TRANSFER.md](TRANSFER.md) about `--live`.

## 7. Hunt boxes

Per rental, from [MACHINE.md](MACHINE.md) — or `china-bootstrap.sh` on a
mainland-China box, which mirrors PyPI and GitHub and builds only RCKangaroo.

Nothing sensitive is copied to them: the pubkey and the token are enough to send
a sealed hit and useless for opening one. Passing `--relay` also disables the
local sweep, so a dest left over in a hunt box's `.env` cannot cause a second,
competing broadcast.

For runs longer than a few hours, add the watchdog (`scripts/watchdog.py`,
supervised by `scripts/supervise.sh`). It exists because a kangaroo DP table
grew 35 GB/h into a 116 GB limit, was OOM-killed every ~3.5 hours, and lost all
accumulated work each time — while `nvidia-smi` reported 99% utilisation
throughout and nothing was ever logged as wrong.
