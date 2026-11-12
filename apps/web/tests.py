"""End-to-end checks for sign-in, navigation and the marketing/panel flows."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.template.loader import get_template
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Membership, SignupRequest
from apps.billing import mpesa
from apps.billing.models import Payment
from apps.network.models import Package
from apps.tenants.models import Isp, MpesaConfig, Router, SubscriptionPlan
from apps.web.admin_views import _daily_totals

TEST_KEY = "ZMD6DO1R8ZmXwA-qxTiemkgDNI3ZqSWbaSyoZWCSe5Q="


def operator_for(isp: Isp, username: str = "ops", password: str = "operator-pass-123"):
    user = get_user_model().objects.create_user(username=username, password=password)
    Membership.objects.create(user=user, isp=isp, role="owner")
    return user


class AuthFlowTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.operator = operator_for(self.isp)
        self.owner = get_user_model().objects.create_superuser(
            username="owner", email="owner@example.com", password="owner-pass-123"
        )

    def test_owner_login_redirects_to_django_admin(self):
        response = self.client.post(
            "/admin/login/", {"username": "owner", "password": "owner-pass-123"}
        )
        self.assertRedirects(response, "/django-admin/", fetch_redirect_response=False)

    def test_owner_deep_link_into_django_admin_is_kept(self):
        response = self.client.post(
            "/admin/login/",
            {
                "username": "owner",
                "password": "owner-pass-123",
                "next": "/django-admin/accounts/signuprequest/",
            },
        )
        self.assertRedirects(
            response,
            "/django-admin/accounts/signuprequest/",
            fetch_redirect_response=False,
        )

    def test_operator_login_redirects_to_the_panel(self):
        response = self.client.post(
            "/admin/login/", {"username": "ops", "password": "operator-pass-123"}
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_wrong_password_shows_a_plain_error(self):
        response = self.client.post(
            "/admin/login/", {"username": "ops", "password": "not-the-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in failed")
        self.assertNotContains(response, "not-the-password")

    def test_owner_direct_panel_access_redirects_to_django_admin(self):
        self.client.force_login(self.owner)
        for path in ("/admin/", "/admin/network/", "/admin/settings/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertRedirects(
                    response, "/django-admin/", fetch_redirect_response=False
                )

    def test_unauthenticated_panel_access_redirects_to_login(self):
        response = self.client.get("/admin/")
        self.assertRedirects(
            response,
            "/admin/login/?next=/admin/",
            fetch_redirect_response=False,
        )

    def test_logout_ends_the_panel_session(self):
        self.client.force_login(self.operator)
        response = self.client.post("/admin/logout/")
        self.assertRedirects(
            response, "/admin/login/", fetch_redirect_response=False
        )
        self.assertRedirects(
            self.client.get("/admin/"),
            "/admin/login/?next=/admin/",
            fetch_redirect_response=False,
        )

    def test_password_change_then_sign_in_with_the_new_password(self):
        self.client.force_login(self.operator)
        response = self.client.post(
            "/admin/password-change/",
            {
                "old_password": "operator-pass-123",
                "new_password1": "new-operator-pass-456",
                "new_password2": "new-operator-pass-456",
            },
        )
        self.assertRedirects(
            response,
            "/admin/password-change/done/",
            fetch_redirect_response=False,
        )
        self.client.logout()
        response = self.client.post(
            "/admin/login/",
            {"username": "ops", "password": "new-operator-pass-456"},
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)


class PanelNavigationTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.operator = operator_for(self.isp)
        self.client.force_login(self.operator)

    def test_every_panel_section_loads(self):
        paths = (
            "/admin/",
            "/admin/live/",
            "/admin/customers/",
            "/admin/packages/",
            "/admin/packages/add/",
            "/admin/vouchers/",
            "/admin/vouchers/batches/",
            "/admin/vouchers/generate/",
            "/admin/vouchers/sheet/",
            "/admin/network/",
            "/admin/revenue/",
            "/admin/settings/",
            "/admin/password-change/",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_panel_nav_links_are_present(self):
        response = self.client.get("/admin/")
        for label in ("Overview", "Live access", "Customers", "Passes", "Vouchers", "Network", "Revenue", "Settings"):
            with self.subTest(label=label):
                self.assertContains(response, label)

    def test_owner_can_still_open_django_admin(self):
        owner = get_user_model().objects.create_superuser(
            username="owner", email="owner@example.com", password="owner-pass-123"
        )
        self.client.force_login(owner)
        response = self.client.get("/django-admin/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Platform administration")


class SignupApprovalTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_superuser(
            username="owner", email="owner@example.com", password="owner-pass-123"
        )
        self.client.force_login(self.owner)
        self.signup = SignupRequest.objects.create(
            business_name="Kondele Fibre",
            contact_name="Amina",
            email="amina@example.com",
            phone="0712000000",
            routers=2,
        )

    def test_signup_request_admin_is_reachable(self):
        response = self.client.get("/django-admin/accounts/signuprequest/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Kondele Fibre")

    def test_approval_creates_a_dated_trial_and_operator_login(self):
        response = self.client.post(f"/signup/{self.signup.pk}/approve/")
        self.assertRedirects(
            response,
            "/django-admin/accounts/signuprequest/",
            fetch_redirect_response=False,
        )

        self.signup.refresh_from_db()
        self.assertEqual(self.signup.status, "approved")
        isp = Isp.objects.get(name="Kondele Fibre")
        self.assertEqual(isp.plan, "trial")
        self.assertIsNotNone(isp.trial_ends_at)
        self.assertGreater(isp.trial_ends_at, timezone.now())
        self.assertTrue(Membership.objects.filter(user__username="amina@example.com", isp=isp).exists())


class TrialEnforcementTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.operator = operator_for(self.isp)

    def test_active_dated_trial_can_use_the_panel(self):
        self.isp.plan = "trial"
        self.isp.trial_ends_at = timezone.now() + timedelta(hours=3)
        self.isp.save(update_fields=["plan", "trial_ends_at"])
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_expired_trial_shows_the_trial_page_and_keeps_the_portal_open(self):
        self.isp.plan = "trial"
        self.isp.trial_ends_at = timezone.now() - timedelta(minutes=5)
        self.isp.save(update_fields=["plan", "trial_ends_at"])
        self.client.force_login(self.operator)

        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "The trial for Net has ended", status_code=403)
        self.assertContains(response, "See plans", status_code=403)

        self.assertEqual(self.client.get("/admin/settings/").status_code, 403)
        self.assertEqual(self.client.get("/portal/net/").status_code, 200)

    def test_paid_plan_ignores_an_old_trial_date(self):
        self.isp.plan = "growth"
        self.isp.trial_ends_at = timezone.now() - timedelta(days=3)
        self.isp.save(update_fields=["plan", "trial_ends_at"])
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_legacy_trial_without_a_date_stays_open(self):
        self.isp.plan = "trial"
        self.isp.trial_ends_at = None
        self.isp.save(update_fields=["plan", "trial_ends_at"])
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/admin/").status_code, 200)


class PricingTests(TestCase):
    def test_pricing_page_reads_live_plan_prices(self):
        plan = SubscriptionPlan.objects.create(
            code="growth",
            name="Growth",
            monthly_price=Decimal("1234.00"),
            max_routers=3,
            features=["Up to 3 routers"],
        )
        response = self.client.get("/pricing/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Growth")
        self.assertContains(response, "1,234")
        self.assertContains(response, "Up to 3 routers")

        plan.monthly_price = Decimal("1500.00")
        plan.save(update_fields=["monthly_price"])
        response = self.client.get("/pricing/")
        self.assertContains(response, "1,500")
        self.assertNotContains(response, "1,234")

    @override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
    def test_seed_demo_creates_plans_and_customer_links(self):
        call_command("seed_demo", verbosity=0)
        self.assertEqual(
            set(SubscriptionPlan.objects.values_list("code", flat=True)),
            {"starter", "growth", "scale"},
        )
        demo_isp = Isp.objects.get(slug="demo-hotspot")
        self.assertFalse(Payment.objects.filter(isp=demo_isp, customer__isnull=True).exists())


class RevenueChartTests(TestCase):
    def test_daily_totals_returns_every_day_even_with_no_sales(self):
        isp = Isp.objects.create(name="Net", slug="net")
        package = Package.objects.create(
            isp=isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        paid_at = timezone.now() - timedelta(days=2)
        Payment.objects.create(
            isp=isp,
            package=package,
            reference="pg-revenue",
            amount=Decimal("10"),
            status="paid",
            paid_at=paid_at,
        )

        rows = _daily_totals(isp, days=7, fmt="%Y-%m-%d")
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[-1]["total"], 0)
        sold_day = timezone.localtime(paid_at).date().strftime("%Y-%m-%d")
        self.assertEqual(next(row["total"] for row in rows if row["day"] == sold_day), 10)

    def test_revenue_page_chart_has_thirty_buckets(self):
        isp = Isp.objects.create(name="Net", slug="net")
        operator = operator_for(isp)
        self.client.force_login(operator)
        response = self.client.get("/admin/revenue/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="revenue-data"')
        rows = _daily_totals(isp, days=30, fmt="%d %b")
        self.assertEqual(len(rows), 30)


class RouterHealthTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")

    def make_router(self, **kwargs):
        defaults = {"isp": self.isp, "name": "AP"}
        defaults.update(kwargs)
        return Router.objects.create(**defaults)

    def test_disabled_router_wins_over_an_old_online_probe(self):
        router = self.make_router(enabled=False, status="connected", last_status="online")
        self.assertEqual(router.health_state, "disabled")
        self.assertEqual(router.health_label, "Disabled")

    def test_pending_router_is_waiting_for_install(self):
        router = self.make_router(status="pending")
        self.assertEqual(router.health_state, "pending")
        self.assertEqual(router.health_label, "Waiting for install")

    def test_successful_probe_is_online(self):
        router = self.make_router(status="connected", last_status="online")
        self.assertEqual(router.health_state, "online")
        self.assertEqual(router.health_label, "Online")

    def test_failed_probe_is_offline_after_a_connected_install(self):
        router = self.make_router(status="connected", last_status="timeout · 10.70.0.2:8728")
        self.assertEqual(router.health_state, "offline")
        self.assertEqual(router.health_label, "Last check failed")

    def test_connected_router_without_a_probe_is_not_checked(self):
        router = self.make_router(status="connected")
        self.assertEqual(router.health_state, "unchecked")
        self.assertEqual(router.health_label, "Not checked")


@override_settings(PUBLIC_BASE_URL="https://pingota.example")
class PortalCheckoutTests(TestCase):
    def setUp(self):
        self.isp = Isp.objects.create(name="Net", slug="net")
        self.package = Package.objects.create(
            isp=self.isp,
            name="1 hour",
            price=Decimal("10"),
            duration_minutes=60,
        )

    @mock.patch("apps.web.portal_views.paystack.initialize")
    def test_no_js_pass_form_creates_payment_with_customer_and_callback(self, initialize):
        initialize.return_value = {"authorization_url": "https://checkout.paystack.com/test"}
        response = self.client.post(
            "/portal/net/packages/",
            {
                "package_id": str(self.package.id),
                "mac": "A4-5E-60-1C-92-0B",
                "phone": "254712345678",
                "provider": "paystack",
                "email": "guest@example.com",
            },
        )

        self.assertRedirects(
            response,
            "https://checkout.paystack.com/test",
            fetch_redirect_response=False,
        )
        payment = Payment.objects.get()
        self.assertEqual(payment.customer.mac_address, "A4:5E:60:1C:92:0B")
        self.assertEqual(
            initialize.call_args.kwargs["callback_url"],
            "https://pingota.example/payments/callback/",
        )


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY, PUBLIC_BASE_URL="https://pingota.example")
class CallbackUrlTests(TestCase):
    def test_mpesa_callback_url_matches_the_registered_route(self):
        isp = Isp.objects.create(name="Net", slug="net")
        package = Package.objects.create(
            isp=isp, name="1 hour", price=Decimal("10"), duration_minutes=60
        )
        cfg = MpesaConfig.objects.create(isp=isp, enabled=True)
        cfg.set_shortcode("174379")
        cfg.set_consumer_key("consumer-key")
        cfg.set_secret("consumer-secret")
        cfg.set_passkey("passkey")
        cfg.save()
        payment = Payment.objects.create(
            isp=isp,
            package=package,
            provider="mpesa",
            reference="pg-callback",
            amount=Decimal("10"),
            phone="254712345678",
        )

        with mock.patch("apps.billing.mpesa.access_token", return_value="token"), mock.patch(
            "apps.billing.mpesa.httpx.post"
        ) as post:
            post.return_value.status_code = 200
            post.return_value.content = b"{}"
            post.return_value.json.return_value = {"ResponseCode": "0"}
            mpesa.stk_push(payment, "254712345678")

        self.assertEqual(
            post.call_args.kwargs["json"]["CallBackURL"],
            "https://pingota.example/payments/mpesa/callback/net/",
        )


@override_settings(DEBUG=False)
class ErrorPageTests(TestCase):
    def test_unknown_address_uses_the_branded_page(self):
        response = self.client.get("/this-address-does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Page not found", status_code=404)
        self.assertNotContains(response, "Traceback", status_code=404)

    def test_server_error_template_is_branded(self):
        body = get_template("500.html").render()
        self.assertIn("This page could not load", body)
        self.assertIn("Pingota", body)
        self.assertNotIn("Traceback", body)
