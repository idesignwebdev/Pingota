# Running a network

Written for the person behind the panel, not the person who deployed it.

---

## The daily view

`/admin/` opens on one number: **collected today**. Under it, how many checkouts settled, which
passes sold, and routers that are not answering. If a router shows *Unreachable* in the corner, fix
that before anything else — payments will still be taken, but access will queue.

The **event log** on the same page is the first place to look when a customer says "I paid and it did
not work". Each line says what happened and when.

---

## Connected people

`/admin/live/` lists every device holding a valid pass, nearest expiry first.

| Action | What it does | When to use it |
| --- | --- | --- |
| **Extend** | Adds the minutes you type to the router timer and the record | A customer paid cash, or was throttled unfairly |
| **Revoke** | Removes the hotspot user and closes the pass | Refund, abuse, or a shared code |
| **Disconnect** | Drops the live session but keeps the pass | Kick a device so it reconnects with a new profile |

Below the list are **sessions on the router** — read live from RouterOS. A device can hold a valid
pass and still be offline; this is where you see the difference.

---

## Passes

`/admin/packages/` is what the capture page sells. Each pass has:

- **Billing type** — *hourly pass* (prepaid, one purchase) or *subscription* (a recurring period).
- **Price and duration** — duration is what the router counts down.
- **Speed** — download/upload in kbps. Zero means "use the router profile".
- **Fair use allowance** — see below.

⚠ A pass shorter than the content customers actually download will feel broken even when it works.
On a 5 Mbps link a 700 MB update takes about 18 minutes.

### Fair use policy (FUP)

Set **Data included (MB)** to cap a pass. Usage is read from the router's byte counters every 30
seconds and added up across reconnects.

When the cap is crossed, the package decides what happens:

- **Slow the connection down** — the device is moved to the throttled profile (default 256 kbps
  down / 128 kbps up, or a named profile if you set one). The customer stays online. This is what most
  Kenyan ISPs advertise as FUP.
- **End the pass** — the hotspot user is removed and the pass closes; the guest must buy again.

Throttling happens once per pass: the grant is marked `throttled` and not touched again. The event log
records it as *Throttled by allowance*. To reset a device after a false throttle, set its allowance
back in the live view or start a new package.

---

## Vouchers

For counter sales and resellers who do not want to pay by phone.

1. **Vouchers → Generate codes.** Pick the pass, how many, and a batch label such as
   `Market gate · November`. Optionally set an expiry date for *unredeemed* codes.
2. **Print sheet** produces a cut-and-sell page, four tickets per row, each showing the code, the
   pass length and the network name.
3. The guest enters the code on the capture page under **“I have a voucher code”**.

Rules enforced by the software:

- Each code works **once**, on the first device that redeems it — two people typing the same code at
  the same second cannot both get online.
- Codes are case-insensitive and the dash is optional (`k7qm3xpd` = `K7QM-3XPD`).
- Letters that look alike are not used at all (no `0/O`, `1/I/L`), so a cashier reading one out cannot
  create a support call.
- **Void** cancels an unused code. A redeemed code cannot be voided — revoke the pass instead.

---

## Customers

`/admin/customers/` is every device that has bought at least once, keyed by MAC: phone number, current
pass, first seen, total paid. Search by partial MAC or the last digits of a phone.

Phones rotate their MAC address for privacy on some networks. If a customer "lost" their pass after
walking away, that is usually why: the new MAC gets a fresh record. Turning off "randomised MAC" for
that SSID on the phone fixes it.

---

## Payments

### Paystack

1. Panel → **Settings → Paystack**: secret key, public key, and a subaccount code if you want
   settlements split to a specific bank account.
2. Copy the webhook URL from the same page into Paystack → Settings → API Keys & Webhooks.
3. Test with `sk_test_…` keys first, then swap to live keys.

Each payment is re-verified against Paystack's API before access is granted — a webhook alone is not
treated as proof. Duplicate deliveries are safe: only the first flips the payment to paid.

### M-Pesa (Daraja)

1. Panel → **Settings → M-Pesa**: shortcode, consumer key, consumer secret, Lipa na M-Pesa passkey.
2. Register the callback URL shown on the same page in the Daraja portal.
3. Customers choose **M-Pesa** at checkout, type their number, and approve the prompt on their phone.

Money lands in the ISP's own till. The STK push is sent with the ISP's own credentials, so the platform
never sits in the payment path.

### When a payment succeeds but the internet does not

The pass is recorded as *waiting for router* and the sweep retries every 30 seconds. If the router
comes back, access appears on its own. If it does not, the payment is still in `/admin/revenue/` —
grant access manually from that reference, or extend the device in the live view once the router is up.

---

## Network and monitoring

`/admin/network/` shows each router: online users, CPU, probe latency, and a strip of recent samples.
A red segment means the probe failed at that minute.

- **Test connection** dials the router now.
- **Install script** reopens the onboarding page. Use **Regenerate credentials** only if the script
  leaked or the router was replaced — the router goes offline until the new script is pasted.

Samples are kept as one row per router per minute in the same database, so there is no Prometheus to
run and nothing to keep alive. Prune the `RouterMetric` collection from `django-admin` if the volume
grows.

---

## Accounts

| Role | Can reach |
| --- | --- |
| Platform owner (`ADMIN_USERNAME`) | `/django-admin/` only — approves signups, sees all tenants |
| ISP owner | `/admin/`, every section for their own network |
| ISP staff | Same panel; the role field is recorded for audit |

There is deliberately **no** way to reach another ISP's data from the panel: the ISP comes from the
signed-in user's membership, never from a URL.

Change a password at `/admin/password-change/`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Capture page shows "Device not recognised" | Page opened outside the hotspot | Connect to the SSID; the hotspot must redirect with `?mac=$(mac)` |
| "Waiting for install" never clears | Router could not reach Pingota's RouterOS API through the tunnel | Check TunGuard shows a recent peer handshake, then verify the server can reach the router IP on the tunnel |
| Payments settle, access queues | API user password mismatch or tunnel down | Router page → **Test connection**; regenerate if credentials were changed by hand |
| Everyone drops at the same time | Router rebooted; hotspot users live in RAM unless saved | `/ip hotspot user` entries are volatile on some configs — Pingota recreates them on the next timestamp change |
| Throttled too early | Router counters were reset and read as fresh | Reset the allowance; counters are re-read on the next tick |

For anything else, the event log on the overview page usually names the router and the error.
