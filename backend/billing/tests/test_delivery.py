"""Invoice email delivery: content, failure recording, the retry command and
the claim that prevents double sends.
"""
import datetime
import smtplib
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.mail import EmailMessage
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from billing.delivery import (
    ABANDONED_ERROR,
    MAX_ATTEMPTS,
    MISSING_EMAIL_ERROR,
    STALE_SENDING_AFTER,
    aggregate_delivery_status,
    attempt_delivery,
    send_deliveries,
)
from billing.models import Invoice, InvoiceDelivery

pytestmark = pytest.mark.django_db

COMPANY_EMAIL = 'billing@northstar.test'
FREELANCER_EMAIL = 'freelancer@test.test'


@pytest.fixture
def invoice_entries(northstar, freelancer_user, make_contract, make_entry):
    contract = make_contract(northstar, freelancer_user.freelancer, rate='450.00')
    make_entry(contract, '2026-03-02', hours='7.5')  # 7.5 / 8 * 450 = 421.875 -> £421.88
    make_entry(contract, '2026-03-03', hours='8.0')  # £450.00


@pytest.fixture
def invoice(bill, invoice_entries):
    """March's single invoice: freelancer_user, 7.5h + 8h at £450/day."""
    return bill().invoices.get()


def delivery(invoice, recipient_type):
    return InvoiceDelivery.objects.get(invoice=invoice, recipient_type=recipient_type)


def company_delivery(invoice):
    return delivery(invoice, InvoiceDelivery.RECIPIENT_COMPANY)


def freelancer_delivery(invoice):
    return delivery(invoice, InvoiceDelivery.RECIPIENT_FREELANCER)


def send_for(*invoices):
    """What the billing run does after commit for the invoices it issued."""
    return send_deliveries(InvoiceDelivery.objects.filter(invoice__in=invoices))


def run_command():
    out = StringIO()
    call_command('send_invoice_emails', stdout=out)
    return out.getvalue()


def send_raising(exc, only_to=None):
    """Patch EmailMessage.send to raise `exc` (for every message, or only for
    messages addressed to `only_to`); other messages are sent normally."""
    real_send = EmailMessage.send

    def send(self, *args, **kwargs):
        if only_to is None or only_to in self.to:
            raise exc
        return real_send(self, *args, **kwargs)

    return patch.object(EmailMessage, 'send', send)


def age_sending(row, age):
    InvoiceDelivery.objects.filter(pk=row.pk).update(
        status=InvoiceDelivery.STATUS_SENDING, attempts=1, last_attempt_at=timezone.now() - age
    )


@pytest.fixture
def blank_freelancer_email(freelancer_user):
    freelancer_user.email = ''
    freelancer_user.save()


@pytest.fixture
def two_invoices(bill, northstar, invoice_entries, second_freelancer, make_contract, make_entry):
    make_entry(make_contract(northstar, second_freelancer), '2026-03-02')
    run = bill()
    invoices = list(run.invoices.order_by('pk'))
    assert len(invoices) == 2
    return invoices


class TestPostRunSend:
    """The billing run's one send attempt after commit."""

    def test_post_run_send_sends_one_email_per_recipient(self, invoice, mailoutbox):
        send_for(invoice)

        assert sorted(m.to for m in mailoutbox) == [[COMPANY_EMAIL], [FREELANCER_EMAIL]]
        for message in mailoutbox:
            assert invoice.reference in message.subject
            body = message.body
            assert 'Mon 02 Mar 2026' in body and '7.50h' in body and '£421.88' in body
            assert 'Tue 03 Mar 2026' in body and '8.00h' in body
            assert body.count('£450.00/day') == 2
            assert 'Total due: £871.88 (GBP)' in body

        for row in InvoiceDelivery.objects.filter(invoice=invoice):
            assert row.status == InvoiceDelivery.STATUS_SENT
            assert row.sent_at is not None
            assert row.attempts == 1
            assert row.last_error == ''

    def test_send_failure_is_recorded(self, invoice, mailoutbox):
        with send_raising(smtplib.SMTPException('mail server said no')):
            send_for(invoice)  # must not raise

        failed = company_delivery(invoice)  # attempted first
        assert failed.status == InvoiceDelivery.STATUS_FAILED
        assert failed.last_error == 'SMTPException: mail server said no'
        assert failed.attempts == 1
        assert failed.last_attempt_at is not None
        assert failed.sent_at is None
        assert mailoutbox == []

    def test_post_run_delivery_stops_at_the_first_send_failure(self, two_invoices, mailoutbox):
        """With SMTP down, the post-run attempt costs one send (one EMAIL_TIMEOUT
        at most), not one per delivery."""
        send = patch.object(EmailMessage, 'send', side_effect=ConnectionRefusedError(61, 'refused'))
        with send as mock_send:
            send_for(*two_invoices)

        assert mock_send.call_count == 1
        rows = list(InvoiceDelivery.objects.filter(invoice__in=two_invoices))  # attempt order
        assert [(r.status, r.attempts) for r in rows] == [
            ('failed', 1), ('pending', 0), ('pending', 0), ('pending', 0),
        ]
        assert 'ConnectionRefusedError' in rows[0].last_error

        # SMTP is back: the command sends everything that is left.
        assert run_command().strip() == 'Sent 4.'
        assert len(mailoutbox) == 4

    def test_post_run_send_leaves_other_runs_deliveries_alone(
        self, bill, northstar, freelancer_user, make_contract, make_entry, mailoutbox,
        django_capture_on_commit_callbacks,
    ):
        contract = make_contract(northstar, freelancer_user.freelancer)
        make_entry(contract, '2026-02-10')
        make_entry(contract, '2026-03-10')
        february = bill(datetime.date(2026, 2, 1)).invoices.get()
        # Left over from an earlier send; still retryable by the command.
        InvoiceDelivery.objects.filter(invoice=february).update(
            status=InvoiceDelivery.STATUS_FAILED, attempts=1
        )

        with django_capture_on_commit_callbacks(execute=True):
            march = bill().invoices.get()

        assert len(mailoutbox) == 2
        assert all(march.reference in m.subject for m in mailoutbox)
        assert set(february.deliveries.values_list('status', 'attempts')) == {('failed', 1)}


