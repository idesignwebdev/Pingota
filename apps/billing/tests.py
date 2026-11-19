"""Smoke tests for the paths that cost money if they break.

They run on the default test database (SQLite unless MONGODB_URI is set), with
the router layer stubbed out.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Membership
from apps.billing import paystack
from apps.billing.models import Payment, PlanPayment, WebhookEvent
from apps.billing.service import settle_payment, settle_plan_payment
from apps.network.models import AccessGrant, Customer, Package
from apps.network.provisioning import (
    ProvisioningDeferred,
    activate_grant,
    revoke_suspended_grants,
)
from apps.tenants.models import Isp, PaystackConfig, Router, SubscriptionPlan

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
    def test_queued_grant_survives_the_sweeper_and_is_retried(self, add_user):
        from apps.network import mikrotik
        from apps.network.provisioning import expire_due_grants, retry_pending_grants

        add_user.side_effect = mikrotik.RouterUnreachable("timeout")
        settle_payment(self.payment, provider_reference="1")
        grant = AccessGrant.objects.get(isp=self.isp)
        self.assertEqual(grant.revoked_reason, "waiting for router")
        # The provisional clock must run from the pass, not from now.
        self.assertGreater(grant.expires_at, timezone.now())

        self.assertEqual(expire_due_grants(), 0)
        grant.refresh_from_db()
        self.assertEqual(grant.state, "active")

        add_user.side_effect = None
        self.assertEqual(retry_pending_grants(), 1)
        grant.refresh_from_db()
        self.assertEqual(grant.revoked_reason, "")
        self.assertGreater(grant.expires_at, timezone.now())

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


class HotspotHandoffTests(TestCase):
    """The router-derived login page and the post-payment sign-in hand-off."""

    def setUp(self):
        self.isp = Isp.objects.create(name="Test Net", slug="demo-hotspot")
        self.package = Package.objects.create(
            isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        self.customer = Customer.objects.create(
            isp=self.isp, mac_address="A4:5E:60:1C:92:0B"
        )

    def test_login_page_carries_the_portal_redirect(self):
        response = self.client.get("/portal/demo-hotspot/hotspot/login.html")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/portal/demo-hotspot/")
        self.assertContains(response, "mac=$(mac)")
        self.assertContains(response, "link-login-only=$(link-login-only)")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_status_hands_back_a_router_login_once_the_pass_is_live(self):
        payment = Payment.objects.create(
            isp=self.isp,
            package=self.package,
            customer=self.customer,
            reference="pg-handoff",
            amount=Decimal("10"),
            status="paid",
        )
        AccessGrant.objects.create(
            isp=self.isp,
            customer=self.customer,
            package=self.package,
            started_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(minutes=60),
        )
        self.client.get(
            "/portal/demo-hotspot/?mac=A4:5E:60:1C:92:0B"
            "&link-login-only=http://192.168.88.1/login"
            "&link-orig=http://example.com/news"
        )

        state = self.client.get(
            f"/portal/demo-hotspot/status/{payment.reference}/state/"
        ).json()

        self.assertTrue(state["connect_url"].startswith("http://192.168.88.1/login?"))
        self.assertIn("username=pg-a45e601c920b", state["connect_url"])
        self.assertIn("password=A45E601C920B", state["connect_url"])
        self.assertIn("dst=http%3A%2F%2Fexample.com%2Fnews", state["connect_url"])

    def test_reopening_the_portal_with_a_pass_signs_the_device_in(self):
        AccessGrant.objects.create(
            isp=self.isp,
            customer=self.customer,
            package=self.package,
            started_at=timezone.now(),
            expires_at=timezone.now() + timezone.timedelta(minutes=60),
        )
        response = self.client.get(
            "/portal/demo-hotspot/?mac=A4:5E:60:1C:92:0B"
            "&link-login-only=http://192.168.88.1/login"
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("http://192.168.88.1/login?"))

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_platform")
    def test_returning_device_finds_its_phone_filled_in(self):
        self.customer.phone = "0712345678"
        self.customer.save(update_fields=["phone"])

        response = self.client.get("/portal/demo-hotspot/?mac=A4:5E:60:1C:92:0B")

        self.assertContains(response, 'value="0712345678"')

    def test_status_has_no_login_url_before_the_pass_exists(self):
        payment = Payment.objects.create(
            isp=self.isp,
            package=self.package,
            customer=self.customer,
            reference="pg-waiting",
            amount=Decimal("10"),
            status="pending",
        )
        self.client.get(
            "/portal/demo-hotspot/?mac=A4:5E:60:1C:92:0B"
            "&link-login-only=http://192.168.88.1/login"
        )

        state = self.client.get(
            f"/portal/demo-hotspot/status/{payment.reference}/state/"
        ).json()

        self.assertEqual(state["connect_url"], "")


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, PAYSTACK_SECRET_KEY="")
class CheckoutRailTests(TestCase):
    """Only offer payment methods the ISP can actually complete."""

    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.package = Package.objects.create(
            isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        self.mac = "A4:5E:60:1C:92:0B"

    def enable_mpesa(self):
        from apps.tenants.models import MpesaConfig

        cfg = MpesaConfig.objects.create(isp=self.isp, enabled=True)
        cfg.set_shortcode("174379")
        cfg.set_consumer_key("consumer-key")
        cfg.set_secret("consumer-secret")
        cfg.set_passkey("passkey")
        cfg.save()
        return cfg

    def test_page_hides_the_form_when_no_rail_is_configured(self):
        response = self.client.get(f"/portal/net/?mac={self.mac}")
        self.assertContains(response, "No payment method yet")
        self.assertNotContains(response, 'value="mpesa"')
        self.assertNotContains(response, 'value="paystack"')

    def test_checkout_refuses_an_unconfigured_rail(self):
        response = self.client.post(
            f"/portal/net/checkout/{self.package.id}/",
            {"provider": "mpesa", "phone": "0712345678", "mac": self.mac},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "not switched on", status_code=400)
        self.assertEqual(Payment.objects.count(), 0)

    def test_configured_mpesa_is_offered(self):
        self.enable_mpesa()
        response = self.client.get(f"/portal/net/?mac={self.mac}")
        self.assertContains(response, 'value="mpesa"')
        self.assertNotContains(response, "No payment method yet")

    def test_mpesa_needs_every_credential(self):
        from apps.billing import mpesa

        cfg = self.enable_mpesa()
        isp = Isp.objects.get(pk=self.isp.pk)
        self.assertTrue(mpesa.configured_for(isp))

        cfg.passkey = ""
        cfg.save(update_fields=["passkey"])
        isp = Isp.objects.get(pk=self.isp.pk)
        self.assertFalse(mpesa.configured_for(isp))


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

    def test_report_is_refused_when_the_trial_has_ended(self):
        from apps.network import onboarding

        self.isp.plan = "trial"
        self.isp.trial_ends_at = timezone.now() - timedelta(minutes=5)
        self.isp.save(update_fields=["plan", "trial_ends_at"])
        router = Router(isp=self.isp, name="AP1")
        onboarding.provision_identity(router)
        router.save()

        response = self.client.get(f"/routers/{router.install_token}/report/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "network paused")


class MpesaCallbackTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        Router.objects.create(isp=self.isp, name="AP", host="127.0.0.1")
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
            provider="mpesa",
            reference="pg-mpesa",
            provider_reference="ws_CO_123",
            amount=Decimal("10"),
            phone="254712345678",
        )

    def callback(self, amount):
        return self.client.post(
            "/payments/mpesa/callback/net/",
            data=json.dumps(
                {
                    "Body": {
                        "stkCallback": {
                            "ResultCode": 0,
                            "ResultDesc": "The service request is processed successfully.",
                            "CheckoutRequestID": "ws_CO_123",
                            "CallbackMetadata": {
                                "Item": [
                                    {"Name": "Amount", "Value": amount},
                                    {"Name": "MpesaReceiptNumber", "Value": "RCPT123"},
                                    {"Name": "PhoneNumber", "Value": 254712345678},
                                ]
                            },
                        }
                    }
                }
            ),
            content_type="application/json",
        )

    def test_amount_mismatch_does_not_provision(self):
        response = self.callback(1)
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")
        self.assertEqual(AccessGrant.objects.filter(isp=self.isp).count(), 0)

    @mock.patch("apps.network.mikrotik.add_hotspot_user")
    def test_matching_amount_settles(self, add_user):
        response = self.callback(10)
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "paid")
        self.assertEqual(AccessGrant.objects.filter(isp=self.isp).count(), 1)


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


@override_settings(
    PAYSTACK_SECRET_KEY="sk_platform",
    PAYSTACK_WEBHOOK_SECRET="whsec_platform",
    MPESA_PLATFORM_SHORTCODE="174379",
    MPESA_PLATFORM_CONSUMER_KEY="platform-key",
    MPESA_PLATFORM_CONSUMER_SECRET="platform-secret",
    MPESA_PLATFORM_PASSKEY="platform-passkey",
)
class PlanCheckoutTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(
            name="Net",
            slug="net",
            plan="trial",
            trial_ends_at=timezone.now() - timedelta(hours=1),
        )
        self.user = get_user_model().objects.create_user(
            username="ops", password="operator-pass-123", email="ops@example.com"
        )
        Membership.objects.create(user=self.user, isp=self.isp, role="owner")
        self.plan = SubscriptionPlan.objects.create(
            code="growth",
            name="Growth",
            monthly_price=Decimal("1000"),
            interval="monthly",
            max_routers=3,
        )

    def create_payment(self, **overrides):
        data = {
            "isp": self.isp,
            "plan": self.plan,
            "provider": "paystack",
            "reference": "plan-test",
            "amount": Decimal("1000"),
            "currency": "KES",
        }
        data.update(overrides)
        return PlanPayment.objects.create(**data)

    def platform_signature(self, raw: bytes) -> str:
        timestamp = int(time.time())
        digest = hmac.new(
            b"whsec_platform",
            str(timestamp).encode() + b"." + raw,
            hashlib.sha512,
        ).hexdigest()
        return f"t={timestamp},v1=whk_platform:{digest}"

    def post_plan_webhook(self, payload):
        raw = json.dumps(payload).encode()
        return self.client.post(
            "/payments/plan/webhook/paystack/",
            data=raw,
            content_type="application/json",
            headers={"paystack-signature": self.platform_signature(raw)},
        )

    def verified(self, payment, **overrides):
        data = {
            "id": 9001,
            "reference": payment.reference,
            "status": "success",
            "amount": int(payment.amount * 100),
            "currency": payment.currency,
        }
        data.update(overrides)
        return data

    def test_anonymous_checkout_goes_to_sign_in_first(self):
        response = self.client.get("/admin/plan/growth/")
        self.assertRedirects(
            response,
            "/admin/login/?next=/admin/plan/growth/",
            fetch_redirect_response=False,
        )

    def test_checkout_page_offers_both_rails(self):
        self.client.force_login(self.user)
        response = self.client.get("/admin/plan/growth/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pay for Growth")
        self.assertContains(response, "Card or bank")
        self.assertContains(response, "M-Pesa")
        self.assertContains(response, "1,000")

    @mock.patch("apps.web.plan_views.paystack.initialize_platform")
    def test_paystack_starts_a_platform_plan_payment(self, initialize):
        initialize.return_value = {"authorization_url": "https://checkout.paystack.com/plan"}
        self.client.force_login(self.user)

        response = self.client.post(
            "/admin/plan/growth/",
            {"provider": "paystack", "email": "ops@example.com"},
        )

        self.assertRedirects(
            response,
            "https://checkout.paystack.com/plan",
            fetch_redirect_response=False,
        )
        payment = PlanPayment.objects.get()
        self.assertEqual(payment.isp, self.isp)
        self.assertEqual(payment.plan, self.plan)
        self.assertEqual(payment.amount, Decimal("1000"))
        self.assertEqual(payment.status, "pending")
        initialize.assert_called_once()

    @mock.patch("apps.web.plan_views.mpesa.stk_push_platform")
    def test_mpesa_starts_a_prompt_and_records_the_checkout(self, stk_push):
        stk_push.return_value = {"CheckoutRequestID": "ws_CO_123"}
        self.client.force_login(self.user)

        response = self.client.post(
            "/admin/plan/growth/",
            {"provider": "mpesa", "phone": "0712 345 678"},
        )

        payment = PlanPayment.objects.get()
        self.assertRedirects(
            response,
            f"/admin/plan/status/{payment.reference}/",
            fetch_redirect_response=False,
        )
        self.assertEqual(payment.provider, "mpesa")
        self.assertEqual(payment.provider_reference, "ws_CO_123")
        self.assertEqual(payment.phone, "254712345678")

    @mock.patch("apps.billing.views.paystack.verify_platform")
    def test_platform_webhook_activates_the_plan_once(self, verify):
        payment = self.create_payment()
        verify.return_value = self.verified(payment)
        payload = {
            "event": "charge.success",
            "data": {"id": 9001, "reference": payment.reference},
        }

        response = self.post_plan_webhook(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "paid")
        payment.refresh_from_db()
        self.isp.refresh_from_db()
        self.assertEqual(payment.status, "paid")
        self.assertIsNotNone(payment.period_end)
        self.assertEqual(self.isp.plan, "growth")
        self.assertAlmostEqual(
            self.isp.plan_ends_at,
            timezone.now() + timedelta(days=30),
            delta=timedelta(seconds=5),
        )
        self.assertEqual(WebhookEvent.objects.filter(provider="paystack").count(), 1)

        # A replayed delivery must not add a second period.
        end = self.isp.plan_ends_at
        verify.return_value = self.verified(payment)
        self.post_plan_webhook(payload)
        self.isp.refresh_from_db()
        self.assertEqual(self.isp.plan_ends_at, end)

    def test_bad_platform_signature_is_refused(self):
        payment = self.create_payment()
        raw = json.dumps(
            {"event": "charge.success", "data": {"reference": payment.reference}}
        ).encode()
        response = self.client.post(
            "/payments/plan/webhook/paystack/",
            data=raw,
            content_type="application/json",
            headers={"paystack-signature": "t=1,v1=whk_platform:" + "0" * 128},
        )
        self.assertEqual(response.status_code, 401)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")

    @mock.patch("apps.billing.views.paystack.verify_platform")
    def test_plan_callback_verifies_and_opens_the_status_page(self, verify):
        payment = self.create_payment()
        verify.return_value = self.verified(payment)

        response = self.client.get(
            f"/payments/plan/callback/?reference={payment.reference}"
        )

        self.assertRedirects(
            response,
            f"/admin/login/?next=/admin/plan/status/{payment.reference}/",
            fetch_redirect_response=False,
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "paid")

    def test_renewal_extends_from_the_existing_end_date(self):
        future = timezone.now() + timedelta(days=10)
        self.isp.plan = "growth"
        self.isp.plan_ends_at = future
        self.isp.save(update_fields=["plan", "plan_ends_at"])
        payment = self.create_payment(reference="plan-renew")

        settle_plan_payment(payment)

        self.isp.refresh_from_db()
        self.assertAlmostEqual(
            self.isp.plan_ends_at,
            future + timedelta(days=30),
            delta=timedelta(seconds=5),
        )

    def test_payment_does_not_lift_a_manual_suspension(self):
        self.isp.status = "suspended"
        self.isp.save(update_fields=["status"])
        payment = self.create_payment(reference="plan-paused")

        settle_plan_payment(payment)

        self.isp.refresh_from_db()
        self.assertEqual(self.isp.status, "suspended")
        self.assertTrue(self.isp.service_suspended)
        self.assertEqual(self.isp.plan, "growth")

    def test_other_isp_cannot_read_a_plan_payment(self):
        payment = self.create_payment(reference="plan-private")
        other = Isp.objects.create(name="Other", slug="other")
        other_user = get_user_model().objects.create_user(
            username="other", password="other-pass-123"
        )
        Membership.objects.create(user=other_user, isp=other, role="owner")
        self.client.force_login(other_user)

        self.assertEqual(
            self.client.get(f"/admin/plan/status/{payment.reference}/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/admin/plan/status/{payment.reference}/state/").status_code,
            404,
        )

    def test_mpesa_callback_checks_the_checkout_and_amount(self):
        payment = self.create_payment(
            reference="plan-mpesa", provider="mpesa", provider_reference="ws_CO_123"
        )
        payload = {
            "Body": {
                "stkCallback": {
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CheckoutRequestID": "ws_CO_other",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 1000},
                            {"Name": "MpesaReceiptNumber", "Value": "RCPT123"},
                            {"Name": "PhoneNumber", "Value": 254712345678},
                        ]
                    },
                }
            }
        }

        self.client.post(
            f"/payments/plan/mpesa/callback/{payment.reference}/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")

        payload["Body"]["stkCallback"]["CheckoutRequestID"] = "ws_CO_123"
        payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"][0]["Value"] = 1
        self.client.post(
            f"/payments/plan/mpesa/callback/{payment.reference}/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")

        payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"][0]["Value"] = 1000
        self.client.post(
            f"/payments/plan/mpesa/callback/{payment.reference}/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        payment.refresh_from_db()
        self.isp.refresh_from_db()
        self.assertEqual(payment.status, "paid")
        self.assertEqual(self.isp.plan, "growth")

    def test_pending_status_page_polls_and_returns_json(self):
        payment = self.create_payment(reference="plan-pending")
        self.client.force_login(self.user)

        response = self.client.get(f"/admin/plan/status/{payment.reference}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirming your payment")
        self.assertContains(response, f"/admin/plan/status/{payment.reference}/state/")

        state = self.client.get(f"/admin/plan/status/{payment.reference}/state/")
        self.assertEqual(state.json()["status"], "pending")

    def test_paid_status_page_opens_the_panel(self):
        end = timezone.now() + timedelta(days=30)
        self.isp.plan = "growth"
        self.isp.plan_ends_at = end
        self.isp.save(update_fields=["plan", "plan_ends_at"])
        payment = self.create_payment(
            reference="plan-paid",
            status="paid",
            period_start=timezone.now(),
            period_end=end,
            paid_at=timezone.now(),
        )
        self.client.force_login(self.user)

        response = self.client.get(f"/admin/plan/status/{payment.reference}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Plan active")
        self.assertContains(response, "Growth for Net")
        self.assertContains(response, "Open the panel")

    @override_settings(
        PAYSTACK_SECRET_KEY="",
        MPESA_PLATFORM_SHORTCODE="",
        MPESA_PLATFORM_CONSUMER_KEY="",
        MPESA_PLATFORM_CONSUMER_SECRET="",
        MPESA_PLATFORM_PASSKEY="",
    )
    def test_checkout_explains_when_no_rail_is_configured(self):
        self.client.force_login(self.user)

        response = self.client.get("/admin/plan/growth/")

        self.assertContains(response, "Payments unavailable")
        self.assertNotContains(response, "Send payment prompt")

    def test_manual_suspension_blocks_a_new_plan_payment(self):
        self.isp.status = "suspended"
        self.isp.save(update_fields=["status"])
        self.client.force_login(self.user)

        response = self.client.post(
            "/admin/plan/growth/",
            {"provider": "mpesa", "phone": "0712 345 678"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Contact support", status_code=403)
        self.assertEqual(PlanPayment.objects.count(), 0)


class SuspensionSweepTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(
            name="Net",
            slug="net",
            plan="trial",
            trial_ends_at=timezone.now() - timedelta(minutes=5),
        )
        self.package = Package.objects.create(
            isp=self.isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        self.customer = Customer.objects.create(
            isp=self.isp, mac_address="A4:5E:60:1C:92:0B"
        )

    def grant(self):
        return AccessGrant.objects.create(
            isp=self.isp,
            customer=self.customer,
            package=self.package,
            state="active",
            started_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_sweep_revokes_live_guest_access(self):
        grant = self.grant()
        self.assertEqual(revoke_suspended_grants(), 1)
        grant.refresh_from_db()
        self.assertEqual(grant.state, "revoked")
        self.assertEqual(grant.revoked_reason, "network paused")

    def test_provisioning_is_deferred_for_a_paused_network(self):
        grant = self.grant()
        with self.assertRaises(ProvisioningDeferred):
            activate_grant(grant)
        grant.refresh_from_db()
        self.assertEqual(grant.state, "active")
        self.assertEqual(grant.revoked_reason, "network paused")
