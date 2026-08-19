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
and only then is it worth reading the next section.

## 7. Turning on live broadcast

Everything above leaves sweeps in **dry-run**: a hit is signed and written to
`state/dryrun_*.txhex`, and nothing reaches the network. This is the only step
that spends real money, so it is deliberately the last one and deliberately
awkward to do by accident.

### The switch

Two settings must both hold. `AUTO_TRANSFER_DRY_RUN=false` on its own is not
enough — the broadcast path checks the confirm phrase separately and returns
`live broadcast blocked: missing AUTO_TRANSFER_LIVE_CONFIRM`.

```ini
AUTO_TRANSFER_DRY_RUN=false
AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC
```

`btc-puzzle-lab config --dest <addr> --live` writes both.

### What still stands between a hit and a broadcast

Live does not mean unconditional. In order:

| Check | Effect |
|---|---|
| The hit must audit | The hub derives the address from the key it just unsealed and compares. A mismatch is recorded and notified, never swept. |
| Sealed payload is authoritative | A POST whose outer `puzzle_id` or `address` disagrees with the sealed content is rejected outright. |
| Duplicates | An already-recorded hit is not swept a second time. |
| `AUTO_TRANSFER_MIN_BALANCE_SATS` (5000) | Below this the UTXO set is left alone. |
| `AUTO_TRANSFER_MIN_SEND_SATS` (546) | Refuses to create a dust output. |
| `AUTO_TRANSFER_MAX_FEE_SATS` (100000) | Absolute ceiling on the fee, whatever the rate maths says. |
| `AUTO_TRANSFER_MAX_FEE_RATE` (250 sat/vB) | Checked twice: once against the estimate, and again against `fee / vsize` of the transaction actually built — an estimate that looked sane cannot become an expensive transaction on the way out. |
| `AUTO_TRANSFER_CONFIRMED_ONLY` (true) | Unconfirmed UTXOs are not spent. |
| `AUTO_TRANSFER_RBF` (true) | Inputs signal replace-by-fee, so a stuck sweep can be re-fee'd. |

### Confirm the destination against a real transaction first

The one thing no check above can catch: whether `AUTO_TRANSFER_DEST_ADDR` is
*your* address. The code validates that it is **a** well-formed Bitcoin address,
not that it is yours. One wrong character that still forms a valid address sends
the money to someone else, irreversibly.

Reading the address off the screen is not the same as watching a signed
transaction and confirming where it pays. While still in dry-run, produce one and
check it:

```bash
btc-puzzle-lab verify-dry-run state/dryrun_<id>.txhex --check-dest
```

`--check-dest` asserts the output actually pays `AUTO_TRANSFER_DEST_ADDR` and
clears the minimum-send floor. Do this before flipping the switch, not after.

### Who pulls the trigger

Automatic broadcast buys speed, which is the honest argument for it: the funded
addresses in this puzzle series are watched, and a slow sweep can be front-run.

The cost is that your control VPS becomes something that can spend money on its
own. A compromise of the box, an edited `.env`, or a bad response from an
explorer all end in a real transaction.

There are three arrangements, and the switch above is only about the first.

**Hub broadcasts.** Live enabled, `hub` started normally. A hit that audits is
swept within seconds and you find out from the notification. Fastest, and the
only one that works while you are asleep.

**Hub signs, you send.** Leave `AUTO_TRANSFER_DRY_RUN=true` and let the hub
sweep. It builds and signs the transaction into `state/dryrun_*.txhex` and
notifies, but sends nothing. To release it you set the two live settings and run:

```bash
btc-puzzle-lab verify-dry-run state/dryrun_<id>.txhex --check-dest
btc-puzzle-lab transfer --broadcast-dry-run state/dryrun_<id>.txhex
```

`--broadcast-dry-run` refuses unless both live settings are in place, and
re-verifies the destination and the minimum send against the artifact before it
goes out — so the address check happens again at the moment it matters, not only
when you first configured it.

**Hub only tells you.** `hub --no-sweep`, live enabled. The hub unseals, audits,
records and notifies, and does not build a transaction at all. You sweep from
scratch when you are ready:

```bash
btc-puzzle-lab transfer --puzzle <id>
```

The second and third both turn the delay into however long it takes you to read
the alert and act. Whether that is acceptable depends on the target: a long-tail
unsolved puzzle gives you all the time you need, a contested one may not.

## 8. Hunt boxes

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
