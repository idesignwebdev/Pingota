"""Smoke tests for the paths that cost money if they break.

They run on the default test database (SQLite unless MONGODB_URI is set), with
the router layer stubbed out.
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing import paystack
from apps.billing.models import Payment, WebhookEvent
from apps.billing.service import settle_payment
from apps.network.models import AccessGrant, Customer, Package
from apps.tenants.models import Isp, PaystackConfig, Router

#: A real Fernet key. The key validator rejects placeholder words such as
#: "test", so tests must supply something the production path would accept.
TEST_KEY = "ZMD6DO1R8ZmXwA-qxTiemkgDNI3ZqSWbaSyoZWCSe5Q="


class SettleOnceTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Test Net", slug="demo-hotspot")
        self.router = Router.objects.create(isp=self.isp, name="AP1", host="127.0.0.1")
        self.router.set_password("secret")
        self.router.save()
        self.package = Package.objects.create(
            isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        self.customer = Customer.objects.create(isp=self.isp, mac_address="A4:5E:60:1C:92:0B")
        self.payment = Payment.objects.create(
            isp=self.isp,
            package=self.package,
            customer=self.customer,
            reference="pg-test",
            amount=Decimal("10"),
            status="pending",
        )

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    def test_settlement_provisions_once_even_if_callback_repeats(self, add_user):
        settle_payment(self.payment, provider_reference="1")
        settle_payment(self.payment, provider_reference="1")
        self.assertEqual(add_user.call_count, 1)
        self.assertEqual(AccessGrant.objects.filter(isp=self.isp).count(), 1)

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    def test_grant_is_not_marked_live_when_router_fails(self, add_user):
        from apps.network import mikrotik

        add_user.side_effect = mikrotik.RouterUnreachable("timeout")
        settle_payment(self.payment, provider_reference="1")
        grant = AccessGrant.objects.get(isp=self.isp)
        self.assertEqual(grant.revoked_reason, "waiting for router")
        self.assertEqual(self.payment.status, "paid")  # money was taken; keep the record

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    def test_grant_expires_after_package_duration(self, add_user):
        settle_payment(self.payment, provider_reference="1")
        grant = AccessGrant.objects.get(isp=self.isp)
        self.assertLessEqual(abs((grant.expires_at - grant.started_at).total_seconds() - 3600), 2)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
class CryptoTests(TestCase):
    def test_router_password_roundtrip_is_encrypted(self):
        isp = Isp.objects.create(name="Net", slug="net")
        router = Router.objects.create(isp=isp, name="AP", host="10.0.0.1")
        router.set_password("hunter2")
        router.save()
        stored = Router.objects.get(pk=router.pk).api_password
        self.assertNotIn("hunter2", stored)
        self.assertEqual(Router.objects.get(pk=router.pk).get_password(), "hunter2")


class PortalTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Test Net", slug="demo-hotspot")
        Package.objects.create(isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60)

    def test_capture_page_shows_passes(self):
        resp = self.client.get("/portal/demo-hotspot/?mac=a4-5e-60-1c-92-0b")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "1 hour")

    def test_checkout_without_mac_is_refused(self):
        package = self.isp.packages.first()
        resp = self.client.post(f"/portal/demo-hotspot/checkout/{package.id}/", {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Payment.objects.count(), 0)

    def test_unknown_isp_returns_404(self):
        self.assertEqual(self.client.get("/portal/nope/").status_code, 404)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
class OnboardingTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")

    @mock.patch("apps.network.tunguard.server_public_key", return_value="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    def test_new_router_gets_identity_and_script(self, _server_key):
        from apps.network import onboarding

        router = Router(isp=self.isp, name="AP1")
        onboarding.provision_identity(router)
        router.save()

        self.assertTrue(router.install_token)
        self.assertTrue(router.tunnel_address.startswith("10.70."))
        self.assertNotIn("private", router.tunnel_private_key)  # stored encrypted, not raw

        script = onboarding.install_script(router)
        self.assertIn("/interface/wireguard/add", script)
        self.assertIn(f'private-key="{router.get_tunnel_private_key()}"', script)
        self.assertIn('allowed-address="10.70.0.1/32"', script)
        self.assertIn(router.install_token, script)
        self.assertIn(router.api_user, script)
        # The script is a one-time secret bundle and must be treated like a password.
        self.assertIn(router.get_password(), script)

    def test_tunnel_addresses_do_not_collide(self):
        from apps.network import onboarding

        addresses = []
        for index in range(3):
            router = Router(isp=self.isp, name=f"AP{index}")
            onboarding.provision_identity(router)
            router.save()
            addresses.append(router.tunnel_address)
        self.assertEqual(len(set(addresses)), 3)

    def test_regeneration_keeps_router_api_username_stable(self):
        from apps.network import onboarding

        router = Router(isp=self.isp, name="AP1")
        onboarding.provision_identity(router)
        original_user = router.api_user
        original_address = router.tunnel_address
        router.save()

        onboarding.provision_identity(router)

        self.assertEqual(router.api_user, original_user)
        self.assertNotEqual(router.tunnel_address, original_address)

    @mock.patch("apps.web.onboarding_views.mikrotik.health")
    def test_report_marks_router_connected(self, _router_health):
        from apps.network import onboarding

        router = Router(isp=self.isp, name="AP1")
        onboarding.provision_identity(router)
        router.save()
        resp = self.client.get(f"/routers/{router.install_token}/report/?version=7.15&board=hEX")
        self.assertEqual(resp.status_code, 200)
        router.refresh_from_db()
        self.assertEqual(router.status, "connected")
        self.assertEqual(router.routeros_version, "7.15")

    def test_unknown_token_is_rejected(self):
        self.assertEqual(self.client.get("/routers/not-a-real-token/report/").status_code, 404)


class FupTests(TestCase):
    @mock.patch("apps.network.mikrotik.apply_profile")
    @mock.patch("apps.network.mikrotik.usage_by_user")
    def test_cap_throttles_once(self, usage, apply_profile):
        from apps.network.fup import enforce

        isp = Isp.objects.create(name="Net", slug="net")
        router = Router.objects.create(isp=isp, name="AP")
        package = Package.objects.create(
            isp=isp,
            name="4 hours",
            price=Decimal("15"),
            duration_minutes=240,
            data_cap_mb=100,
            fup_action="throttle",
        )
        customer = Customer.objects.create(isp=isp, mac_address="A4:5E:60:1C:92:0B")
        grant = AccessGrant.objects.create(
            isp=isp,
            customer=customer,
            package=package,
            router=router,
            started_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(hours=3),
        )

        # 60 MB, then another 60 MB: the cap is crossed on the second sync.
        usage.return_value = {"pg-a45e601c920b": 60 * 1024 * 1024}
        enforce([(router, [grant])])
        grant.refresh_from_db()
        self.assertEqual(grant.fup_state, "ok")

        usage.return_value = {"pg-a45e601c920b": 120 * 1024 * 1024}
        enforce([(router, [grant])])
        grant.refresh_from_db()
        self.assertEqual(grant.fup_state, "throttled")
        self.assertEqual(apply_profile.call_count, 1)

        # A third tick must not throttle again.
        enforce([(router, [grant])])
        self.assertEqual(apply_profile.call_count, 1)


class VoucherTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.package = Package.objects.create(isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60)

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    def test_redeem_once_only(self, add_user):
        from apps.network.vouchers import VoucherError, generate, redeem

        code = generate(self.isp, self.package, 1)[0]
        redeem(self.isp, code.code, "A4:5E:60:1C:92:0B")
        with self.assertRaises(VoucherError):
            redeem(self.isp, code.code, "7C:2F:80:44:19:AE")
        self.assertEqual(AccessGrant.objects.filter(isp=self.isp).count(), 1)

    def test_code_is_case_and_dash_insensitive(self):
        from apps.network.vouchers import generate, normalise

        code = generate(self.isp, self.package, 1)[0]
        messy = code.code.replace("-", "").lower()
        self.assertEqual(normalise(messy), code.code)

    def test_unknown_code_is_refused(self):
        from apps.network.vouchers import VoucherError, redeem

        with self.assertRaises(VoucherError):
            redeem(self.isp, "ZZZZ-ZZZZ", "A4:5E:60:1C:92:0B")


class TenantIsolationTests(TestCase):
    def test_operator_cannot_see_another_isp(self):
        from django.contrib.auth import get_user_model

        from apps.accounts.models import Membership

        mine = Isp.objects.create(name="Mine", slug="mine")
        other = Isp.objects.create(name="Other", slug="other")
        Membership.objects.create(user=get_user_model().objects.create_user("ops", password="x"), isp=mine)

        package = Package.objects.create(isp=other, name="Theirs", price=Decimal("99"), duration_minutes=60)
        self.client.login(username="ops", password="x")

        resp = self.client.get("/admin/packages/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Theirs")
        self.assertEqual(self.client.get(f"/admin/packages/{package.id}/").status_code, 404)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, DEBUG=False)
class KeyHardeningTests(TestCase):
    """The encryption key is the root of every other control."""

    def test_short_passphrase_is_rejected(self):
        from core.crypto import SecretsUnavailable, validate_key

        with self.assertRaises(SecretsUnavailable):
            validate_key("short")

    def test_placeholder_is_rejected_outside_debug(self):
        from core.crypto import SecretsUnavailable, validate_key

        with self.assertRaises(SecretsUnavailable):
            validate_key("change-me-please-this-is-not-a-real-key-1234")

    def test_low_variety_passphrase_is_rejected(self):
        from core.crypto import SecretsUnavailable, validate_key

        with self.assertRaises(SecretsUnavailable):
            validate_key("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def test_real_fernet_key_is_accepted(self):
        from cryptography.fernet import Fernet

        from core.crypto import validate_key

        validate_key(Fernet.generate_key().decode())  # must not raise

    def test_missing_key_is_rejected(self):
        from core.crypto import SecretsUnavailable, validate_key

        with self.assertRaises(SecretsUnavailable):
            validate_key("")


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
class SecretExposureTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        from apps.accounts.models import Membership
        from apps.tenants.models import MpesaConfig, PaystackConfig

        self.isp = Isp.objects.create(name="Net", slug="net")
        self.user = get_user_model().objects.create_user("ops", password="x")
        Membership.objects.create(user=self.user, isp=self.isp, role="owner")

        paystack = PaystackConfig.objects.create(isp=self.isp, enabled=True, public_key="pk_live_abc")
        paystack.set_secret("sk_live_SUPERSECRET")
        paystack.set_webhook_secret("whsec_SUPERSECRET")
        paystack.save()

        mpesa = MpesaConfig.objects.create(isp=self.isp, enabled=True)
        mpesa.set_shortcode("174379")
        mpesa.set_consumer_key("KEYVALUE123")
        mpesa.set_secret("SECRETVALUE456")
        mpesa.set_passkey("PASSKEY789")
        mpesa.save()

    def test_daraja_fields_are_encrypted_at_rest(self):
        from apps.tenants.models import MpesaConfig

        cfg = MpesaConfig.objects.get(isp=self.isp)
        for plain in ("174379", "KEYVALUE123", "SECRETVALUE456", "PASSKEY789"):
            self.assertNotIn(plain, cfg.shortcode + cfg.consumer_key + cfg.consumer_secret + cfg.passkey)
        self.assertEqual(cfg.get_shortcode(), "174379")
        self.assertEqual(cfg.get_consumer_key(), "KEYVALUE123")

    def test_settings_page_never_echoes_a_secret(self):
        self.client.login(username="ops", password="x")
        body = self.client.get("/admin/settings/").content.decode()
        for secret in (
            "sk_live_SUPERSECRET",
            "whsec_SUPERSECRET",
            "SECRETVALUE456",
            "PASSKEY789",
            "KEYVALUE123",
            "174379",
        ):
            self.assertNotIn(secret, body, f"{secret} leaked into the settings page")

    def test_revenue_page_never_echoes_ciphertext(self):
        self.client.login(username="ops", password="x")
        body = self.client.get("/admin/revenue/").content.decode()
        self.assertNotIn("gAAAAA", body)  # Fernet prefix
        self.assertIn("174379", body)  # the till is shown, decrypted, for the operator

    def test_settings_actions_are_scoped_to_own_isp(self):
        """An operator cannot post a router change onto another ISP's router."""
        from apps.accounts.models import Membership
        from django.contrib.auth import get_user_model

        other = Isp.objects.create(name="Other", slug="other")
        foreign_router = Router.objects.create(isp=other, name="Theirs")
        intruder = get_user_model().objects.create_user("stranger", password="y")
        Membership.objects.create(user=intruder, isp=self.isp, role="owner")

        self.client.login(username="stranger", password="y")
        resp = self.client.post(
            "/admin/settings/",
            {"section": "router", "router_id": foreign_router.id, "router_name": "Hijacked"},
        )
        self.assertEqual(resp.status_code, 404)
        foreign_router.refresh_from_db()
        self.assertEqual(foreign_router.name, "Theirs")


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
class RotationTests(TestCase):
    def test_rotation_moves_every_value_to_the_new_key(self):
        from cryptography.fernet import Fernet
        from django.core.management import call_command

        from core.crypto import decrypt_with

        old_key = TEST_KEY  # active during this test
        new_key = Fernet.generate_key().decode()

        isp = Isp.objects.create(name="N", slug="n")
        router = Router.objects.create(isp=isp, name="AP")
        router.set_password("routerpass")
        router.save()

        paystack = PaystackConfig.objects.create(isp=isp)
        paystack.set_secret("sk_test_rotate")
        paystack.set_webhook_secret("whsec_rotate")
        paystack.save()

        call_command("rotate_key", f"--old-key={old_key}", f"--new-key={new_key}", verbosity=0)

        router.refresh_from_db()
        self.assertEqual(decrypt_with(router.api_password, new_key), "routerpass")
        self.assertEqual(decrypt_with(router.api_password, old_key), "")
        paystack.refresh_from_db()
        self.assertEqual(decrypt_with(paystack.secret_key, new_key), "sk_test_rotate")
        self.assertEqual(decrypt_with(paystack.webhook_secret, new_key), "whsec_rotate")

    def test_rotation_aborts_when_old_key_cannot_open_a_value(self):
        from cryptography.fernet import Fernet
        from django.core.management import CommandError, call_command

        router = Router.objects.create(isp=Isp.objects.create(name="N", slug="n"), name="AP")
        router.set_password("routerpass")
        router.save()

        with self.assertRaises(CommandError):
            call_command(
                "rotate_key",
                old_key="the-wrong-key-entirely",
                new_key=Fernet.generate_key().decode(),
                verbosity=0,
            )


