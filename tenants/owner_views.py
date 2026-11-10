from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.timezone import localdate
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import StockLot, StockMovement
from sales.models import SalesAgent, SalesSettlement

from .models import (
    BranchPaymentLine,
    BranchPaymentRequest,
    BranchRequest,
    Tenant,
)
from .pricing import catalog, fees_for_modules
from .serializers import TenantSerializer

User = get_user_model()

MONEY = Decimal("0.00")
RANGES = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "180d": 180,
    "365d": 365,
}
RANGE_LABELS = {
    "1d": "Daily",
    "7d": "Last 7 days",
    "30d": "Monthly",
    "90d": "Quarterly",
    "180d": "Half a year",
    "365d": "Annually",
    "ytd": "This year",
    "all": "All time",
}
METHOD_LABELS = {"TELEBIRR": "Telebirr", "BANK": "Bank", "RECEIPT": "Receipt"}
MOVEMENT_LABELS = {
    "REGISTRATION": "Registered in",
    "STOCKOUT": "Issued to agents",
    "SALE": "Sold",
    "SALE_RETURN": "Returned",
    "PROMOTION": "Promotion",
    "PRICE_CHANGE": "Price change",
}


def money(value):
    return str((value or MONEY).quantize(Decimal("0.01")))


CHANNELS = ("TELEBIRR", "CBE", "OTHER")
CHANNEL_LABELS = {
    "TELEBIRR": "Telebirr",
    "CBE": "CBE",
    "BANK": "Bank",
    "CASH": "Cash",
    "OTHER": "Other",
}


