"""Smoke tests for the paths that cost money if they break.

They run on the default test database (SQLite unless MONGODB_URI is set), with
the router layer stubbed out.
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Payment
from apps.billing.service import settle_payment
from apps.network.models import AccessGrant, Customer, Package
from apps.tenants.models import Isp, Router

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
        for secret in ("sk_live_SUPERSECRET", "SECRETVALUE456", "PASSKEY789", "KEYVALUE123", "174379"):
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

        router = Router.objects.create(isp=Isp.objects.create(name="N", slug="n"), name="AP")
        router.set_password("routerpass")
        router.save()

        call_command("rotate_key", old_key=old_key, new_key=new_key, verbosity=0)

        router.refresh_from_db()
        self.assertEqual(decrypt_with(router.api_password, new_key), "routerpass")
        self.assertEqual(decrypt_with(router.api_password, old_key), "")

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
