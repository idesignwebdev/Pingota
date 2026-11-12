# Pingota

Hotspot billing and network control for MikroTik ISPs. An operator signs up, pastes one script into
their router, and starts selling internet access — hourly passes, subscriptions, or printed vouchers
paid by Paystack or M-Pesa into **their own** account.

The panel is multi-tenant: one deployment serves many ISPs, and every login sees only its own
routers, customers and money.

> [!WARNING]
> **Pre-release — secret handling is still under review.**
> Credential management is not final. Until it is, **do not enter real router passwords, live
> Paystack/M-Pesa keys, or any production secret** into a Pingota instance — use throwaway values
> on a test deployment. The storage format for these secrets may change before release, and dev-only
> defaults (`DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, the `ADMIN_PASSWORD` in `.env`) must be
> replaced before you expose the app to anyone.
>
> You may run the demo and explore the panel, but that is at your own risk. We will update this
> notice as soon as safely storing production credentials is supported.

---

## What it does

**For the ISP operator**

- Self-service router onboarding. Add a router, get a TunGuard tunnel peer, a restricted API user and a
  single block to paste into RouterOS. No port forwarding, no public IP, no WinBox back-and-forth.
- **Connected people** — live view of everyone online: device, pass, router, time left. Extend,
  revoke, or disconnect a session while keeping the pass.
- **Fair use policy** — per-pass data allowance. Cross it and the device is throttled to a slow
  profile (or cut off, your choice), enforced on the router.
- **Vouchers** — generate batches, print a cut-and-sell sheet, void a leak. Each code works once.
- **Packages** — hourly, daily or monthly, each with its own speed limit, device limit and allowance.
- **Monitoring** — router CPU, uptime, latency and online count sampled every minute; an event log
  of every connect, expiry, throttle and router error.

**For the customers**

A phone-first captive portal lets customers pick a package, pay, or enter a voucher. It uses server-rendered
HTML with a small amount of JavaScript. Time and speed are enforced on the router, so access ends even if
the server is down.

---

## How the money flows

| Provider | Where it settles | What Pingota does |
| --- | --- | --- |
| Paystack | The ISP's own account or subaccount | Creates the checkout, verifies the webhook, switches the router on |
| M-Pesa (Daraja STK) | The ISP's own shortcode/till | Sends the prompt with the ISP's keys, waits for the callback |

Pingota never holds customer funds. That keeps it software you host and charge for, not a payment
intermediary.

---

## Languages and stack

- **Python** — Django application and backend services.
- **HTML** — server-rendered pages and templates.
- **CSS** — responsive interface styling.
- **JavaScript** — small browser interactions and polling; no frontend framework or build step.
- **YAML and shell** — deployment configuration and container entrypoint.

- **Django 5.2** — server-rendered HTML, no SPA build step, no Node runtime in production.
- **MongoDB** by default (SQLite for a laptop, Postgres optional) via `django-mongodb-backend`.
- **Gunicorn + WhiteNoise** — one container, static files served from the same process.
- **MikroTik RouterOS API** — a purpose-built Python client.
- **TunGuard** — dynamically registers router peers over its API; Routers behind CGNAT need no port forwarding.

---

## Quick start

The router tunnel runs as TunGuard on the Docker host. Configure it and create an API key as described in
[installation step 3](docs/INSTALLATION.md#3-the-router-tunnel) before adding a router.

```bash
cp .env.example .env          # set app secrets and TunGuard API settings
docker compose up -d --build
```

Then open:

| Page | URL |
| --- | --- |
| Platform owner | `/admin/login/` (the `ADMIN_USERNAME` account) |
| ISP signup requests | `/django-admin/accounts/signuprequest/` |
| Operator panel | `/admin/` (an account with a network membership) |
| Capture page | `/portal/<isp-slug>/` |

---

## Try it with demo data

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Sign in at `/admin/login/` with `operator` / `changeme123`, capture page at `/portal/demo-hotspot/`.
The seed creates four passes, five customers, a week of payments, a connected router with an
install script, and two voucher batches.

---

## Tests

```bash
python manage.py test apps.billing -v 2
```

Covers single-settlement under duplicate callbacks, "paid but router down" handling, encrypted
credential round-trips, voucher single-use, FUP throttling exactly once, onboarding identity, and
tenant isolation between two ISPs.

---

## Repository layout

```
config/          settings, urls, wsgi, CSP middleware
core/            shared model base, crypto, sweeper (scheduler), management commands
apps/accounts/   operator membership, ISP signup requests
apps/tenants/    Isp, Router + onboarding state, payment credentials, platform keys
apps/network/    packages, customers, grants, vouchers, router events, metrics, MikroTik client
apps/billing/    payments, Paystack, M-Pesa, webhooks, settlement service
apps/web/        marketing pages, capture portal, operator panel, signup, onboarding views
templates/       server-rendered HTML
static/          one CSS file, one JS file
docs/            installation and usage guides
```



---

## Licence

MIT.

## Inspiration

Pingota was inspired by Frost-bit-star's [M-Pesa Based WiFi Billing System](https://github.com/Frost-bit-star/mpesa-based-wifi-billing).