class TestBlankAddress:
    def test_blank_address_is_issued_failed_and_never_attempted(self, blank_freelancer_email, invoice, mailoutbox):
        blank = freelancer_delivery(invoice)
        assert (blank.status, blank.attempts, blank.last_error) == ('failed', 0, MISSING_EMAIL_ERROR)

        send_for(invoice)
        for _ in range(2):
            output = run_command()

        assert [m.to for m in mailoutbox] == [[COMPANY_EMAIL]]
        blank.refresh_from_db()
        assert (blank.status, blank.attempts) == ('failed', 0)
        assert company_delivery(invoice).status == InvoiceDelivery.STATUS_SENT
        assert output.strip() == (
            'Sent 0. 1 failed deliveries cannot be retried and need manual follow-up.'
        )


class TestRetryCommand:
    """manage.py send_invoice_emails."""

    def test_command_resends_only_the_failed_recipient(self, invoice, mailoutbox):
        with send_raising(smtplib.SMTPException('rejected'), only_to=FREELANCER_EMAIL):
            send_for(invoice)
        assert [m.to for m in mailoutbox] == [[COMPANY_EMAIL]]
        assert freelancer_delivery(invoice).status == InvoiceDelivery.STATUS_FAILED
        mailoutbox.clear()

        output = run_command()

        assert output.strip() == 'Sent 1.'
        assert [m.to for m in mailoutbox] == [[FREELANCER_EMAIL]]
        retried, untouched = freelancer_delivery(invoice), company_delivery(invoice)
        assert (retried.status, retried.attempts, retried.last_error) == ('sent', 2, '')
        assert (untouched.status, untouched.attempts) == ('sent', 1)

    def test_outage_only_spends_the_attempts_of_the_first_row(self, invoice, mailoutbox):
        first, second = InvoiceDelivery.objects.filter(invoice=invoice).order_by('pk')
        with send_raising(smtplib.SMTPException('down')):
            for _ in range(MAX_ATTEMPTS):
                output = run_command()

        # Every run stops at the first failure, so only the first row used its attempts.
        first.refresh_from_db()
        second.refresh_from_db()
        assert (first.status, first.attempts) == ('failed', MAX_ATTEMPTS)
        assert (second.status, second.attempts) == ('pending', 0)
        assert output.strip() == (
            f'Sent 0. Delivery {first.pk} failed; stopped with 1 left for the next run.'
            ' 1 failed deliveries cannot be retried and need manual follow-up.'
        )

        # With sending working again, the exhausted row is left alone.
        output = run_command()

        assert [m.to for m in mailoutbox] == [[second.email]]
        first.refresh_from_db()
        assert (first.status, first.attempts) == ('failed', MAX_ATTEMPTS)
        assert output.strip() == (
            'Sent 1. 1 failed deliveries cannot be retried and need manual follow-up.'
        )


