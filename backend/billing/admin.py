from django.contrib import admin
from django.db.models import Model
from django.http import HttpRequest

from billing.models import BillingRun, Invoice, InvoiceDelivery, InvoiceLine


class ReadOnlyAdmin(admin.ModelAdmin):
    """Issued billing records are immutable; the admin is for inspection
    (e.g. finance checking failed deliveries), not editing."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    fields = ['date', 'hours', 'daily_rate', 'amount', 'contract', 'timesheet_entry']
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


class InvoiceDeliveryInline(admin.TabularInline):
    model = InvoiceDelivery
    fields = ['recipient_type', 'email', 'status', 'attempts', 'last_attempt_at', 'sent_at', 'last_error']
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False


@admin.register(BillingRun)
class BillingRunAdmin(ReadOnlyAdmin):
    list_display = ['id', 'company', 'period', 'triggered_by', 'created_at', 'invoices_generated', 'invoices_existing']
    list_filter = ['company', 'period']


@admin.register(Invoice)
class InvoiceAdmin(ReadOnlyAdmin):
    list_display = ['reference', 'company_name', 'freelancer_name', 'period', 'total', 'created_at']
    list_filter = ['company', 'period']
    search_fields = ['reference', 'freelancer_name', 'company_name']
    inlines = [InvoiceLineInline, InvoiceDeliveryInline]


@admin.register(InvoiceLine)
class InvoiceLineAdmin(ReadOnlyAdmin):
    list_display = ['invoice', 'date', 'hours', 'daily_rate', 'amount']


@admin.register(InvoiceDelivery)
class InvoiceDeliveryAdmin(ReadOnlyAdmin):
    list_display = ['invoice', 'recipient_type', 'email', 'status', 'attempts', 'last_attempt_at', 'sent_at']
    list_filter = ['status', 'recipient_type']
    search_fields = ['invoice__reference', 'email']
