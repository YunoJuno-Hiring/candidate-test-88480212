"""Billing models.

General flow:
BillingRun indicates when someone submitted a request, for auditability.

Invoice(s) are created synchronously when a run is triggered, saving any necessary data,
which will then get used for sending emails.

InvoiceLines are used to link up to specific timesheet entries for traceability and
to ensure we don't bill multiple times. Defined here rather than on the timesheet for domain separation.

InvoiceDelivery created synchronously and one delivery attempt is made.
Any retries are processed async to handle unreliable emails.
One email for company and one for freelancer.
"""
import datetime

from django.conf import settings
from django.db import models

from accounts.models import Company, Freelancer
from contracts.models import Contract, TimesheetEntry


class BillingRun(models.Model):
    """Audit record of one accepted "Run billing" request."""

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='billing_runs')
    period = models.DateField(help_text='First day of the billed month.')
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='billing_runs'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    invoices_generated = models.PositiveIntegerField(default=0)
    invoices_existing = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period__day=1), name='billing_run_period_first_of_month'
            ),
        ]

    def __str__(self) -> str:
        return f'Run #{self.pk} — {self.company} {self.period:%Y-%m}'


class Invoice(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name='invoices')
    freelancer = models.ForeignKey(Freelancer, on_delete=models.PROTECT, related_name='invoices')
    billing_run = models.ForeignKey(BillingRun, on_delete=models.PROTECT, related_name='invoices')
    period = models.DateField(help_text='First day of the billed month.')

    reference = models.CharField(max_length=64, unique=True)  # Human-readable as (period-company-freelancer)

    total = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    # Snapshots at issue time.
    company_name = models.CharField(max_length=255)
    company_billing_email = models.EmailField()
    freelancer_name = models.CharField(max_length=255)

    class Meta:
        ordering = ['-period', 'freelancer_name', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'freelancer', 'period'],
                name='invoice_unique_company_freelancer_period',
            ),
            models.CheckConstraint(
                condition=models.Q(period__day=1), name='invoice_period_first_of_month'
            ),
        ]
        indexes = [
            models.Index(fields=['company', 'period'], name='invoice_company_period_idx'),  # Used in ?month= list query
        ]

    def __str__(self) -> str:
        return self.reference

    @staticmethod
    def build_reference(*, period: datetime.date, company_id: int, freelancer_id: int) -> str:
        return f'INV-{period:%Y%m}-{company_id:04d}-{freelancer_id:04d}'


class InvoiceLine(models.Model):
    """One billed timesheet entry. OneToOne to TimesheetEntry to guarantee that an entry is billed at most once."""

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='lines')
    timesheet_entry = models.OneToOneField(
        TimesheetEntry, on_delete=models.PROTECT, related_name='invoice_line'
    )
    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name='invoice_lines')

    # Snapshots at issue time.
    date = models.DateField()
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    daily_rate = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ['date', 'id']

    def __str__(self) -> str:
        return f'{self.invoice.reference} {self.date} {self.hours}h'


class InvoiceDelivery(models.Model):
    """Outbox row: one per recipient per invoice.

    Rows are created with the invoice: `pending`, or already `failed` if the
    recipient has no email address. Senders update while sending.
    Delivery is at-least-once.
    """

    RECIPIENT_COMPANY = 'company'
    RECIPIENT_FREELANCER = 'freelancer'
    RECIPIENT_CHOICES = [
        (RECIPIENT_COMPANY, 'Company'),
        (RECIPIENT_FREELANCER, 'Freelancer'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_SENDING = 'sending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_SENDING, 'Sending'),
        (STATUS_SENT, 'Sent'),
        (STATUS_FAILED, 'Failed'),
    ]

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='deliveries')
    recipient_type = models.CharField(max_length=10, choices=RECIPIENT_CHOICES)

    email = models.EmailField(blank=True)  # Snapshotted email address, to support retries

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['invoice_id', 'recipient_type']
        verbose_name_plural = 'invoice deliveries'
        constraints = [
            models.UniqueConstraint(
                fields=['invoice', 'recipient_type'],
                name='invoice_delivery_unique_recipient',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'last_attempt_at'], name='invoice_delivery_status_idx'),  # For retries
        ]

    def __str__(self) -> str:
        return f'{self.invoice.reference} → {self.recipient_type} ({self.status})'
