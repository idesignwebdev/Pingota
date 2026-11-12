"""Router onboarding screens and the router's call-home endpoint."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.network import mikrotik, onboarding
from apps.network.tunguard import TunGuardError
from apps.network.models import SessionEvent
from apps.tenants.models import Router
from apps.web.admin_views import current_isp, panel_required

log = logging.getLogger(__name__)


@panel_required
@require_POST
def add_router(request):
    isp = current_isp(request)
    name = (request.POST.get("name") or "").strip()[:80]
    if not name:
        messages.error(request, "Give the router a name, such as the location it serves.")
        return redirect("panel:network")

    limit = isp.routers.count()
    from apps.tenants.models import SubscriptionPlan

    plan = SubscriptionPlan.objects.filter(code=isp.plan).first()
    if plan and plan.max_routers and limit >= plan.max_routers:
        messages.error(
            request,
            f"The {plan.name} plan allows {plan.max_routers} router(s). Remove one or upgrade before adding another.",
        )
        return redirect("panel:network")

    router = Router(isp=isp, name=name)
    onboarding.provision_identity(router)
    registered = False
    try:
        onboarding.register_tunnel_peer(router)
        registered = True
        router.save()
    except (TunGuardError, IntegrityError, RuntimeError) as exc:
        if registered:
            try:
                onboarding.remove_tunnel_peer(router.tunnel_public_key)
            except TunGuardError:
                log.exception("Could not roll back tunnel peer after router save failed")
        messages.error(request, f"Could not prepare the router tunnel: {exc}")
        return redirect("panel:network")
    messages.success(request, f"{name} is ready. Copy the install script onto the router.")
    return redirect("panel:router_detail", router_id=router.id)


@panel_required
@require_GET
def router_detail(request, router_id):
    isp = current_isp(request)
    router = get_object_or_404(Router, pk=router_id, isp=isp)
    try:
        script = onboarding.install_script(router)
        script_error = ""
    except (ValueError, TunGuardError) as exc:
        script, script_error = "", str(exc)
    return render(
        request,
        "admin/router_detail.html",
        {
            "active_nav": "network",
            "isp": isp,
            "router": router,
            "script": script,
            "script_error": script_error,
            "tunnel_host": settings.ROUTER_TUNNEL_HOST,
            "tunnel_port": settings.ROUTER_TUNNEL_PORT,
            "events": SessionEvent.objects.filter(router=router)[:10],
        },
    )


@panel_required
@require_POST
def regenerate(request, router_id):
    isp = current_isp(request)
    router = get_object_or_404(Router, pk=router_id, isp=isp)
    was_connected = router.status == "connected"
    old_public_key = router.tunnel_public_key
    onboarding.provision_identity(router)
    try:
        onboarding.register_tunnel_peer(router)
        router.save()
    except (TunGuardError, IntegrityError) as exc:
        try:
            onboarding.remove_tunnel_peer(router.tunnel_public_key)
        except TunGuardError:
            log.exception("Could not roll back a new tunnel peer for router %s", router.pk)
        messages.error(request, f"Could not regenerate router tunnel credentials: {exc}")
        return redirect("panel:router_detail", router_id=router.id)
    try:
        onboarding.remove_tunnel_peer(old_public_key)
    except TunGuardError:
        log.exception("Could not remove the previous tunnel peer for router %s", router.pk)
    msg = "New credentials generated. The old script no longer works; paste the new one."
    if was_connected:
        msg += " This router will go offline until it is reinstalled."
    messages.warning(request, msg)
    return redirect("panel:router_detail", router_id=router.id)


@csrf_exempt
@require_GET
def router_report(request, token):
    """The router calling home after applying its script.

    Authenticated by the unguessable install token only. It records what the
    router says about itself and nothing else - the panel still verifies the
    tunnel separately before trusting it.
    """
    router = Router.objects.filter(install_token=token).first()
    if router is None:
        return JsonResponse({"ok": False, "error": "unknown token"}, status=404)
    version = (request.GET.get("version") or "")[:40]
    board = (request.GET.get("board") or "")[:80]
    try:
        mikrotik.health(router)
    except (mikrotik.RouterError, OSError, ValueError) as exc:
        router.last_status = f"Tunnel not reachable: {exc}"[:120]
        router.save(update_fields=["last_status"])
        return HttpResponse(
            "Router reported installation, but Pingota cannot reach its RouterOS API through the tunnel yet. "
            "Check the tunnel host, UDP port and TunGuard status, then paste the install script again.\n",
            status=503,
            content_type="text/plain",
        )
    onboarding.report_as_connected(router, version=version, board=board)
    log.info("router %s reported in (RouterOS %s)", router, version)
    return HttpResponse("ok\n", content_type="text/plain")
