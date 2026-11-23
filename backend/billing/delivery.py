"""Invoice email delivery (the outbox sender).

Used by both the post-commit hook of a billing run and the
`send_invoice_emails` management command.

Delivery is at-least-once. If we fail to save that sending completed, it will retry.
"""
import datetime
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.mail import EmailMessage
from django.db.models import Q, QuerySet
from django.utils import timezone

from billing.models import Invoice, InvoiceDelivery

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
STALE_SENDING_AFTER = datetime.timedelta(minutes=10)

MISSING_EMAIL_ERROR = 'No email address on record for this recipient.'
ABANDONED_ERROR = 'Delivery attempt never completed (sender stopped mid-send); attempts exhausted.'


def aggregate_delivery_status(deliveries: Iterable[InvoiceDelivery]) -> str:
    """One status for an invoice's deliveries.

    'failed' if any delivery failed, 'sent' once every delivery has been sent,
    otherwise 'pending' (which includes rows currently being sent).
    """
    statuses = {delivery.status for delivery in deliveries}
    if InvoiceDelivery.STATUS_FAILED in statuses:
        return InvoiceDelivery.STATUS_FAILED
    if statuses == {InvoiceDelivery.STATUS_SENT}:
        return InvoiceDelivery.STATUS_SENT
    return InvoiceDelivery.STATUS_PENDING


def new_delivery(*, invoice: Invoice, recipient_type: str, email: str) -> InvoiceDelivery:
    """Unsaved outbox row for one recipient."""
    if not email:
        # Missing email means this will never be deliverable, so just mark as failed and skip it.
        return InvoiceDelivery(
            invoice=invoice,
            recipient_type=recipient_type,
            status=InvoiceDelivery.STATUS_FAILED,
            last_error=MISSING_EMAIL_ERROR,
        )
    return InvoiceDelivery(invoice=invoice, recipient_type=recipient_type, email=email)


def _claimable(now: datetime.datetime) -> Q:
    # Deliveries can be sent if they:
    # Have an address AND have not hit max attempts AND
    # Are pending, failed or have been stuck in sending for too long.
    return Q(attempts__lt=MAX_ATTEMPTS) & ~Q(email='') & (
        Q(status__in=[InvoiceDelivery.STATUS_PENDING, InvoiceDelivery.STATUS_FAILED])
        | Q(status=InvoiceDelivery.STATUS_SENDING, last_attempt_at__lt=now - STALE_SENDING_AFTER)
    )


def attempt_delivery(delivery_id: int) -> str | None:
    """Claim and send one delivery. Returns the resulting status, or None if
    the row could not be claimed (already sent, in flight elsewhere, or out of
    attempts).

    `attempts` is incremented at claim time, so a sender that crashes
    mid-send still uses up an attempt and a row can't loop forever. This is used to check another
    attempt hasn't been made for this InvoiceDelivery.
    """
    delivery = InvoiceDelivery.objects.select_related('invoice').get(pk=delivery_id)
    attempt = delivery.attempts + 1
    now = timezone.now()
    claimed = InvoiceDelivery.objects.filter(
        _claimable(now), pk=delivery_id, attempts=delivery.attempts
    ).update(status=InvoiceDelivery.STATUS_SENDING, attempts=attempt, last_attempt_at=now)
    if not claimed:
        return None

    # Work with the queryset to avoid calling .save() on an individual object.
    this_claim = InvoiceDelivery.objects.filter(pk=delivery_id, attempts=attempt)
    try:
        build_invoice_email(delivery.invoice, delivery.email).send(fail_silently=False)
    except Exception as exc:  # SMTP errors, timeouts, connection refused etc.
        this_claim.update(
            status=InvoiceDelivery.STATUS_FAILED,
            last_error=f'{type(exc).__name__}: {exc}'[:2000],
        )
        logger.warning('Invoice delivery %s failed: %s', delivery_id, exc)
        return InvoiceDelivery.STATUS_FAILED

    this_claim.update(status=InvoiceDelivery.STATUS_SENT, sent_at=timezone.now(), last_error='')
    return InvoiceDelivery.STATUS_SENT