class TestNoDoubleSend:
    """The claim that stops two senders sending the same delivery."""

    def test_sent_delivery_is_not_sent_again(self, invoice, mailoutbox):
        send_for(invoice)
        row = company_delivery(invoice)
        mailoutbox.clear()

        assert attempt_delivery(row.pk) is None
        assert run_command().strip() == 'Sent 0.'
        assert mailoutbox == []
        row.refresh_from_db()
        assert (row.status, row.attempts) == ('sent', 1)

    def test_concurrent_attempt_during_send_is_refused(self, invoice, mailoutbox):
        """A second sender arriving while the first is mid-send (row `sending`)
        can't claim the row."""
        row = company_delivery(invoice)
        real_send = EmailMessage.send
        racing_results = []

        def send(self, *args, **kwargs):
            racing_results.append(attempt_delivery(row.pk))
            return real_send(self, *args, **kwargs)

        with patch.object(EmailMessage, 'send', send):
            assert attempt_delivery(row.pk) == InvoiceDelivery.STATUS_SENT

        assert racing_results == [None]
        assert len(mailoutbox) == 1
        row.refresh_from_db()
        assert (row.status, row.attempts) == ('sent', 1)

    def test_recently_claimed_sending_row_is_skipped(self, invoice, mailoutbox):
        row = company_delivery(invoice)
        age_sending(row, datetime.timedelta(minutes=1))

        assert attempt_delivery(row.pk) is None
        run_command()  # sends the pending freelancer row only

        assert [m.to for m in mailoutbox] == [[FREELANCER_EMAIL]]
        row.refresh_from_db()
        assert (row.status, row.attempts) == ('sending', 1)

    def test_stale_sending_row_is_reclaimed_and_sent(self, invoice, mailoutbox):
        row = company_delivery(invoice)
        age_sending(row, STALE_SENDING_AFTER + datetime.timedelta(seconds=1))

        assert attempt_delivery(row.pk) == InvoiceDelivery.STATUS_SENT
        assert [m.to for m in mailoutbox] == [[COMPANY_EMAIL]]
        row.refresh_from_db()
        assert (row.status, row.attempts) == ('sent', 2)

    @pytest.mark.parametrize('send_raises', [False, True], ids=['sent', 'failed'])
    def test_outcome_of_a_superseded_claim_is_not_recorded(self, invoice, send_raises):
        """A sender whose claim went stale and was re-claimed by another sender
        must not overwrite the newer claimant's row when it finally finishes."""
        row = company_delivery(invoice)
        reclaimed_at = timezone.now() + datetime.timedelta(minutes=11)

        def send(self, *args, **kwargs):
            # While we are mid-send, another sender takes the row over as stale.
            InvoiceDelivery.objects.filter(pk=row.pk).update(
                attempts=2, last_attempt_at=reclaimed_at
            )
            if send_raises:
                raise smtplib.SMTPException('late failure')

        with patch.object(EmailMessage, 'send', send):
            attempt_delivery(row.pk)

        row.refresh_from_db()
        # Still exactly as the newer claimant left it.
        assert (row.status, row.attempts, row.last_attempt_at) == ('sending', 2, reclaimed_at)
        assert (row.last_error, row.sent_at) == ('', None)

    def test_abandoned_row_on_last_attempt_is_marked_failed_and_reported(self, invoice, mailoutbox):
        # A sender died while holding the row on its final attempt.
        row = company_delivery(invoice)
        age_sending(row, STALE_SENDING_AFTER + datetime.timedelta(seconds=1))
        InvoiceDelivery.objects.filter(pk=row.pk).update(attempts=MAX_ATTEMPTS)

        output = run_command()

        row.refresh_from_db()
        assert (row.status, row.attempts) == ('failed', MAX_ATTEMPTS)
        assert row.last_error == ABANDONED_ERROR
        assert '1 failed deliveries cannot be retried' in output
        assert [m.to for m in mailoutbox] == [[FREELANCER_EMAIL]]  # only the pending row


class TestTransactionBoundary:
    @pytest.mark.django_db(transaction=True)
    def test_no_email_is_sent_for_a_rolled_back_run(self, bill, invoice_entries, mailoutbox):
        """Sending after a commit is covered by the API email test."""
        with pytest.raises(RuntimeError), transaction.atomic():
            bill()
            assert mailoutbox == []  # not sent from inside the transaction
            raise RuntimeError('rollback')

        # The post-commit send is discarded with the invoice.
        assert not Invoice.objects.exists()
        assert mailoutbox == []

    @pytest.mark.django_db(transaction=True)
    def test_crash_in_post_commit_send_keeps_the_invoice(self, bill, invoice_entries, mailoutbox):
        with patch('billing.delivery.attempt_delivery', side_effect=RuntimeError('db went away')):
            run = bill()

        # The run still succeeded; its deliveries wait in the outbox for the command.
        invoice = run.invoices.get()
        assert set(invoice.deliveries.values_list('status', flat=True)) == {InvoiceDelivery.STATUS_PENDING}
        assert mailoutbox == []

        assert run_command().strip() == 'Sent 2.'
        assert len(mailoutbox) == 2


class TestAggregateDeliveryStatus:
    @pytest.mark.parametrize('statuses, expected', [
        (['pending', 'pending'], 'pending'),
        (['sent', 'pending'], 'pending'),
        (['sent', 'sending'], 'pending'),
        (['sent', 'sent'], 'sent'),
        (['sent', 'failed'], 'failed'),
        (['failed', 'pending'], 'failed'),
    ])
    def test_aggregate_delivery_status(self, statuses, expected):
        deliveries = [SimpleNamespace(status=status) for status in statuses]
        assert aggregate_delivery_status(deliveries) == expected
