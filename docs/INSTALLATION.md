# Installation

Three things get a network selling access: this server running, a router that dials in, and a payment
account. Each is independent, so you can test the first two before touching payments.

---

## 1. Deploy the server

### Option A — Dokploy (recommended)

1. Create a project, choose **Compose**, and point it at this repository. Compose path:
   `docker-compose.yml`.
2. Add the environment variables from `.env.example`. Generate the two required secrets:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(50))"                                  # DJANGO_SECRET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"     # FIELD_ENCRYPTION_KEY
   ```

3. Set `PUBLIC_BASE_URL` to your domain, and `ROUTER_TUNNEL_HOST` to the hostname routers will dial
   (must resolve publicly — see step 3).
4. Attach a domain to the `web` service on port **8000**, enable HTTPS.
5. Deploy. The entrypoint runs migrations, collects static files, creates the platform owner from
   `ADMIN_USERNAME`/`ADMIN_PASSWORD`, then starts Gunicorn.

Health check: `GET /healthz/` returns `ok`.

> **Changing `FIELD_ENCRYPTION_KEY` later** makes every stored router password and payment secret
> undecryptable. Rotate it only by re-entering those secrets in the panel.

### Option B — Docker on one box

```bash
cp .env.example .env      # fill in the two secrets and PUBLIC_BASE_URL
docker compose up -d --build
```

Put Caddy or nginx in front for TLS, or set `COOKIE_SECURE=false` only for local testing.

### Option C — no Docker (laptop or a single VPS)

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # set the two secrets
python manage.py migrate
python manage.py bootstrap                        # creates the platform owner
python manage.py runserver
```

For production without Docker, run `python manage.py collectstatic` once, then Gunicorn:

```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4
```

The sweeper (expiry, FUP, health sampling) starts inside the web process. To run it separately
instead, set `RUN_SCHEDULER=false` on the web service and run `python manage.py tick` from cron
every 30 seconds.

---

## 2. Route requests

`web` listens on 8000 and serves everything: marketing, capture page, panel, webhooks. Your reverse
proxy must:

- pass `X-Forwarded-Proto` so Django knows the request is HTTPS;
- **not** cache `/portal/`, `/admin/` or `/payments/`;
- allow public access to `/portal/`, `/payments/`, `/routers/<token>/report/` and `/signup/`. The
  router report endpoint is token-authenticated, not session-authenticated.

---

## 3. Install TunGuard on the Docker host

