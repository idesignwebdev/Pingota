"""Rate limiting, lockout and browser-hardening checks."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.accounts.models import Membership
from apps.network.models import Package
from apps.tenants.models import Isp
from core.security import (
    brute_force_check,
    brute_force_clear,
    check_rate_limit,
    get_client_ip,
)

FORWARDED_A = "8.8.4.4"
FORWARDED_B = "9.9.9.9"


def operator_for(isp: Isp, username: str = "ops", password: str = "operator-pass-123"):
    user = get_user_model().objects.create_user(username=username, password=password)
    Membership.objects.create(user=user, isp=isp, role="owner")
    return user


class ClientIpTests(SimpleTestCase):
    def request(self, remote="127.0.0.1", **headers):
        request = RequestFactory().get("/")
        request.META["REMOTE_ADDR"] = remote
        for name, value in headers.items():
            request.META[name] = value
        return request

    def test_cloudflare_header_wins_over_forwarded_chain(self):
        request = self.request(
            HTTP_CF_CONNECTING_IP="1.1.1.1",
            HTTP_X_FORWARDED_FOR="8.8.8.8, 10.0.0.2",
        )
        self.assertEqual(get_client_ip(request), "1.1.1.1")

    def test_forwarded_chain_prefers_the_leftmost_public_address(self):
        request = self.request(HTTP_X_FORWARDED_FOR="8.8.4.4, 10.0.0.2")
        self.assertEqual(get_client_ip(request), FORWARDED_A)

    def test_public_peer_cannot_spoof_a_forwarded_header(self):
        request = self.request(
            remote="8.8.8.8",
            HTTP_X_FORWARDED_FOR="1.1.1.1",
        )
        self.assertEqual(get_client_ip(request), "8.8.8.8")

    @override_settings(TRUSTED_PROXY_IPS=["8.8.8.8"])
    def test_configured_public_proxy_is_trusted(self):
        request = self.request(
            remote="8.8.8.8",
            HTTP_X_FORWARDED_FOR="1.1.1.1",
        )
        self.assertEqual(get_client_ip(request), "1.1.1.1")

    @override_settings(TRUST_PROXY_HEADERS=False)
    def test_forwarded_headers_can_be_disabled(self):
        request = self.request(
            remote="127.0.0.1",
            HTTP_X_FORWARDED_FOR="1.1.1.1",
        )
        self.assertEqual(get_client_ip(request), "127.0.0.1")


@override_settings(SECURITY_LIMITS_ENABLED=True)
class RateLimitCoreTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_budget_is_per_client_ip(self):
        request = RequestFactory().get("/")

        for _ in range(3):
            allowed, _ = check_rate_limit(request, "core", 3, 60)
            self.assertTrue(allowed)

        allowed, retry = check_rate_limit(request, "core", 3, 60)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)

        other = RequestFactory().get("/", HTTP_X_FORWARDED_FOR=FORWARDED_B)
        other.META["REMOTE_ADDR"] = "127.0.0.1"
        allowed, _ = check_rate_limit(other, "core", 3, 60)
        self.assertTrue(allowed)

    def test_scopes_have_separate_budgets(self):
        request = RequestFactory().get("/")
        for _ in range(3):
            self.assertTrue(check_rate_limit(request, "one", 3, 60)[0])
        self.assertFalse(check_rate_limit(request, "one", 3, 60)[0])
        self.assertTrue(check_rate_limit(request, "two", 3, 60)[0])


@override_settings(SECURITY_LIMITS_ENABLED=True)
class SignupRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_eleventh_signup_from_one_address_is_refused(self):
        payload = {
            "business_name": "Kondele Fibre",
            "email": "owner@example.com",
            "phone": "0712000000",
        }
        for _ in range(10):
            response = self.client.post("/signup/", payload)
            self.assertEqual(response.status_code, 302)

        response = self.client.post("/signup/", payload)
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many tries", status_code=429)
        self.assertIn("Retry-After", response)


@override_settings(SECURITY_LIMITS_ENABLED=True)
class LoginLockoutTests(TestCase):
    def setUp(self):
        cache.clear()
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.operator = operator_for(self.isp)

    def tearDown(self):
        cache.clear()

    def wrong(self, **extra):
        headers = extra.pop("headers", {})
        return self.client.post(
            "/admin/login/",
            {"username": "ops", "password": "wrong-password"},
            **headers,
            **extra,
        )

    def correct(self, forwarded: str | None = None):
        extra = {"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {}
        return self.client.post(
            "/admin/login/",
            {"username": "ops", "password": "operator-pass-123"},
            **extra,
        )

    def test_fourth_failure_starts_friction_and_sixth_locks_out(self):
        for _ in range(3):
            self.assertEqual(self.wrong().status_code, 200)

        response = self.wrong()
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many tries", status_code=429)
        self.assertIn("Retry-After", response)

        self.assertEqual(self.wrong().status_code, 429)
        response = self.wrong()
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response["Retry-After"]), 60)

        # A correct password does not bypass the lockout.
        self.assertEqual(self.correct().status_code, 429)

    def test_success_clears_the_failure_counter(self):
        for _ in range(3):
            self.assertEqual(self.wrong().status_code, 200)

        self.assertEqual(self.correct().status_code, 302)
        tier, count, _, _ = brute_force_check("login", "ops", "127.0.0.1")
        self.assertEqual((tier, count), ("ok", 0))

        for _ in range(3):
            self.assertEqual(self.wrong().status_code, 200)

    def test_a_different_address_is_not_locked_out(self):
        for _ in range(6):
            self.wrong(headers={"HTTP_X_FORWARDED_FOR": FORWARDED_A})

        self.assertEqual(self.correct(forwarded=FORWARDED_A).status_code, 429)
        self.assertEqual(self.correct(forwarded=FORWARDED_B).status_code, 302)

    def test_clear_helper_releases_the_pair(self):
        for _ in range(6):
            self.wrong()
        self.assertEqual(self.correct().status_code, 429)

        brute_force_clear("login", "ops", "127.0.0.1")
        self.assertEqual(self.correct().status_code, 302)


@override_settings(SECURITY_LIMITS_ENABLED=True)
class DjangoAdminLoginLockoutTests(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_superuser(
            username="owner", email="owner@example.com", password="owner-pass-123"
        )

    def tearDown(self):
        cache.clear()

    def test_django_admin_login_uses_the_same_lockout(self):
        for _ in range(6):
            response = self.client.post(
                "/django-admin/login/",
                {"username": "owner", "password": "wrong-password"},
            )
            self.assertIn(response.status_code, (200, 429))

        response = self.client.post(
            "/django-admin/login/",
            {"username": "owner", "password": "owner-pass-123"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

        brute_force_clear("login", "owner", "127.0.0.1")
        response = self.client.post(
            "/django-admin/login/",
            {"username": "owner", "password": "owner-pass-123"},
        )
        self.assertEqual(response.status_code, 302)

    def test_custom_admin_site_is_active(self):
        from django.contrib import admin

        self.assertEqual(admin.site.__class__.__name__, "PingotaAdminSite")


@override_settings(SECURITY_LIMITS_ENABLED=True)
class PortalRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.package = Package.objects.create(
            isp=self.isp, name="1 hour", price="10", duration_minutes=60
        )

    def tearDown(self):
        cache.clear()

    def test_checkout_is_limited_per_device(self):
        import uuid

        unknown_package = uuid.uuid4()
        payload = {"mac": "A4-5E-60-1C-92-0B"}
        for _ in range(10):
            response = self.client.post(
                f"/portal/net/checkout/{unknown_package}/", payload
            )
            self.assertEqual(response.status_code, 400)

        response = self.client.post(
            f"/portal/net/checkout/{unknown_package}/", payload
        )
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many tries", status_code=429)
        self.assertIn("Retry-After", response)

    def test_voucher_guesses_are_limited_per_address(self):
        for _ in range(10):
            response = self.client.post(
                "/portal/net/voucher/",
                {"voucher": "ZZZZ-ZZZZ", "mac": "A4-5E-60-1C-92-0B"},
            )
            self.assertEqual(response.status_code, 400)

        response = self.client.post(
            "/portal/net/voucher/",
            {"voucher": "ZZZZ-ZZZZ", "mac": "A4-5E-60-1C-92-0B"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many tries", status_code=429)


@override_settings(SECURITY_LIMITS_ENABLED=True)
class RouterReportRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_router_call_home_is_limited_per_address(self):
        for _ in range(30):
            response = self.client.get("/routers/not-a-real-token/report/")
            self.assertEqual(response.status_code, 404)

        response = self.client.get("/routers/not-a-real-token/report/")
        self.assertEqual(response.status_code, 429)
        self.assertFalse(response.json()["ok"])
        self.assertIn("Retry-After", response)


class SecurityHeaderTests(TestCase):
    def test_private_pages_are_not_indexed(self):
        response = self.client.get("/admin/login/")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn("no-store", response["Cache-Control"])

    def test_browser_hardening_headers_are_present(self):
        response = self.client.get("/")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(
            response["Referrer-Policy"], "strict-origin-when-cross-origin"
        )
        self.assertIn("object-src 'none'", response["Content-Security-Policy"])

    @override_settings(
        SECURE_HSTS_SECONDS=31536000,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
        SECURE_HSTS_PRELOAD=True,
        SECURE_SSL_REDIRECT=False,
    )
    def test_hsts_is_sent_on_https(self):
        response = self.client.get("/", secure=True)
        self.assertEqual(
            response["Strict-Transport-Security"],
            "max-age=31536000; includeSubDomains; preload",
        )