def truthy(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def role_of(user):
    return getattr(getattr(user, "profile", None), "role", None)


class OwnerOnly(IsAuthenticated):
    """Every endpoint here belongs to a company owner and nothing else."""

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        return role_of(request.user) == "OWNER"


def owner_company(user):
    """The root tenant of the signed-in owner, or None.

    An OWNER profile is attached to the company root when VoloSync Admin
    creates it, but a branch created later also carries OWNER rows, so walk
    up to the root instead of trusting profile.tenant.
    """
    profile = getattr(user, "profile", None)
    tenant = getattr(profile, "tenant", None)
    if tenant is None:
        return None
    seen = set()
    while tenant.parent_id is not None:
        if tenant.id in seen:
            return None
        seen.add(tenant.id)
        tenant = Tenant.objects.filter(pk=tenant.parent_id).first()
        if tenant is None:
            return None
    return tenant


def company_ids(root):
    return [root.id, *Tenant.objects.filter(parent=root).values_list("id", flat=True)]


def tree(root):
    """The company and its branches, main branch first."""
    heal_main(root)
    members = list(Tenant.objects.filter(Q(pk=root.id) | Q(parent=root)))
    members.sort(key=lambda t: (not t.is_main, t.name.lower()))
    return members


def heal_main(root):
    """Guarantee exactly one main in the tree, the way Admin expects to read it."""
    members = list(Tenant.objects.filter(Q(pk=root.id) | Q(parent=root)))
    if not members:
        return
    mains = [t for t in members if t.is_main]
    if len(mains) == 1:
        return
    keep = mains[0] if mains else root
    for tenant in members:
        want = tenant.id == keep.id
        if tenant.is_main != want:
            tenant.is_main = want
            tenant.save(update_fields=["is_main"])


def set_main(root, chosen):
    """Promote one member of the tree and demote the rest, atomically."""
    with transaction.atomic():
        for tenant in Tenant.objects.filter(Q(pk=root.id) | Q(parent=root)):
            want = tenant.id == chosen.id
            if tenant.is_main != want:
                tenant.is_main = want
                tenant.save(update_fields=["is_main"])


def subscription_state(tenant):
    """Owner-facing renewal status; post-year billing covers year one from creation."""
    expires = tenant.subscription_expires_on
    if expires is None:
        expires = tenant.created_at.date() + timedelta(days=365)
    today = date.today()
    days_left = (expires - today).days
    if days_left < 0:
        state = "expired"
    elif days_left <= 30:
        state = "due"
    else:
        state = "active"
    return {
        "expires_on": expires.isoformat(),
        "paid_on": tenant.subscription_paid_on.isoformat() if tenant.subscription_paid_on else None,
        "days_left": days_left,
        "state": state,
        "transaction_id": tenant.subscription_transaction_id,
        "annual_fee": money(tenant.annual_fee),
    }


def period_from_request(request):
    """Resolve ?range= / ?start=&end= into a closed date window."""
    key = (request.query_params.get("range") or "30d").strip().lower()
    raw_start = (request.query_params.get("start") or "").strip()
    raw_end = (request.query_params.get("end") or "").strip()
    if raw_start and raw_end:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            return None, None, None
        if start > end:
            start, end = end, start
        labelled = key if request.query_params.get("range") and key in RANGE_LABELS else "custom"
        return start, end, labelled
    today = date.today()
    if key == "ytd":
        return today.replace(month=1, day=1), today, "ytd"
    if key == "all":
        return None, today, "all"
    days = RANGES.get(key)
    if days is None:
        return None, None, None
    return today - timedelta(days=days - 1), today, key


def windowed(queryset, start, end, field="created_at"):
    """Filter a DateTimeField down to a closed date window."""
    if start:
        queryset = queryset.filter(**{f"{field}__date__gte": start})
    if end:
        queryset = queryset.filter(**{f"{field}__date__lte": end})
    return queryset


def dated(queryset, start, end, field):
    """The same window for a DateField, which has no time part."""
    if start:
        queryset = queryset.filter(**{f"{field}__gte": start})
    if end:
        queryset = queryset.filter(**{f"{field}__lte": end})
    return queryset


def profiles_of(tenant):
    from user.models import Profile

    return list(
        Profile.objects.filter(tenant=tenant).select_related("user").order_by("role", "id")
    )


def manager_of(tenant):
    for profile in profiles_of(tenant):
        if profile.role == "MANAGER":
            return {
                "user_id": profile.user_id,
                "username": profile.user.username,
                "name": profile.user.get_full_name() or profile.user.username,
                "is_active": profile.user.is_active,
            }
    return None


def eligible_branches(root):
    """Branches the owner may pay for.

    Main + satellites with a real yearly fee; billing holds and zero-fee
    branches are excluded, and illustration accounts are skipped unless
    they are the main branch.
    """
    rows = []
    for member in tree(root):
        if member.billing_hold:
            continue
        if member.is_illustration and not member.is_main:
            continue
        fee = member.annual_fee or MONEY
        if fee <= 0:
            continue
        rows.append(
            {
                "id": member.id,
                "tin": member.tin,
                "name": member.name,
                "is_main": member.is_main,
                "kind": "main" if member.is_main else "satellite",
                "annual_fee": money(fee),
                "expires_on": (
                    member.subscription_expires_on.isoformat()
                    if member.subscription_expires_on
                    else None
                ),
                "state": subscription_state(member)["state"],
            }
        )
    return rows


def branch_request_row(row):
    """One owner-filed branch request, including the Apex rejection note."""
    return {
        "id": row.id,
        "parent": row.parent_id,
        "name": row.name,
        "tin": row.tin,
        "phone": row.phone,
        "address": row.address,
        "vat_rate": str(row.vat_rate),
        "module_inventory": row.module_inventory,
        "module_hr": row.module_hr,
        "sales_role_enabled": row.sales_role_enabled,
        "make_main": row.make_main,
        "manager_name": row.manager_name,
        "manager_username": row.manager_username,
        "setup_channel": row.setup_channel,
        "setup_channel_label": CHANNEL_LABELS.get(
            row.setup_channel, row.setup_channel.title()
        ),
        "setup_transaction_ref": row.setup_transaction_ref,
        "setup_amount": money(row.setup_amount),
        "status": row.status,
        "note": row.note,
        "decision_note": row.decision_note,
        "created_at": row.created_at,
        "decided_at": row.decided_at,
        "created_branch": row.created_branch_id,
        "created_branch_tin": row.created_branch.tin if row.created_branch else "",
    }


def branch_payment_row(row):
    """One multi-branch yearly payment, with per-branch decision lines."""
    lines = list(row.lines.all())
    approved = [line for line in lines if line.status == "APPROVED"]
    return {
        "id": row.id,
        "parent": row.parent_id,
        "transaction_ref": row.transaction_ref,
        "channel": row.channel,
        "channel_label": CHANNEL_LABELS.get(row.channel, row.channel.title()),
        "years": row.years,
        "amount": money(row.amount),
        "approved_amount": money(
            sum((line.amount for line in approved), MONEY)
        ),
        "status": row.status,
        "note": row.note,
        "decision_note": row.decision_note,
        "submitted_at": row.submitted_at,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
        "branches": [
            {
                "line_id": line.id,
                "tenant": line.tenant_id,
                "name": line.tenant.name,
                "tin": line.tenant.tin,
                "is_main": line.tenant.is_main,
                "amount": money(line.amount),
                "years": line.years,
                "status": line.status,
                "covers_until": line.covers_until,
                "approved_until": line.approved_until,
                "decision_note": line.decision_note,
            }
            for line in lines
        ],
        "approved_count": len(approved),
    }


def branch_row(tenant, root):
    """One branch with the numbers an owner compares them on."""
    lots = StockLot.objects.filter(tenant=tenant)
    approved = SalesSettlement.objects.filter(tenant=tenant, status="APPROVED")
    people = profiles_of(tenant)
    manager = next((p for p in people if p.role == "MANAGER"), None)
    totals = approved.aggregate(
        revenue=Sum("sales_amount"),
        units=Sum("sold_quantity"),
        returned=Sum("returned_quantity"),
        penalty_amount=Sum("penalty_amount"),
    )
    revenue = totals["revenue"] or MONEY
    penalty_total = totals["penalty_amount"] or MONEY
    collected_gross = revenue - penalty_total
    return {
        **TenantSerializer(tenant).data,
        "is_branch": tenant.parent_id is not None,
        "is_root": tenant.id == root.id,
        "kind": "main" if tenant.is_main else "satellite",
        "manager": (
            {
                "user_id": manager.user_id,
                "username": manager.user.username,
                "name": manager.user.get_full_name() or manager.user.username,
                "is_active": manager.user.is_active,
            }
            if manager
            else None
        ),
        "staff_count": len(people),
        "agent_count": SalesAgent.objects.filter(tenant=tenant).count(),
        "lots": lots.count(),
        "stock_value": money(lots.aggregate(v=Sum(F("remaining_quantity") * F("unit_cost")))["v"]),
        "registered_value": money(lots.aggregate(v=Sum(F("quantity") * F("unit_cost")))["v"]),
        "revenue": money(collected_gross),
        "units_sold": str(totals["units"] or Decimal("0")),
        "returned_units": str(totals["returned"] or Decimal("0")),
        "settlements": approved.count(),
        "pending": SalesSettlement.objects.filter(
            tenant=tenant, status__in=("PENDING_FINANCE", "PENDING_MANAGER")
        ).count(),
        "penalties": approved.filter(penalty=True).count(),
        "penalty_amount": money(totals["penalty_amount"]),
    }


class OwnerSessionView(APIView):
    """Who is signed in, which company they own, and how it is wired."""

    permission_classes = [OwnerOnly]

    def get(self, request):
        root = owner_company(request.user)
        if root is None:
            return Response(
                {"errors": {"message": "This owner is not attached to a company."}},
                status=400,
            )
        branches = Tenant.objects.filter(Q(pk=root.id) | Q(parent=root))
        return Response(
            {
                "data": {
                    "user": {
                        "id": request.user.id,
                        "username": request.user.username,
                        "name": request.user.get_full_name() or request.user.username,
                        "email": request.user.email,
                        "role": "OWNER",
                    },
                    "company": TenantSerializer(root).data,
                    "branches": [TenantSerializer(branch).data for branch in branches],
                    "counts": {
                        "branches": branches.count(),
                        "staff": Tenant.objects.filter(
                            Q(pk=root.id) | Q(parent=root)
                        ).aggregate(v=Count("staff"))["v"],
                        "lots": StockLot.objects.filter(
                            tenant_id__in=company_ids(root)
                        ).count(),
                    },
                    "subscription": subscription_state(root),
                }
            }
        )


class OwnerReportsView(APIView):
    """Consolidated company report with a period window and per-branch split."""

    permission_classes = [OwnerOnly]

    def get(self, request):
        root = owner_company(request.user)
        if root is None:
            return Response(
                {"errors": {"message": "This owner is not attached to a company."}},
                status=400,
            )

        start, end, key = period_from_request(request)
        if start is None and end is None and key not in ("all",):
            return Response(
                {"errors": {"message": "Use a known range, or a start and end date."}},
                status=400,
            )

        ids = company_ids(root)
        branch_id = (request.query_params.get("branch") or "").strip()
        scoped = None
        if branch_id:
            scoped = Tenant.objects.filter(pk=branch_id).filter(Q(parent=root) | Q(pk=root.id)).first()
            if scoped is None:
                return Response(
                    {"errors": {"message": "That branch is not in your company."}},
                    status=404,
                )
            ids = [scoped.id]

        lots = StockLot.objects.filter(tenant_id__in=ids)
        registered = lots.aggregate(v=Sum(F("quantity") * F("unit_cost")))["v"] or MONEY
        in_store = lots.aggregate(v=Sum(F("remaining_quantity") * F("unit_cost")))["v"] or MONEY
        registered_units = lots.aggregate(v=Sum("quantity"))["v"] or Decimal("0")
        remaining_units = lots.aggregate(v=Sum("remaining_quantity"))["v"] or Decimal("0")
        new_lots = dated(lots, start, end, "registration_date").count()
        # Every price is entered VAT-inclusive, so VAT is read out of the amount.
        inclusive_rate = root.vat_rate or Decimal("0")
        vat_share = inclusive_rate / (100 + inclusive_rate)
        vat_on_stock = in_store * vat_share

        approved = windowed(
            SalesSettlement.objects.filter(tenant_id__in=ids, status="APPROVED"),
            start,
            end,
        )
        all_settlements = SalesSettlement.objects.filter(tenant_id__in=ids)
        totals = approved.aggregate(
            revenue=Sum("sales_amount"),
            units=Sum("sold_quantity"),
            expected=Sum("expected_amount"),
            penalty_amount=Sum("penalty_amount"),
        )
        revenue = totals["revenue"] or MONEY
        expected_gross = totals["expected"] or MONEY
        penalty_total = totals["penalty_amount"] or MONEY
        collected_gross = revenue - penalty_total
        vat_collected = collected_gross * vat_share

        # Profit is cost against selling price for the units actually sold:
        # the lot carries the purchase cost, the assignment the selling price.
        # Selling price = unit_sales_at_assignment + changing_amount (matches volosync backend)
        sold_rows = approved.values_list(
            "sold_quantity",
            "assignment__lot__unit_cost",
            "assignment__unit_sales_at_assignment",
            "assignment__changing_amount",
        )
        sold_units = Decimal("0")
        cost_of_sold = Decimal("0")
        selling_of_sold = Decimal("0")
        for quantity, unit_cost, unit_sales, changing_amount in sold_rows:
            quantity = quantity or Decimal("0")
            sold_units += quantity
            cost_of_sold += quantity * (unit_cost or Decimal("0"))
            selling_of_sold += quantity * ((unit_sales or Decimal("0")) + (changing_amount or Decimal("0")))
        gross_profit = selling_of_sold - cost_of_sold
        average_cost = cost_of_sold / sold_units if sold_units else Decimal("0")
        average_selling = selling_of_sold / sold_units if sold_units else Decimal("0")

        movement_window = windowed(
            StockMovement.objects.filter(tenant_id__in=ids), start, end, field="at"
        )
        promotions = movement_window.filter(type="PROMOTION")
        movements = []
        for row in movement_window.values("type").annotate(
            lines=Count("id"), units=Sum(F("quantity_delta"))
        ):
            movements.append(
                {
                    "type": row["type"],
                    "label": MOVEMENT_LABELS.get(row["type"], row["type"].title()),
                    "lines": row["lines"],
                    "units": str(row["units"] or Decimal("0")),
                }
            )
        movements.sort(key=lambda row: -row["lines"])

        payment_methods = []
        for row in approved.values("payment_method").annotate(
            amount=Sum("sales_amount"), count=Count("id")
        ):
            method = row["payment_method"]
            payment_methods.append(
                {
                    "method": method,
                    "label": METHOD_LABELS.get(method, method.title()),
                    "amount": money(row["amount"]),
                    "settlements": row["count"],
                }
            )
        payment_methods.sort(key=lambda row: -Decimal(row["amount"]))

        agents = []
        agent_rows = approved.filter(
            assignment__isnull=False
        ).values(
            "assignment__sales_agent_id",
            "assignment__sales_agent__first_name",
            "assignment__sales_agent__last_name",
        ).annotate(
            revenue=Sum("sales_amount"),
            units=Sum("sold_quantity"),
            settlements=Count("id"),
            penalties=Count("id", filter=Q(penalty=True)),
            penalty_amount=Sum("penalty_amount"),
        )
        for row in agent_rows:
            first = row["assignment__sales_agent__first_name"] or ""
            last = row["assignment__sales_agent__last_name"] or ""
            agents.append(
                {
                    "id": row["assignment__sales_agent_id"],
                    "name": f"{first} {last}".strip() or "Unassigned",
                    "revenue": money(row["revenue"]),
                    "units": str(row["units"] or Decimal("0")),
                    "settlements": row["settlements"],
                    "penalties": row["penalties"],
                    "penalty_amount": money(row["penalty_amount"]),
                }
            )
        # Agents with no activity in the window still belong to the leaderboard.
        quiet = SalesAgent.objects.filter(tenant_id__in=ids).exclude(
            id__in=[agent["id"] for agent in agents]
        )
        for agent in quiet:
            agents.append(
                {
                    "id": agent.id,
                    "name": agent.full_name,
                    "revenue": money(Decimal("0")),
                    "units": "0",
                    "settlements": 0,
                    "penalties": 0,
                    "penalty_amount": money(Decimal("0")),
                }
            )
        agents.sort(key=lambda row: (-Decimal(row["revenue"]), -Decimal(row["units"]), Decimal(row["penalty_amount"])))
        for index, agent in enumerate(agents, start=1):
            agent["rank"] = index

        top_items = []
        for row in approved.filter(
            assignment__lot__isnull=False
        ).values(
            "assignment__lot__item__name", "assignment__lot__item__category"
        ).annotate(
            revenue=Sum("sales_amount"), units=Sum("sold_quantity")
        ):
            top_items.append(
                {
                    "name": row["assignment__lot__item__name"],
                    "category": row["assignment__lot__item__category"] or "Uncategorised",
                    "revenue": money(row["revenue"]),
                    "units": str(row["units"] or Decimal("0")),
                }
            )
        top_items.sort(key=lambda row: -Decimal(row["revenue"]))

        # MySQL cannot bucket by timezone here, so the window is grouped in Python.
        bucketed_by_day = bool(start and end and (end - start).days <= 62)
        daily = {}
        for stamp, amount, units in approved.values_list(
            "created_at", "sales_amount", "sold_quantity"
        ):
            day = localdate(stamp)
            if bucketed_by_day:
                label = day.strftime("%d %b")
            else:
                day = day.replace(day=1)
                label = day.strftime("%b")
            entry = daily.setdefault(day, {"label": label, "revenue": Decimal("0"), "units": Decimal("0")})
            entry["revenue"] += amount or Decimal("0")
            entry["units"] += units or Decimal("0")
        trend = [
            {
                "label": entry["label"],
                "date": day.isoformat(),
                "revenue": money(entry["revenue"]),
                "units": str(entry["units"]),
            }
            for day, entry in sorted(daily.items())
        ][-12:]

        comparison = [branch_row(member, root) for member in tree(root)]

        main_tenant = next((row for row in tree(root) if row.is_main), None)
        main_row = branch_row(main_tenant, root) if main_tenant else None
        issued = movement_window.filter(type="STOCKOUT").aggregate(
            v=Sum(F("quantity_delta") * -1)
        )["v"] or Decimal("0")
        returned = approved.aggregate(v=Sum("returned_quantity"))["v"] or Decimal("0")

        data = {
            "tenant": TenantSerializer(root).data,
            "company": {
                "name": root.name,
                "tin": root.tin,
                "vat_rate": str(root.vat_rate),
                "subscription_expires_on": root.subscription_expires_on,
            },
            "period": {
                "key": key,
                "label": RANGE_LABELS.get(key, "Custom dates"),
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
            "scope": {
                "branch": scoped.id if scoped else None,
                "branch_name": scoped.name if scoped else "All branches",
            },
            "subscription": subscription_state(root),
            "overview": {
                "registered_value": money(registered),
                "in_store_value": money(in_store),
                "stocked_out_value": money(registered - in_store),
                "registered_units": str(registered_units),
                "in_store_units": str(remaining_units),
                "stocked_out_units": str(registered_units - remaining_units),
                "vat_on_stock": money(vat_on_stock),
                "revenue": money(collected_gross),
                "expected_gross": money(expected_gross),
                "units_sold": str(totals["units"] or Decimal("0")),
                "returned_units": str(totals["returned"] or Decimal("0")),
                "vat_collected": money(vat_collected),
                "penalties": approved.filter(penalty=True).count(),
                "penalty_amount": money(totals["penalty_amount"]),
                "settlements": approved.count(),
                "lots": lots.count(),
                "new_lots": new_lots,
                "issued_in_period": str(issued),
                "returned_in_period": str(returned),
                "cost_of_sold": money(cost_of_sold),
                "selling_of_sold": money(selling_of_sold),
                "gross_profit": money(gross_profit),
                "profit_margin": str(
                    (gross_profit / selling_of_sold * 100).quantize(Decimal("0.01"))
                    if selling_of_sold
                    else Decimal("0")
                ),
                "average_unit_cost": money(average_cost),
                "average_unit_selling": money(average_selling),
            },
            "pipeline": {
                "pending_finance": all_settlements.filter(status="PENDING_FINANCE").count(),
                "pending_manager": all_settlements.filter(status="PENDING_MANAGER").count(),
                "rejected": all_settlements.filter(status="REJECTED").count(),
                "expected_amount": money(expected_gross),
                "variance": money(collected_gross - expected_gross),
            },
            "payment_methods": payment_methods,
            "promotions": {
                "lines": promotions.count(),
                "units": str(promotions.aggregate(v=Sum(F("quantity_delta") * -1))["v"] or Decimal("0")),
            },
            "movements": movements,
            "agents": agents,
            "top_items": top_items[:8],
            "trend": trend,
            "branches": [row for row in comparison if row["is_branch"] or row["is_main"] or row["is_root"]],
            "main_branch": main_row,
        }
        return Response({"data": data})


class BranchListView(APIView):
    """GET the branch directory, POST a new branch with its manager login."""

    permission_classes = [OwnerOnly]

    def get(self, request):
        root = owner_company(request.user)
        if root is None:
            return Response(
                {"errors": {"message": "This owner is not attached to a company."}},
                status=400,
            )
        rows = [branch_row(member, root) for member in tree(root)]
        branches = [row for row in rows if row["is_branch"] or row["is_main"] or row["is_root"]]
        requests = BranchRequest.objects.filter(parent=root).select_related(
            "created_branch", "decided_by"
        )[:50]
        payment_requests = BranchPaymentRequest.objects.filter(
            parent=root
        ).prefetch_related("lines__tenant")[:50]
        return Response(
            {
                "data": {
                    "company": TenantSerializer(root).data,
                    "rows": rows,
                    "branches": branches,
                    "subscription": subscription_state(root),
                    "requests": [branch_request_row(row) for row in requests],
                    "payment_requests": [
                        branch_payment_row(row) for row in payment_requests
                    ],
                    "eligible_branches": eligible_branches(root),
                    "catalog": catalog(),
                    "summary": {
                        "branches": len(branches),
                        "with_manager": sum(1 for row in branches if row["manager"]),
                        "without_manager": sum(1 for row in branches if not row["manager"]),
                        "active": sum(
                            1
                            for row in branches
                            if row["manager"] and row["manager"]["is_active"]
                        ),
                        "main": next(row["name"] for row in rows if row["is_main"]),
                        "pending_requests": sum(
                            1 for row in requests if row.status == "PENDING"
                        ),
                    },
                }
            }
        )

    def post(self, request):
        root = owner_company(request.user)
        if root is None:
            return Response(
                {"errors": {"message": "This owner is not attached to a company."}},
                status=400,
            )

        name = (request.data.get("name") or "").strip()
        tin = (request.data.get("tin") or "").strip()
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        errors = {}
        if not name:
            errors["name"] = "Give the branch a name."
        elif Tenant.objects.filter(name__iexact=name).exists():
            errors["name"] = "A branch with that name already exists."
        if not tin:
            errors["tin"] = "Every branch runs on its own TIN."
        elif tin == root.tin:
            errors["tin"] = "That is the company TIN — a branch needs its own."
        elif Tenant.objects.filter(tin=tin).exists():
            errors["tin"] = "That TIN is already registered."
        elif BranchRequest.objects.filter(tin=tin, status="PENDING").exists():
            errors["tin"] = "A pending branch request already uses that TIN."
        if username or password:
            if len(username) < 3:
                errors["username"] = "Use at least 3 characters."
            if len(password) < 6:
                errors["password"] = "Use a password of at least 6 characters."
            elif User.objects.filter(username__iexact=username).exists():
                errors["username"] = "That username is already taken."
            elif BranchRequest.objects.filter(
                manager_username__iexact=username, status="PENDING"
            ).exists():
                errors["username"] = "Another pending branch request uses that username."

        channel = (request.data.get("setup_channel") or "TELEBIRR").upper()
        if channel not in CHANNELS:
            errors["setup_channel"] = "Pick Telebirr, CBE or Other."

        vat_rate = root.vat_rate
        vat_raw = request.data.get("vat_rate")
        if vat_raw not in (None, ""):
            try:
                vat_rate = Decimal(str(vat_raw))
            except (InvalidOperation, ValueError):
                errors["vat_rate"] = "Use a number between 0 and 100."
            else:
                if vat_rate < 0 or vat_rate > 100:
                    errors["vat_rate"] = "Use a number between 0 and 100."
        transaction_ref = (
            request.data.get("setup_transaction_ref")
            or request.data.get("transaction_ref")
            or request.data.get("transaction_id")
            or ""
        ).strip()
        if not transaction_ref:
            errors["setup_transaction_ref"] = "Enter the setup payment transaction number."
        elif len(transaction_ref) > 60:
            errors["setup_transaction_ref"] = "Keep the transaction number under 60 characters."

        if errors:
            return Response({"errors": errors}, status=400)

        module_inventory = (
            truthy(request.data["module_inventory"])
            if "module_inventory" in request.data
            else root.module_inventory
        )
        module_hr = (
            truthy(request.data["module_hr"])
            if "module_hr" in request.data
            else root.module_hr
        )
        sales_role_enabled = (
            truthy(request.data["sales_role_enabled"])
            if "sales_role_enabled" in request.data
            else root.sales_role_enabled
        )
        _plan, _yearly, setup = fees_for_modules(
            module_inventory, module_hr, sales_role_enabled
        )
        row = BranchRequest.objects.create(
            parent=root,
            name=name,
            tin=tin,
            phone=(request.data.get("phone") or "").strip(),
            address=(request.data.get("address") or "").strip(),
            vat_rate=vat_rate,
            module_inventory=module_inventory,
            module_hr=module_hr,
            sales_role_enabled=sales_role_enabled,
            make_main=False,  # the main branch is created with the tenant
            manager_name=(
                request.data.get("manager_name")
                or request.data.get("manager_first_name")
                or ""
            ).strip(),
            manager_username=username,
            manager_password_hash=make_password(password) if password else "",
            setup_channel=channel,
            setup_transaction_ref=transaction_ref,
            setup_amount=setup if setup is not None else Decimal("0.00"),
            note=(request.data.get("note") or "").strip()[:255],
            created_by=request.user,
        )
        return Response(
            {
                "data": branch_request_row(row),
                "message": f"{name} was sent to Apex for approval.",
            },
            status=201,
        )


class BranchDetailView(APIView):
    """Customise one branch: details, main or satellite, manager password, removal."""

    permission_classes = [OwnerOnly]

    def _branch(self, request, pk):
        root = owner_company(request.user)
        if root is None:
            return None, None
        branch = Tenant.objects.filter(pk=pk, parent=root).select_related("parent").first()
        return root, branch

    def patch(self, request, pk):
        root, branch = self._branch(request, pk)
        if branch is None:
            return Response({"errors": {"message": "Not found."}}, status=404)

        errors = {}
        vat = request.data.get("vat_rate")
        if vat is not None:
            try:
                value = Decimal(str(vat))
            except Exception:
                value = None
            if value is None or value < 0 or value > 100:
                errors["vat_rate"] = "Enter a VAT rate between 0 and 100."
        name = request.data.get("name")
        if name is not None:
            name = name.strip()
            if not name:
                errors["name"] = "Give the branch a name."
            elif Tenant.objects.filter(name__iexact=name).exclude(pk=branch.pk).exists():
                errors["name"] = "Another branch already uses that name."
        tin = request.data.get("tin")
        if tin is not None:
            tin = tin.strip()
            if not tin:
                errors["tin"] = "Every branch runs on its own TIN."
            elif tin == root.tin:
                errors["tin"] = "That is the company TIN."
            elif Tenant.objects.filter(tin__iexact=tin).exclude(pk=branch.pk).exists():
                errors["tin"] = "That TIN is already registered."

        if errors:
            return Response({"errors": errors}, status=400)

        wants_main = None
        if "is_main" in request.data:
            wants_main = str(request.data["is_main"]).lower() in ("1", "true", "yes")
            if wants_main is False and branch.is_main:
                return Response(
                    {
                        "errors": {
                            "is_main": "The company always needs one main branch — promote another one first."
                        }
                    },
                    status=400,
                )

        with transaction.atomic():
            for field in ("name", "tin", "phone", "address"):
                if request.data.get(field) is not None:
                    setattr(branch, field, str(request.data[field]).strip())
            if request.data.get("vat_rate") is not None:
                branch.vat_rate = Decimal(str(request.data["vat_rate"]))
            branch.save()
            if wants_main:
                set_main(root, branch)
                branch.refresh_from_db()

        return Response({"data": branch_row(branch, root), "message": "Branch updated."})

    def delete(self, request, pk):
        root, branch = self._branch(request, pk)
        if branch is None:
            return Response({"errors": {"message": "Not found."}}, status=404)

        lots = StockLot.objects.filter(tenant=branch).count()
        settlements = SalesSettlement.objects.filter(tenant=branch).count()
        if lots or settlements:
            return Response(
                {
                    "errors": {
                        "message": (
                            f"{branch.name} still holds {lots} voucher(s) and {settlements} settlement(s). "
                            "Move or clear them before deleting the branch."
                        )
                    }
                },
                status=400,
            )

        with transaction.atomic():
            was_main = branch.is_main
            user_ids = list(branch.staff.values_list("user_id", flat=True))
            branch.delete()
            # A login that lost its only profile is useless, so drop it too.
            from user.models import Profile

            still_attached = Profile.objects.filter(user_id__in=user_ids).values_list(
                "user_id", flat=True
            )
            User.objects.filter(id__in=user_ids).exclude(id__in=still_attached).delete()
            if was_main:
                remaining = Tenant.objects.filter(parent=root).order_by("id").first()
                set_main(root, remaining or root)

        return Response({"data": {"deleted": True}, "message": "Branch removed."})


class BranchManagerPasswordView(APIView):
    """Reset the branch manager's password from the owner app."""

    permission_classes = [OwnerOnly]

    def post(self, request, pk):
        root = owner_company(request.user)
        branch = (
            Tenant.objects.filter(pk=pk, parent=root).first() if root else None
        )
        if branch is None:
            return Response({"errors": {"message": "Not found."}}, status=404)

        password = request.data.get("password") or ""
        if len(password) < 6:
            return Response(
                {"errors": {"password": "Use a password of at least 6 characters."}},
                status=400,
            )
        manager = manager_of(branch)
        if not manager:
            return Response(
                {"errors": {"message": f"{branch.name} has no manager login yet."}},
                status=400,
            )
        user = User.objects.filter(pk=manager["user_id"]).first()
        user.set_password(password)
        user.save(update_fields=["password"])
        from user.models import Profile

        Profile.objects.filter(user=user).update(must_change_password=False)
        return Response({"data": {"reset": True}, "message": f"Password reset for {user.username}."})


class SubscriptionEntryView(APIView):
    """Owner pays the yearly fee for one or more branches at once.

    GET  — eligible branches plus any pending multi-branch request.
    POST — {branches: [id...], channel, transaction_ref, years} files one
           request whose lines are approved branch-by-branch by Apex.
    """

    permission_classes = [OwnerOnly]

    def get(self, request):
        root = owner_company(request.user)
        if root is None:
            return Response(
                {"errors": {"message": "This owner is not attached to a company."}},
                status=400,
            )
        pending = BranchPaymentRequest.objects.filter(
            parent=root, status="PENDING"
        ).prefetch_related("lines__tenant")
        return Response(
            {
                "data": {
                    "company": TenantSerializer(root).data,
                    "subscription": subscription_state(root),
                    "branches": eligible_branches(root),
                    "pending_requests": [branch_payment_row(row) for row in pending],
                }
            }
        )

    def post(self, request):
        root = owner_company(request.user)
        if root is None:
            return Response(
                {"errors": {"message": "This owner is not attached to a company."}},
                status=400,
            )

        eligible = {row["id"]: row for row in eligible_branches(root)}
        raw_ids = (
            request.data.get("branches")
            if "branches" in request.data
            else request.data.get("branch_ids")
        )
        if raw_ids is None:
            # Legacy shape: one transaction for the company itself.
            raw_ids = [root.id]
        if not isinstance(raw_ids, (list, tuple)):
            raw_ids = [raw_ids]

        errors = {}
        chosen = []
        for raw in raw_ids:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                errors["branches"] = "Pick branches from your company list."
                break
            if value not in eligible:
                errors["branches"] = (
                    "One of the selected branches is on hold, exempt, or has no yearly fee."
                )
                break
            if value not in chosen:
                chosen.append(value)
        if not chosen and "branches" not in errors:
            errors["branches"] = "Select at least one branch to renew."

        transaction_ref = (
            request.data.get("transaction_ref")
            or request.data.get("transaction_id")
            or ""
        ).strip()
        if not transaction_ref:
            errors["transaction_ref"] = "Enter the yearly payment transaction number."
        elif len(transaction_ref) > 60:
            errors["transaction_ref"] = "Keep the transaction number under 60 characters."

        channel = (request.data.get("channel") or "TELEBIRR").upper()
        if channel not in CHANNELS:
            errors["channel"] = "Pick Telebirr, CBE or Other."

        years = request.data.get("years", 1)
        try:
            years = int(years)
        except (TypeError, ValueError):
            years = 0
        if years < 1 or years > 5:
            errors["years"] = "Renew between 1 and 5 years."

        # A branch already waiting on a decision cannot be paid twice.
        if chosen:
            already = set(
                BranchPaymentLine.objects.filter(
                    tenant_id__in=chosen,
                    status="PENDING",
                    request__status="PENDING",
                ).values_list("tenant_id", flat=True)
            )
            if already:
                names = ", ".join(
                    eligible[value]["name"] for value in chosen if value in already
                )
                errors["branches"] = f"{names} already has a pending payment request."

        if errors:
            return Response({"errors": errors}, status=400)

        today = date.today()
        rows = [eligible[value] for value in chosen]
        total = MONEY
        with transaction.atomic():
            payment_request = BranchPaymentRequest.objects.create(
                parent=root,
                transaction_ref=transaction_ref,
                channel=channel,
                years=years,
                amount=MONEY,
                status="PENDING",
                note=(request.data.get("note") or "").strip()[:255],
                submitted_by=request.user,
                submitted_at=timezone.now(),
            )
            for row in rows:
                member = Tenant.objects.get(pk=row["id"])
                fee = member.annual_fee or MONEY
                line_amount = fee * years
                base = member.subscription_expires_on or today
                if base < today:
                    base = today
                BranchPaymentLine.objects.create(
                    request=payment_request,
                    tenant=member,
                    amount=line_amount,
                    years=years,
                    status="PENDING",
                    covers_until=base + timedelta(days=365 * years),
                )
                total += line_amount
            payment_request.amount = total
            payment_request.save(update_fields=["amount"])

        payment_request.refresh_from_db()
        return Response(
            {
                "data": {
                    "request": branch_payment_row(payment_request),
                    "branches": eligible_branches(root),
                    "subscription": subscription_state(root),
                },
                "message": (
                    f"Yearly payment for {len(rows)} branch"
                    f"{'es' if len(rows) != 1 else ''} sent to Apex."
                ),
            },
            status=201,
        )
