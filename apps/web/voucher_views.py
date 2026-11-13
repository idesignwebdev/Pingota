"""Voucher screens: generate a batch, list codes, print a sheet, void one."""
from __future__ import annotations

from datetime import datetime

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.network.models import Package, Voucher
from apps.network.vouchers import VoucherError, generate as generate_codes
from apps.web.admin_views import current_isp, panel_required, staff_required


@panel_required
def list_vouchers(request):
    isp = current_isp(request)
    state = request.GET.get("state") or "unused"
    batch = request.GET.get("batch") or ""
    qs = isp.vouchers.select_related("package", "redeemed_by")
    if state != "all":
        qs = qs.filter(state=state)
    if batch:
        qs = qs.filter(batch=batch)
    return render(
        request,
        "admin/vouchers.html",
        {
            "active_nav": "vouchers",
            "isp": isp,
            "vouchers": qs[:200],
            "state": state,
            "batch": batch,
            "counts": {
                row["state"]: row["n"] for row in isp.vouchers.values("state").annotate(n=Count("id"))
            },
        },
    )


@panel_required
def list_batches(request):
    isp = current_isp(request)
    rows = (
        isp.vouchers.values("batch", "package__name")
        .annotate(total=Count("id"))
        .order_by("-batch")
    )
    return render(
        request,
        "admin/voucher_batches.html",
        {"active_nav": "vouchers", "isp": isp, "rows": rows},
    )


@staff_required
def generate(request):
    isp = current_isp(request)
    packages_qs = isp.packages.filter(is_active=True)
    if request.method == "POST":
        package = get_object_or_404(Package, pk=request.POST.get("package_id"), isp=isp)
        try:
            count = int(request.POST.get("count") or 10)
            expires_on = request.POST.get("expires_on") or None
            expiry = datetime.strptime(expires_on, "%Y-%m-%d").date() if expires_on else None
            made = generate_codes(
                isp, package, count, batch=(request.POST.get("batch") or "").strip(), expires_on=expiry
            )
        except (VoucherError, ValueError) as exc:
            messages.error(request, str(exc))
            return redirect("panel:voucher_generate")
        messages.success(request, f"{len(made)} codes created for {package.name}")
        return redirect(f"/admin/vouchers/?batch={made[0].batch}&state=unused" if made else "/admin/vouchers/")

    return render(
        request,
        "admin/voucher_generate.html",
        {
            "active_nav": "vouchers",
            "isp": isp,
            "packages_qs": packages_qs,
            "today": timezone.localdate().isoformat(),
        },
    )


@staff_required
@require_POST
def void(request, voucher_id):
    isp = current_isp(request)
    voucher = get_object_or_404(Voucher, pk=voucher_id, isp=isp)
    if voucher.state == "redeemed":
        messages.error(request, "That code has already been used and cannot be voided.")
    else:
        voucher.state = "void"
        voucher.save(update_fields=["state"])
        messages.success(request, f"{voucher.code} cancelled")
    return redirect(request.META.get("HTTP_REFERER", "/admin/vouchers/"))


@panel_required
@require_GET
def print_sheet(request):
    """A print-ready sheet: four codes per row, cut lines, no navigation."""
    isp = current_isp(request)
    batch = request.GET.get("batch") or ""
    qs = isp.vouchers.filter(state="unused").select_related("package")
    if batch:
        qs = qs.filter(batch=batch)
    codes = list(qs[:200])
    return render(
        request,
        "admin/voucher_sheet.html",
        {"isp": isp, "codes": codes, "batch": batch, "now": timezone.now()},
    )