Pingota calls TunGuard's API to register each router and sends the router's WireGuard traffic to
TunGuard. Run TunGuard as a Linux service on the same VPS that runs Dokploy; keep Pingota in the
Dokploy Compose deployment. TunGuard's supported setup is its Linux binary, not another Compose
service. Install it using the [official TunGuard guide](https://tunguard.github.io/docs/) and
[installer](https://github.com/TunGuard/tanguard-binary).

Set these environment variables on the TunGuard `tanguard` systemd service (for example, with
`sudo systemctl edit tanguard`), then restart it:

```ini
[Service]
Environment="WG_ADDRESS=10.70.0.1/16"
Environment="WG_SUBNET=10.70.0.0/16"
Environment="WG_LISTEN_PORT=13231"
Environment="API_LISTEN=:9000"
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart tanguard
sudo systemctl status tanguard
```

Change the default TunGuard dashboard login on first sign-in. In **Settings → API Key**, create an
API key. Pingota uses this key for the peer add/remove API calls.

### Network and subnet

Keep the defaults below unless you have a reason to change them. The subnet is an ordinary private
IPv4 CIDR, but it must be the **same pool in TunGuard and Pingota**, and it must not overlap the
VPS's LAN, the Docker networks, or networks behind the customer routers. `/16` provides roughly
65,000 usable addresses; `.1` is reserved for TunGuard and Pingota assigns the remaining addresses
to routers. The prefix can be changed (for example to `/24` for about 250 routers), but change all
three subnet values together and leave `.1` for the server. The routers' own hotspot/LAN subnet is
separate and may be different for each site; it should not overlap this tunnel pool.

Open **UDP 13231** on the VPS firewall/security group for router connections. Do not expose TunGuard's
**TCP 9000** API/dashboard to the public internet. Pingota's container reaches it through the Docker
host gateway configured in Compose.

Set these variables in Pingota's Dokploy environment:

```dotenv
ROUTER_TUNNEL_HOST=vpn.yourdomain.example
ROUTER_TUNNEL_PORT=13231
ROUTER_TUNNEL_SUBNET=10.70.0.0/16
TUNGARD_API_URL=http://host.docker.internal:9000
TUNGARD_API_KEY=<key from TunGuard Settings → API Key>
```

Also set `PUBLIC_BASE_URL` to Pingota's HTTPS URL, and set `MONGODB_URI` to a MongoDB address
reachable **from the Pingota container**. `localhost` inside that container means the container
itself; use the Compose service name for bundled Mongo (`mongodb://mongo:27017`) or a host-reachable
address for Mongo running elsewhere.

For router management to work end to end, the host and Docker network must pass traffic to the
tunnel subnet, and Pingota must be able to reach connected routers over TCP **8728**. The router
onboarding script configures the corresponding tunnel address and allows the API only from
TunGuard's `.1` address.

---

## 4. Add a router

1. In the panel go to **Network → Add router**, name it after the place it serves.
2. Open the router page and press **Copy** on the script.
3. On the router: WinBox → **New Terminal** (or SSH), paste the whole block, press Enter.
4. Within about ten seconds the page changes from *Waiting for install* to **Connected**, showing the
   RouterOS version and board name.

Before showing the script, Pingota registers the router's public key and tunnel address with TunGuard.
The script then installs the matching private key and:

| Line | Purpose |
| --- | --- |
| `/interface/wireguard/add` | Creates the tunnel interface with its registered private key |
| `/ip/address/add` | Assigns the router's unique tunnel address |
| `/interface/wireguard/peers/add` | Points at the TunGuard server key and public endpoint |
| `/user/group/add` + `/user/add` | API user limited to `read,write,api,test`; no reboot, no policy, no sensitive |
| `/ip/service/set api address=…/32` | API port reachable only from Pingota's tunnel address |
| `/ip/firewall/filter/add …` | Explicit accept for port 8728 from Pingota's tunnel address |
| `/tool/fetch …report/` | Tells the panel the install worked |

Safe to paste twice: the tunnel and user are replaced, not duplicated.

### Point the hotspot at the capture page

In `/ip/hotspot/profile`, set the login page to your portal:

```
/ip hotspot profile set [find] login-by=mac,cookie,http-chap
/ip hotspot walled-garden add dst-host=<your-domain> action=allow
```

The capture page reads the device MAC from the hotspot redirect (`?mac=$(mac)`); without it the guest
sees prices but the buttons stay disabled. For the common one-AP setup the MikroTik `login.html`
contains a redirect to:

```
https://your-domain/portal/<isp-slug>/?mac=$(mac)&link-login-only=$(link-login-only)
```

### Bandwidth profiles

Passes name a hotspot profile. Create them once on the router:

```
/ip hotspot user profile add name=hsprof1 rate-limit=2M/2M shared-users=1
/ip hotspot user profile add name=fup-slow rate-limit=256k/128k
```

If a package's FUP names `fup-slow`, capped devices are moved to it. Otherwise Pingota falls back to
the router's default profile.

---

## 5. Create the first ISP account

The `ADMIN_*` account is the **platform owner**: it has no network attached, so `/admin/` returns 404
by design and `/django-admin/` shows signup requests.

Two ways to create an ISP:

**Self-service** — an ISP fills `/signup/`. The owner opens
`/django-admin/accounts/signuprequest/`, selects the request, and runs the "Approve" action. That
creates the ISP, its first owner login (the temporary password is shown once), and the portal at
`/portal/<slug>/`.

**Direct** — in `/django-admin/tenants/isp/`, add an ISP, then add a `Membership` pointing at any
existing user.

Passwords are changed by the operator at `/admin/password-change/`.

---

## 6. Payments

Nothing is charged until an ISP adds their own keys. Open **Settings** in the panel:

- **Paystack** — secret key, public key, optional subaccount code. Paste the webhook URL shown into
  the Paystack dashboard (Settings → API Keys & Webhooks).
- **M-Pesa** — shortcode, consumer key/secret, Lipa na M-Pesa passkey, plus the callback URL shown.

Both are stored encrypted and never rendered back. Full flow and testing instructions:
[docs/USAGE.md](USAGE.md#payments).

---

## 7. Backups and upgrades

```bash
# Mongo
docker compose exec mongo mongodump --db pingota --archive > pingota-$(date +%F).archive

# Restore
docker compose exec -T mongo mongorestore --archive < pingota-2026-11-08.archive
```

Upgrade: pull the new image, `docker compose up -d`. The entrypoint migrates automatically. Take a
dump first — migrations are forward-only.
