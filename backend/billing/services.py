"""Monthly billing run.

Invoice generation is synchronous and happens inside one transaction.

Email delivery is not: the run only writes InvoiceDelivery rows (the outbox)
and makes one send attempt after commit. Anything not sent there is picked up
by `manage.py send_invoice_emails`.
"""
import datetime
import logging
from decimal import ROUND_HALF_UP, Decimal
from itertools import groupby
from operator import attrgetter

from django.contrib.auth.models import User
from django.db import transaction

from accounts.models import Company
from contracts.models import TimesheetEntry

from billing.delivery import new_delivery, send_deliveries
from billing.models import BillingRun, Invoice, InvoiceDelivery, InvoiceLine

logger = logging.getLogger(__name__)

HOURS_PER_DAY = Decimal('8')
PENNY = Decimal('0.01')

def line_amount(hours: Decimal, daily_rate: Decimal) -> Decimal:
    """Bill hours proportionally against an 8-hour day, rounded to the penny."""
    return (hours / HOURS_PER_DAY * daily_rate).quantize(PENNY, rounding=ROUND_HALF_UP)


def run_billing(*, company: Company, triggered_by: User, period: datetime.date) -> BillingRun:
    """Create one invoice per freelancer with approved entries in `period`.

    Idempotent per (company, freelancer, period): freelancers that already have
    an invoice for the period are counted as existing and not touched.

    Entries already on an invoice are never billed again, even if they have
    since been edited into this period.
    """
    with transaction.atomic():
        # Ensure multiple triggers do not try and race each other. Successive attempts wait.
        company = Company.objects.select_for_update().get(pk=company.pk)

        entries = (
            TimesheetEntry.objects.filter(
                status=TimesheetEntry.STATUS_APPROVED,
                date__year=period.year,
                date__month=period.month,
                contract__company=company,
                hours__gt=0,  # Skip 0 hours to avoid empty lines.
                invoice_line__isnull=True,  # Each entry is billed at most once.
            )
            .select_related('contract__freelancer__user')
            .order_by('contract__freelancer_id', 'date', 'id')
        )
        already_invoiced = set(
            Invoice.objects.filter(company=company, period=period).values_list(
                'freelancer_id', flat=True
            )
        )

        to_issue = [
            list(group)
            for freelancer_id, group in groupby(entries, key=attrgetter('contract.freelancer_id'))
            if freelancer_id not in already_invoiced  # Late approvals for an invoiced freelancer are left unbilled.
        ]

        run = BillingRun.objects.create(
            company=company,
            period=period,
            triggered_by=triggered_by,
            invoices_generated=len(to_issue),
            invoices_existing=len(already_invoiced),
        )
        for freelancer_entries in to_issue:
            _issue_invoice(run=run, entries=freelancer_entries)

        if to_issue:
            # Never send email inside the transaction. robust=True: a failure
            # in the post-commit send must not turn a committed run into a 500.
            transaction.on_commit(
                lambda: send_deliveries(InvoiceDelivery.objects.filter(invoice__billing_run=run)),
                robust=True,
            )

    logger.info(
        'Billing run %s for company %s %s: %s generated, %s existing',
        run.pk, company.pk, f'{period:%Y-%m}', run.invoices_generated, run.invoices_existing,
    )
    return run


def _issue_invoice(*, run: BillingRun, entries: list[TimesheetEntry]) -> Invoice:
    company = run.company
    period = run.period
    freelancer = entries[0].contract.freelancer  # Grouped by freelancer so all entries should be the same
    lines = [
        InvoiceLine(
            timesheet_entry=entry,
            contract=entry.contract,
            date=entry.date,
            hours=entry.hours,
            daily_rate=entry.contract.daily_rate,
            amount=line_amount(entry.hours, entry.contract.daily_rate),
        )
        for entry in entries
    ]
    invoice = Invoice.objects.create(
        company=company,
        freelancer=freelancer,
        billing_run=run,
        period=period,
        reference=Invoice.build_reference(
            period=period, company_id=company.pk, freelancer_id=freelancer.pk
        ),
        total=sum((line.amount for line in lines), Decimal('0.00')),
        company_name=company.name,
        company_billing_email=company.billing_email,
        freelancer_name=freelancer.name,
    )
    for line in lines:
        line.invoice = invoice
    InvoiceLine.objects.bulk_create(lines)
    InvoiceDelivery.objects.bulk_create([
        new_delivery(
            invoice=invoice,
            recipient_type=InvoiceDelivery.RECIPIENT_COMPANY,
            email=company.billing_email,
        ),
        new_delivery(
            invoice=invoice,
            recipient_type=InvoiceDelivery.RECIPIENT_FREELANCER,
            email=freelancer.user.email,
        ),
    ])
    return invoice