@dataclass
class SendSummary:
    sent: int = 0
    failed_id: int | None = None  # the failed send that stopped the batch
    deferred: int = 0  # not attempted because the batch stopped


def send_deliveries(deliveries: QuerySet[InvoiceDelivery]) -> SendSummary:
    """
    Attempt to deliver every pending email (plus failed ones needing a retry), stopping at the first failed send.
    Anything not attempted stays claimable for the next run.
    """
    ids = list(
        deliveries.filter(_claimable(timezone.now())).order_by('pk').values_list('pk', flat=True)
    )
    summary = SendSummary()
    for position, delivery_id in enumerate(ids):
        result = attempt_delivery(delivery_id)
        if result == InvoiceDelivery.STATUS_SENT:
            summary.sent += 1
        elif result == InvoiceDelivery.STATUS_FAILED:
            summary.failed_id = delivery_id
            summary.deferred = len(ids) - position - 1
            logger.warning(
                'Stopping invoice delivery after a send failure; %s deliveries left for the next run.',
                summary.deferred,
            )
            break
        # None: another sender claimed the row first, so it is theirs to report.
    return summary


def process_outbox() -> SendSummary:
    """One scheduled pass over the whole outbox."""
    # A sender that died while holding a row on its last attempt would leave it in `sending` forever;
    # record it as failed so it is counted by `count_needing_follow_up`.
    InvoiceDelivery.objects.filter(
        status=InvoiceDelivery.STATUS_SENDING,
        last_attempt_at__lt=timezone.now() - STALE_SENDING_AFTER,
        attempts__gte=MAX_ATTEMPTS,
    ).update(status=InvoiceDelivery.STATUS_FAILED, last_error=ABANDONED_ERROR)
    return send_deliveries(InvoiceDelivery.objects.all())


def count_needing_follow_up() -> int:
    """Deliveries that have failed for good (no address, or out of attempts)."""
    return InvoiceDelivery.objects.filter(
        ~_claimable(timezone.now()), status=InvoiceDelivery.STATUS_FAILED
    ).count()


# --- Rendering ---------------------------------------------------------------

def _gbp(amount: Decimal) -> str:
    return f'£{amount:,.2f}'


def build_invoice_email(invoice: Invoice, to_address: str) -> EmailMessage:
    """
    One plain-text email to one recipient. Content comes from the invoice's
    snapshots, so a retry will be the same.

    In real life this would be more complicated, have some templating, generate PDFs etc. so keeping this basic
    """
    period = f'{invoice.period:%B %Y}'
    lines = list(invoice.lines.all())
    rows = [
        f'  {line.date:%a %d %b %Y}   {line.hours:>6}h   {_gbp(line.daily_rate):>10}/day   {_gbp(line.amount):>12}'
        for line in lines
    ]
    body = '\n'.join([
        f'Invoice {invoice.reference}',
        '',
        f'Bill to:   {invoice.company_name} <{invoice.company_billing_email}>',
        f'Supplier:  {invoice.freelancer_name}',
        f'Period:    {period}',
        f'Issued:    {invoice.created_at:%d %b %Y}',
        '',
        'Work performed (billed per 8-hour day; partial days pro rata):',
        '',
        f'  {"Date":<15}   {"Hours":>7}   {"Day rate":>14}   {"Amount":>12}',
        *rows,
        '',
        f'  Total due: {_gbp(invoice.total)} (GBP)',
        '',
        f'Please quote {invoice.reference} on any correspondence about this invoice.',
    ])
    return EmailMessage(
        subject=f'Invoice {invoice.reference} — {invoice.freelancer_name} for {invoice.company_name}, {period}',
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[to_address],
    )
