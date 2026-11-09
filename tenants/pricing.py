"""Owner-side catalog lookup that mirrors Apex's module pricing rules.

The owner app never writes the catalog — Apex owns it. This module only reads
the default active plan so a branch request can quote the same setup/yearly
fee the console will apply when it approves the branch.
"""

from decimal import Decimal

from .models import PricingPlan, Tenant

PLAN_NAME_MODULES = {
    "Base package": (),
    "Standard plan": ("Inventory", "HR Module"),
    "Basic plan": ("Inventory", "Sales Role"),
    "Complete plan": ("Inventory", "HR Module", "Sales Role"),
}

MODULE_NAMES = ("Inventory", "HR Module", "Sales Role")


def default_plan():
    return (
        PricingPlan.objects.filter(is_default=True, active=True).first()
        or PricingPlan.objects.filter(active=True).first()
    )


def plan_modules(plan):
    name = (plan.name or "").strip()
    if name in PLAN_NAME_MODULES:
        return PLAN_NAME_MODULES[name]
    return tuple(
        part.strip() for part in name.split("+") if part.strip() in MODULE_NAMES
    )


def tenant_modules(tenant):
    modules = []
    if tenant.module_inventory:
        modules.append("Inventory")
    if tenant.module_hr:
        modules.append("HR Module")
    if tenant.sales_role_enabled:
        modules.append("Sales Role")
    return tuple(modules)


def catalog_plan(tenant):
    wanted = set(tenant_modules(tenant))
    for plan in PricingPlan.objects.filter(active=True):
        if set(plan_modules(plan)) == wanted:
            return plan, True
    return default_plan(), False


def suggest_annual_fee(tenant, plan=None):
    if plan is None:
        plan, exact = catalog_plan(tenant)
        if plan is None:
            return None
        if exact:
            return plan.yearly_base_fee
    total = plan.yearly_base_fee
    if tenant.module_inventory:
        total += plan.inventory_yearly_fee
    if tenant.module_hr:
        total += plan.hr_yearly_fee
    if tenant.sales_role_enabled:
        total += plan.sales_role_yearly_fee
    return total


def suggest_fees(tenant):
    """``(plan, yearly, setup)`` for this module mix, or ``(None, None, None)``."""
    plan, exact = catalog_plan(tenant)
    if plan is None:
        return None, None, None
    yearly = plan.yearly_base_fee if exact else suggest_annual_fee(tenant, plan)
    return plan, yearly, plan.setup_fee


def fees_for_modules(module_inventory, module_hr, sales_role_enabled):
    probe = Tenant(
        module_inventory=bool(module_inventory),
        module_hr=bool(module_hr),
        sales_role_enabled=bool(sales_role_enabled),
    )
    return suggest_fees(probe)


def catalog():
    """Active plans with the module mix each one prices.

    The owner branch form previews the setup fee as switches change, and the
    console applies exactly this catalog after approval.
    """
    rows = []
    for plan in PricingPlan.objects.filter(active=True):
        modules = plan_modules(plan)
        rows.append(
            {
                "id": plan.id,
                "name": plan.name,
                "modules": list(modules),
                "module_inventory": "Inventory" in modules,
                "module_hr": "HR Module" in modules,
                "sales_role_enabled": "Sales Role" in modules,
                "setup_fee": str(plan.setup_fee.quantize(Decimal("0.01"))),
                "yearly_fee": str(plan.yearly_base_fee.quantize(Decimal("0.01"))),
                "yearly_base_fee": str(plan.yearly_base_fee.quantize(Decimal("0.01"))),
                "inventory_yearly_fee": str(
                    plan.inventory_yearly_fee.quantize(Decimal("0.01"))
                ),
                "hr_yearly_fee": str(plan.hr_yearly_fee.quantize(Decimal("0.01"))),
                "sales_role_yearly_fee": str(
                    plan.sales_role_yearly_fee.quantize(Decimal("0.01"))
                ),
                "is_default": plan.is_default,
            }
        )
    return rows

