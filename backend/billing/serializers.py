import datetime
import re

from django.utils import timezone
from rest_framework import serializers

from billing.delivery import aggregate_delivery_status
from billing.models import BillingRun, Invoice, InvoiceDelivery, InvoiceLine

CURRENCY = 'GBP'
RUN_STATUS_COMPLETED = 'completed'

_MONTH_RE = re.compile(r'(\d{4})-(\d{2})', re.ASCII)


class MonthField(serializers.Field):
    """A month provided as 'YYYY-MM'. Serialised to the first day of that month in Python."""

    default_error_messages = {'invalid': "Month must be in 'YYYY-MM' format."}

    def to_internal_value(self, data: object) -> datetime.date:
        match = _MONTH_RE.fullmatch(data) if isinstance(data, str) else None
        if match is None:
            self.fail('invalid')
        try:
            return datetime.date(int(match[1]), int(match[2]), 1)
        except ValueError:  # month outside 01-12, or year 0000
            self.fail('invalid')

    def to_representation(self, value: datetime.date) -> str:
        return f'{value:%Y-%m}'


class BillingRunRequestSerializer(serializers.Serializer):
    """Request body for POST /api/billing/runs/."""

    month = MonthField()

    def validate_month(self, period: datetime.date) -> datetime.date:
        # Only completed months can be billed: billing the current month would lock out its later entries.
        if period >= timezone.localdate().replace(day=1):
            raise serializers.ValidationError(
                f'Only completed months can be billed. {period:%B %Y} has not finished yet.'
            )
        return period


class InvoiceListQuerySerializer(serializers.Serializer):
    """Query parameters for GET /api/invoices/."""

    month = MonthField(required=False)


class BillingRunSerializer(serializers.ModelSerializer):
    """Response body for POST /api/billing/runs/."""

    run_id = serializers.IntegerField(source='pk', read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = BillingRun
        fields = ['run_id', 'invoices_generated', 'invoices_existing', 'status']
        read_only_fields = fields

    def get_status(self, run: BillingRun) -> str:
        # Runs are synchronous: by the time a run is serialised it has completed, so always return that.
        return RUN_STATUS_COMPLETED


class InvoiceSummarySerializer(serializers.ModelSerializer):
    period = MonthField(read_only=True)
    currency = serializers.SerializerMethodField()
    delivery_status = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'reference', 'freelancer', 'freelancer_name', 'company_name', 'period',
            'currency', 'total', 'created_at', 'delivery_status',
        ]
        read_only_fields = fields

    def get_currency(self, invoice: Invoice) -> str:
        # Not really necessary as contract does not define a currency, but in real life we'd want this.
        return CURRENCY

    def get_delivery_status(self, invoice: Invoice) -> str:
        # Return state of the batch of emails.
        return aggregate_delivery_status(invoice.deliveries.all())


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = ['id', 'date', 'hours', 'daily_rate', 'amount', 'contract', 'timesheet_entry']
        read_only_fields = fields


class InvoiceDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceDelivery
        fields = [
            'recipient_type', 'email', 'status', 'attempts', 'last_error', 'last_attempt_at',
            'sent_at',
        ]
        read_only_fields = fields


class InvoiceDetailSerializer(InvoiceSummarySerializer):
    lines = InvoiceLineSerializer(many=True, read_only=True)
    deliveries = InvoiceDeliverySerializer(many=True, read_only=True)

    class Meta(InvoiceSummarySerializer.Meta):
        fields = InvoiceSummarySerializer.Meta.fields + [
            'billing_run', 'company_billing_email', 'lines', 'deliveries',
        ]
        read_only_fields = fields
