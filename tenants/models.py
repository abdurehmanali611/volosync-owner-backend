from decimal import Decimal

from django.conf import settings
from django.db import models


class Tenant(models.Model):
    MODULE_CHOICES = (("INVENTORY", "Inventory"), ("HR", "HR"))

    name = models.CharField(max_length=255)
    tin = models.CharField(max_length=50, unique=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    logo_url = models.URLField(blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    module_inventory = models.BooleanField(default=True)
    module_hr = models.BooleanField(default=True)
    sales_role_enabled = models.BooleanField(default=False)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="branches"
    )
    is_main = models.BooleanField(default=False)
    subscription_expires_on = models.DateField(null=True, blank=True)
    subscription_transaction_id = models.CharField(max_length=60, blank=True)
    subscription_paid_on = models.DateField(null=True, blank=True)
    # Billing columns owned by the schema in VoloSync Admin; declared so an
    # INSERT satisfies their NOT NULL constraints and the owner can read the
    # eligibility flags the yearly payment picker needs.
    subscription_note = models.CharField(max_length=255, blank=True)
    annual_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    setup_fee = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    is_illustration = models.BooleanField(default=False)
    billing_hold = models.BooleanField(default=False)
    account_status = models.CharField(max_length=20, default="ACTIVE")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Three backends share this one MySQL schema and Admin owns the DDL.
        managed = False
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_branch(self):
        return self.parent_id is not None


class BranchRequest(models.Model):
    """Owner-side mirror of the admin-owned branch request table."""

    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    )

    parent = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="branch_requests"
    )
    name = models.CharField(max_length=255)
    tin = models.CharField(max_length=50)
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("15.00")
    )
    module_inventory = models.BooleanField(default=True)
    module_hr = models.BooleanField(default=True)
    sales_role_enabled = models.BooleanField(default=False)
    make_main = models.BooleanField(default=False)
    manager_name = models.CharField(max_length=120, blank=True)
    manager_username = models.CharField(max_length=150, blank=True)
    manager_password_hash = models.CharField(max_length=128, blank=True)
    setup_channel = models.CharField(max_length=20, blank=True, default="OTHER")
    setup_transaction_ref = models.CharField(max_length=60, blank=True)
    setup_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="branch_requests_created",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="branch_request_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    created_branch = models.ForeignKey(
        Tenant,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_branch_request",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        ordering = ["-created_at", "-id"]


class BranchPaymentRequest(models.Model):
    """Owner-side mirror of the admin-owned multi-branch payment request."""

    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("PARTIALLY_APPROVED", "Partially approved"),
        ("REJECTED", "Rejected"),
    )

    parent = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="branch_payment_requests"
    )
    transaction_ref = models.CharField(max_length=60)
    channel = models.CharField(max_length=20, blank=True, default="OTHER")
    years = models.PositiveSmallIntegerField(default=1)
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    note = models.CharField(max_length=255, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="branch_payment_submissions",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="branch_payment_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        ordering = ["-created_at", "-id"]


class BranchPaymentLine(models.Model):
    """Owner-side mirror of one branch inside a payment request."""

    STATUS = (
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("EXCLUDED", "Excluded"),
        ("REJECTED", "Rejected"),
    )

    request = models.ForeignKey(
        BranchPaymentRequest, on_delete=models.CASCADE, related_name="lines"
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="branch_payment_lines"
    )
    amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    years = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    covers_until = models.DateField()
    approved_until = models.DateField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        ordering = ["tenant__name", "id"]


class PricingPlan(models.Model):
    """Read-only mirror of Apex's pricing catalog (admin owns the DDL)."""

    name = models.CharField(max_length=100)
    setup_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    yearly_base_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    inventory_yearly_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    hr_yearly_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    sales_role_yearly_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    description = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_default = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        managed = False
        ordering = ["-is_default", "sort_order", "name"]