class PaystackCase(TestCase):
    """Shared fixture: one ISP, one working router, one pending payment."""

    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.router = Router.objects.create(isp=self.isp, name="AP", host="127.0.0.1")
        self.router.set_password("secret")
        self.router.save()
        self.package = Package.objects.create(
            isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        self.customer = Customer.objects.create(
            isp=self.isp, mac_address="A4:5E:60:1C:92:0B"
        )
        self.payment = Payment.objects.create(
            isp=self.isp,
            package=self.package,
            customer=self.customer,
            reference="pg-webhook",
            amount=Decimal("10"),
            currency="KES",
            status="pending",
        )
        cfg = PaystackConfig.objects.create(isp=self.isp, enabled=True)
        cfg.set_secret("sk_test_account")
        cfg.set_webhook_secret("whsec_test_account")
        cfg.save()


@override_settings(
    FIELD_ENCRYPTION_KEY=TEST_KEY,
    PAYSTACK_SECRET_KEY="",
    PAYSTACK_WEBHOOK_SECRET="",
)
class PaystackWebhookTests(PaystackCase):
    def payload(self, **data_overrides):
        data = {
            "id": 4099,
            "reference": self.payment.reference,
            "status": "success",
            "amount": 1000,
            "currency": "KES",
            "metadata": {"isp": "net"},
        }
        data.update(data_overrides)
        return {"event": "charge.success", "data": data}

    def verified(self, **overrides):
        data = {
            "id": 4099,
            "reference": self.payment.reference,
            "status": "success",
            "amount": 1000,
            "currency": "KES",
        }
        data.update(overrides)
        return data

    def new_signature(self, raw: bytes, *, timestamp=None) -> str:
        timestamp = int(time.time()) if timestamp is None else timestamp
        digest = hmac.new(
            b"whsec_test_account",
            str(timestamp).encode() + b"." + raw,
            hashlib.sha512,
        ).hexdigest()
        return f"t={timestamp},v1=whk_test:{digest}"

    def post_webhook(self, payload, *, signature=None, path=None, header="paystack-signature"):
        raw = json.dumps(payload).encode()
        signature = self.new_signature(raw) if signature is None else signature
        return self.client.post(
            path or f"/payments/webhook/paystack/{self.isp.slug}/",
            data=raw,
            content_type="application/json",
            headers={header: signature},
        )

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_current_signature_scheme_settles_the_payment(self, verify, add_user):
        verify.return_value = self.verified()

        response = self.post_webhook(self.payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paid")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "paid")
        self.assertEqual(WebhookEvent.objects.filter(provider="paystack").count(), 1)

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_legacy_signature_scheme_still_settles(self, verify, add_user):
        verify.return_value = self.verified()
        raw = json.dumps(self.payload()).encode()
        signature = hmac.new(b"sk_test_account", raw, hashlib.sha512).hexdigest()

        response = self.client.post(
            f"/payments/webhook/paystack/{self.isp.slug}/",
            data=raw,
            content_type="application/json",
            headers={"x-paystack-signature": signature},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paid")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "paid")

    def test_bad_signature_is_rejected_and_not_recorded(self):
        response = self.post_webhook(
            self.payload(), signature="t=1,v1=whk_test:" + "0" * 128
        )
        self.assertEqual(response.status_code, 401)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_stale_signature_is_rejected(self):
        raw = json.dumps(self.payload()).encode()
        signature = self.new_signature(raw, timestamp=int(time.time()) - 3600)
        response = self.post_webhook(self.payload(), signature=signature)
        self.assertEqual(response.status_code, 401)

    @mock.patch("apps.billing.views.settle_payment")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_amount_mismatch_does_not_settle(self, verify, settle):
        verify.return_value = self.verified(amount=100)

        response = self.post_webhook(self.payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "mismatch")
        settle.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")

    @mock.patch("apps.billing.views.settle_payment")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_currency_mismatch_does_not_settle(self, verify, settle):
        verify.return_value = self.verified(currency="NGN")

        response = self.post_webhook(self.payload())

        self.assertEqual(response.json()["status"], "mismatch")
        settle.assert_not_called()

    @mock.patch("apps.billing.views.paystack.verify")
    def test_failed_verification_marks_the_payment_failed(self, verify):
        verify.return_value = self.verified(status="failed", gateway_response="Declined")

        response = self.post_webhook(self.payload())

        self.assertEqual(response.json()["status"], "failed")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "failed")
        self.assertEqual(self.payment.failure_reason, "Declined")

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_shared_endpoint_reads_string_metadata(self, verify, add_user):
        verify.return_value = self.verified()
        payload = self.payload(metadata=json.dumps({"isp": "net"}))

        response = self.post_webhook(payload, path="/payments/webhook/paystack/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paid")

    def test_invalid_json_is_rejected(self):
        response = self.client.post(
            f"/payments/webhook/paystack/{self.isp.slug}/",
            data=b"{not json",
            content_type="application/json",
            headers={"paystack-signature": "t=1,v1=whk_test:" + "0" * 128},
        )
        self.assertEqual(response.status_code, 400)


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
class PaystackCallbackTests(PaystackCase):
    def success(self, **overrides):
        data = {
            "id": 4099,
            "reference": self.payment.reference,
            "status": "success",
            "amount": 1000,
            "currency": "KES",
        }
        data.update(overrides)
        return data

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_callback_verifies_and_settles_before_the_webhook(self, verify, add_user):
        verify.return_value = self.success()

        response = self.client.get(
            f"/payments/callback/?reference={self.payment.reference}"
        )

        self.assertRedirects(
            response,
            f"/portal/{self.isp.slug}/status/{self.payment.reference}/",
            fetch_redirect_response=False,
        )
        verify.assert_called_once()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "paid")

    @mock.patch("apps.billing.views.settle_payment")
    @mock.patch("apps.billing.views.paystack.verify")
    def test_callback_amount_mismatch_leaves_it_pending(self, verify, settle):
        verify.return_value = self.success(amount=100)

        response = self.client.get(
            f"/payments/callback/?reference={self.payment.reference}"
        )

        self.assertEqual(response.status_code, 302)
        settle.assert_not_called()
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")

    @mock.patch("apps.billing.views.paystack.verify")
    def test_callback_verify_error_still_returns_to_the_status_page(self, verify):
        verify.side_effect = paystack.PaystackError("Paystack is unreachable")

        response = self.client.get(
            f"/payments/callback/?reference={self.payment.reference}"
        )

        self.assertRedirects(
            response,
            f"/portal/{self.isp.slug}/status/{self.payment.reference}/",
            fetch_redirect_response=False,
        )

    def test_unknown_reference_uses_the_branded_error(self):
        response = self.client.get("/payments/callback/?reference=does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "We could not find that payment", status_code=404)


@override_settings(
    FIELD_ENCRYPTION_KEY=TEST_KEY,
    PAYSTACK_SECRET_KEY="sk_platform",
    PAYSTACK_BASE_URL="https://api.paystack.co",
)
class PaystackInitializeTests(PaystackCase):
    def initialize(self):
        with mock.patch("apps.billing.paystack.httpx.post") as post:
            post.return_value.status_code = 200
            post.return_value.content = b"{}"
            post.return_value.json.return_value = {
                "status": True,
                "data": {"authorization_url": "https://checkout.paystack.com/test"},
            }
            paystack.initialize(
                self.payment,
                email="guest@example.com",
                callback_url="https://pingota.example/payments/callback/",
            )
        return post.call_args.kwargs

    def test_own_secret_key_does_not_send_a_foreign_subaccount(self):
        cfg = PaystackConfig.objects.get(isp=self.isp)
        cfg.subaccount_code = "ACCT_other_account"
        cfg.save(update_fields=["subaccount_code"])

        request = self.initialize()

        self.assertNotIn("subaccount", request["json"])
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk_test_account")

    def test_platform_key_can_settle_to_a_subaccount(self):
        cfg = PaystackConfig.objects.get(isp=self.isp)
        cfg.secret_key = ""
        cfg.subaccount_code = "ACCT_platform"
        cfg.save(update_fields=["secret_key", "subaccount_code"])
        cfg.refresh_from_db()
        self.assertEqual(cfg.get_secret(), "")
        self.assertEqual(cfg.subaccount_code, "ACCT_platform")
        # setUp cached this reverse relation when it created the config.
        self.isp.refresh_from_db()

        request = self.initialize()

        self.assertEqual(request["json"]["subaccount"], "ACCT_platform")
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk_platform")


class PaystackMatchingTests(TestCase):
    def test_amount_and_currency_must_match(self):
        isp = Isp.objects.create(name="Net", slug="net")
        package = Package.objects.create(
            isp=isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        payment = Payment.objects.create(
            isp=isp,
            package=package,
            reference="pg-match",
            amount=Decimal("10"),
            currency="KES",
        )
        self.assertTrue(
            paystack.matches_payment(
                payment,
                {
                    "reference": "pg-match",
                    "amount": 1000,
                    "currency": "KES",
                    "status": "success",
                },
            )
        )
        self.assertFalse(
            paystack.matches_payment(
                payment,
                {
                    "reference": "pg-match",
                    "amount": 999,
                    "currency": "KES",
                    "status": "success",
                },
            )
        )
        self.assertFalse(
            paystack.matches_payment(
                payment,
                {
                    "reference": "pg-match",
                    "amount": 1000,
                    "currency": "NGN",
                    "status": "success",
                },
            )
        )
