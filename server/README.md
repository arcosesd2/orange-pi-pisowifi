# PisoWiFi cloud dashboard (`server/`)

A small website that receives signed earnings reports from your PisoWiFi
machines and shows them on any phone/browser: per-machine online status,
earnings today / 7 days / all time, active clients, a 14-day earnings chart,
and board health (CPU temp, uptime, free RAM).

**Why push instead of connecting to the machine directly?** Philippine home
internet is almost always behind CGNAT — your machine has no public IP, so
nothing on the internet can connect *in* to it. But the machine can always
connect *out*, so it pushes a report to this server every few minutes. This
works on any ISP with zero router configuration.

This folder is **not** installed on the Orange Pi — deploy it on any host
with a public address:

- a cheap VPS (DigitalOcean, Vultr, Hetzner…)
- a free/cheap PaaS (Render, Railway, PythonAnywhere)
- a spare PC at home *if* your ISP gives you a real public IP (port-forward)

## Deploy

```bash
pip install flask
export PISO_KEY="pick-a-long-random-secret"     # authenticates the machines
export DASH_PASSWORD="your-viewing-password"    # protects the dashboard (recommended)
python3 app.py                                  # listens on :8000 (override with PORT)
```

For production put it behind HTTPS (the PaaS options give you HTTPS for free;
on a VPS use caddy or nginx + certbot). Data lives in `dashboard.db` (SQLite)
next to `app.py`.

## Connect each machine

On the machine's admin page (`http://10.0.0.1:8080/admin`) → **Remote
monitoring**:

| Field | Value |
|---|---|
| Report URL | `https://your-host/api/heartbeat` |
| Shared secret key | the same `PISO_KEY` |
| Interval | 300 s is fine |
| Device ID | a name per machine, e.g. `site-corner-store` |

Click **Send test report now** — it should show `HTTP 200`, and the machine
appears on the dashboard immediately.

Every report is authenticated with HMAC-SHA256 over the body using the shared
key, so nobody can forge earnings into your dashboard. The dashboard itself
is read-only — it can *see* the machines but never control them.

## Controlling a machine from your phone (optional)

If you also want to open the machine's own admin page from anywhere (kick
clients, change rates, run pin diagnostics remotely), install
[Tailscale](https://tailscale.com) (free for personal use) on the board:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up          # log in with the printed link
tailscale ip -4       # note the 100.x.y.z address
```

Install the Tailscale app on your phone, log into the same account, and open
`http://100.x.y.z:8080/admin` — the full admin + diagnostics UI, end-to-end
encrypted, from anywhere, no port forwarding. This punches through CGNAT
because both ends dial out.
