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

## 3. The router tunnel

Pingota uses TunGuard so it can add each router to the live tunnel as soon as an operator creates it.
Install TunGuard on the same VPS as Docker, following the [TunGuard installation guide](https://tunguard.github.io/docs/).
Its installer is also documented in the [TunGuard repository](https://github.com/TunGuard/tanguard-binary).

Configure TunGuard with:

```
WG_ADDRESS=10.70.0.1/16
WG_SUBNET=10.70.0.0/16
WG_LISTEN_PORT=13231
API_LISTEN=:9000
```

Allow UDP **13231** from the internet. Keep TunGuard's API on TCP **9000** private; the Pingota web
container reaches the host at `host.docker.internal:9000`. In TunGuard's dashboard, create an API key
with peer write access and set `TUNGARD_API_KEY` in Pingota's environment. Set:

```
ROUTER_TUNNEL_HOST=vpn.yourdomain.example   # public DNS name for this VPS
ROUTER_TUNNEL_PORT=13231
ROUTER_TUNNEL_SUBNET=10.70.0.0/16
TUNGARD_API_URL=http://host.docker.internal:9000
```

The Docker host must route `10.70.0.0/16` through TunGuard and allow the Pingota Docker network to
reach router peers over TCP **8728**. TunGuard assigns each router a unique address in this subnet;
the install script uses the same reserved address and keys that Pingota registered with TunGuard.

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
