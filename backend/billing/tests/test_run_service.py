"""Service-level tests for the monthly billing run.

Tests numeric calculations (money), filters and boundaries
"""
import datetime
import threading
from decimal import Decimal
from unittest import mock

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from accounts.models import Company
from billing.models import BillingRun, Invoice, InvoiceDelivery, InvoiceLine
from billing.services import line_amount, run_billing
from billing.tests.data import MARCH
from contracts.models import Contract, TimesheetEntry

pytestmark = pytest.mark.django_db


def only_invoice(company, freelancer, period=MARCH):
    return Invoice.objects.get(company=company, freelancer=freelancer, period=period)


class TestLineAmount:
    @pytest.mark.parametrize('hours, expected', [
        ('7.5', '421.88'),  # 421.875
        # 140.625: HALF_UP gives .63, banker's rounding (HALF_EVEN) would give
        # .62, so this case pins the rounding mode.
        ('2.5', '140.63'),
    ])
    def test_partial_day_rounds_half_up_to_the_penny(self, hours, expected):
        assert line_amount(Decimal(hours), Decimal('450.00')) == Decimal(expected)

    def test_hours_beyond_a_day_are_billed_pro_rata_not_capped(self):
        assert line_amount(Decimal('10.0'), Decimal('600.00')) == Decimal('750.00')


class TestInvoiceAmounts:
    def test_total_is_sum_of_rounded_lines_not_rounded_sum(
        self, bill, make_contract, make_entry, northstar, second_freelancer
    ):
        contract = make_contract(northstar, second_freelancer, rate='450.00')
        for day in ('2026-03-02', '2026-03-03', '2026-03-04'):
            make_entry(contract, day, hours='7.5')

        bill()

        invoice = only_invoice(northstar, second_freelancer)
        # 3 x 421.88 = 1265.64, whereas round(3 x 421.875) = round(1265.625) = 1265.63.
        assert invoice.total == Decimal('1265.64')
        assert invoice.total == sum(line.amount for line in invoice.lines.all())

    def test_two_contracts_at_different_rates_give_one_invoice_with_a_line_per_rate(
        self, bill, make_contract, make_entry, northstar, freelancer_user, active_contract
    ):
        second_contract = make_contract(northstar, freelancer_user.freelancer, rate='450.00')
        make_entry(active_contract, '2026-03-02', hours='8.0')
        make_entry(second_contract, '2026-03-03', hours='4.0')

        run = bill()

        assert run.invoices_generated == 1
        invoice = only_invoice(northstar, freelancer_user.freelancer)
        lines = {line.contract_id: line for line in invoice.lines.all()}
        assert lines[active_contract.pk].daily_rate == Decimal('600.00')
        assert lines[active_contract.pk].amount == Decimal('600.00')
        assert lines[second_contract.pk].daily_rate == Decimal('450.00')
        assert lines[second_contract.pk].amount == Decimal('225.00')
        assert invoice.total == Decimal('825.00')


class TestWhatGetsBilled:
    @pytest.mark.parametrize('status', [
        TimesheetEntry.STATUS_DRAFT,
        TimesheetEntry.STATUS_SUBMITTED,
        TimesheetEntry.STATUS_REJECTED,
    ])
    def test_only_approved_entries_are_billed(self, bill, make_entry, active_contract, status):
        approved = make_entry(active_contract, '2026-03-02')
        not_approved = make_entry(active_contract, '2026-03-03', status=status)

        bill()

        billed = set(InvoiceLine.objects.values_list('timesheet_entry_id', flat=True))
        assert billed == {approved.pk}
        assert not InvoiceLine.objects.filter(timesheet_entry=not_approved).exists()

    def test_entries_are_grouped_per_freelancer_when_dates_interleave(
        self, bill, make_contract, make_entry, northstar, freelancer_user, second_freelancer,
        active_contract,
    ):
        first = make_entry(active_contract, '2026-03-02')
        make_entry(make_contract(northstar, second_freelancer), '2026-03-03')
        last = make_entry(active_contract, '2026-03-04')

        run = bill()

        assert run.invoices_generated == 2
        invoice = only_invoice(northstar, freelancer_user.freelancer)
        assert [line.timesheet_entry_id for line in invoice.lines.all()] == [first.pk, last.pk]

    def test_month_boundaries_are_inclusive_of_last_day_only(
        self, bill, make_entry, active_contract
    ):
        last_day_of_previous = make_entry(active_contract, '2026-02-28')
        first_day = make_entry(active_contract, '2026-03-01')
        last_day = make_entry(active_contract, '2026-03-31')
        first_day_of_next = make_entry(active_contract, '2026-04-01')

        bill()

        billed = set(InvoiceLine.objects.values_list('timesheet_entry_id', flat=True))
        assert billed == {first_day.pk, last_day.pk}
        assert last_day_of_previous.pk not in billed
        assert first_day_of_next.pk not in billed

    def test_other_companies_entries_are_excluded(
        self, bill, make_entry, northstar, freelancer_user, active_contract, other_contract
    ):
        northstar_entry = make_entry(active_contract, '2026-03-02')
        make_entry(other_contract, '2026-03-02')

        bill()

        invoice = only_invoice(northstar, freelancer_user.freelancer)
        assert [line.timesheet_entry_id for line in invoice.lines.all()] == [northstar_entry.pk]
        assert invoice.total == Decimal('600.00')
        assert not Invoice.objects.filter(company=other_contract.company).exists()

    def test_closed_contracts_are_still_billed(
        self, bill, make_contract, make_entry, northstar, second_freelancer
    ):
        closed = make_contract(northstar, second_freelancer, status=Contract.STATUS_CLOSED)
        entry = make_entry(closed, '2026-03-02')

        bill()

        assert only_invoice(northstar, second_freelancer).lines.get().timesheet_entry == entry

    def test_zero_hour_entries_never_produce_a_zero_invoice(
        self, bill, make_contract, make_entry, northstar, second_freelancer, active_contract
    ):
        make_entry(active_contract, '2026-03-02')
        zero_only = make_contract(northstar, second_freelancer)
        make_entry(zero_only, '2026-03-02', hours='0')

        run = bill()

        assert run.invoices_generated == 1
        assert not Invoice.objects.filter(freelancer=second_freelancer).exists()
        assert not InvoiceLine.objects.filter(contract=zero_only).exists()

    def test_run_with_nothing_to_bill_is_still_recorded(self, bill, northstar, admin_user):
        run = bill()

        assert run == BillingRun.objects.get()
        assert (run.company, run.period, run.triggered_by) == (northstar, MARCH, admin_user)
        assert (run.invoices_generated, run.invoices_existing) == (0, 0)
        assert not Invoice.objects.exists()


class TestIssuedInvoice:
    def test_reference_format(self):
        reference = Invoice.build_reference(period=MARCH, company_id=7, freelancer_id=42)
        assert reference == 'INV-202603-0007-0042'

    def test_later_edits_do_not_rewrite_an_issued_invoice(
        self, bill, make_entry, northstar, freelancer_user, active_contract
    ):
        make_entry(active_contract, '2026-03-02', hours='7.5')
        bill()

        northstar.name = 'Renamed Ltd'
        northstar.billing_email = 'new-billing@northstar.test'
        northstar.save()
        active_contract.daily_rate = Decimal('999.00')
        active_contract.save()
        freelancer = freelancer_user.freelancer
        freelancer.name = 'Renamed Freelancer'
        freelancer.save()

        invoice = only_invoice(northstar, freelancer)
        assert invoice.company_name == 'NorthStar Consulting'
        assert invoice.company_billing_email == 'billing@northstar.test'
        assert invoice.freelancer_name == 'Test Freelancer'
        assert invoice.total == Decimal('562.50')
        line = invoice.lines.get()
        assert (line.daily_rate, line.amount) == (Decimal('600.00'), Decimal('562.50'))


class TestRerunningAMonth:
    def test_second_run_records_a_run_but_creates_nothing(
        self, bill, make_contract, make_entry, northstar, second_freelancer, active_contract
    ):
        make_entry(active_contract, '2026-03-02')
        make_entry(make_contract(northstar, second_freelancer), '2026-03-02')
        first = bill()
        counts_after_first = (
            Invoice.objects.count(), InvoiceLine.objects.count(), InvoiceDelivery.objects.count(),
        )

        second = bill()

        assert BillingRun.objects.count() == 2
        assert (first.invoices_generated, first.invoices_existing) == (2, 0)
        assert (second.invoices_generated, second.invoices_existing) == (0, 2)
        assert not second.invoices.exists()
        assert counts_after_first == (2, 2, 4)
        assert (
            Invoice.objects.count(), InvoiceLine.objects.count(), InvoiceDelivery.objects.count(),
        ) == counts_after_first
        assert set(Invoice.objects.values_list('billing_run', flat=True)) == {first.pk}

    def test_late_approval_is_not_added_on_rerun(
        self, bill, make_entry, northstar, freelancer_user, active_contract
    ):
        """Documented gap: no supplementary invoices. An entry approved after
        its month was invoiced stays unbilled."""
        make_entry(active_contract, '2026-03-02')
        late = make_entry(active_contract, '2026-03-03', status=TimesheetEntry.STATUS_SUBMITTED)
        bill()

        late.status = TimesheetEntry.STATUS_APPROVED
        late.save()
        rerun = bill()

        assert (rerun.invoices_generated, rerun.invoices_existing) == (0, 1)
        invoice = only_invoice(northstar, freelancer_user.freelancer)
        assert invoice.lines.count() == 1
        assert invoice.total == Decimal('600.00')
        assert not InvoiceLine.objects.filter(timesheet_entry=late).exists()

    def test_rerun_invoices_a_newly_eligible_freelancer_only(
        self, bill, make_contract, make_entry, northstar, second_freelancer, active_contract
    ):
        make_entry(active_contract, '2026-03-02')
        first = bill()
        make_entry(make_contract(northstar, second_freelancer), '2026-03-10')

        second = bill()

        assert (second.invoices_generated, second.invoices_existing) == (1, 1)
        newcomer = only_invoice(northstar, second_freelancer)
        assert list(second.invoices.all()) == [newcomer]
        assert only_invoice(northstar, active_contract.freelancer).billing_run == first

    def test_different_months_are_independent(self, bill, make_entry, active_contract):
        make_entry(active_contract, '2026-03-02')
        make_entry(active_contract, '2026-04-01')

        march = bill(MARCH)
        april = bill(datetime.date(2026, 4, 1))

        assert march.invoices_generated == 1
        assert april.invoices_generated == 1
        assert sorted(Invoice.objects.values_list('period', flat=True)) == [
            MARCH, datetime.date(2026, 4, 1),
        ]


@pytest.fixture
def issued_invoice(bill, make_entry, northstar, freelancer_user, active_contract):
    make_entry(active_contract, '2026-03-02')
    bill()
    return only_invoice(northstar, freelancer_user.freelancer)


class TestDatabaseGuarantees:
    """Belt and braces behind the service logic: these hold even if a caller
    bypasses run_billing."""

    def _copy_invoice(self, invoice, **overrides):
        fields = dict(
            company=invoice.company,
            freelancer=invoice.freelancer,
            billing_run=invoice.billing_run,
            period=invoice.period,
            reference='INV-DIFFERENT',
            total=Decimal('1.00'),
            company_name='x',
            company_billing_email='x@test.test',
            freelancer_name='x',
        )
        fields.update(overrides)
        return Invoice.objects.create(**fields)

    def test_one_invoice_per_company_freelancer_and_month(self, issued_invoice):
        with pytest.raises(IntegrityError, match='invoice_unique_company_freelancer_period'):
            with transaction.atomic():
                self._copy_invoice(issued_invoice)

    def test_an_entry_can_only_be_billed_once(self, issued_invoice, second_freelancer):
        billed_line = issued_invoice.lines.get()
        other_invoice = self._copy_invoice(
            issued_invoice, freelancer=second_freelancer, reference='INV-OTHER',
        )

        with pytest.raises(IntegrityError, match='timesheet_entry_id'):
            with transaction.atomic():
                InvoiceLine.objects.create(
                    invoice=other_invoice,
                    timesheet_entry=billed_line.timesheet_entry,
                    contract=billed_line.contract,
                    date=billed_line.date,
                    hours=billed_line.hours,
                    daily_rate=billed_line.daily_rate,
                    amount=billed_line.amount,
                )

    def test_a_billed_entry_cannot_be_deleted(self, issued_invoice):
        line = issued_invoice.lines.get()

        with pytest.raises(ProtectedError):
            line.timesheet_entry.delete()

        assert Invoice.objects.filter(pk=issued_invoice.pk).exists()
        assert InvoiceLine.objects.filter(pk=line.pk).exists()


class TestConcurrentRuns:
    @pytest.mark.django_db(transaction=True)
    def test_concurrent_runs_for_the_same_month_issue_one_set_of_invoices(
        self, make_contract, make_entry, northstar, admin_user, second_freelancer, active_contract
    ):
        """Two simultaneous runs are serialised by the company row lock: the second
        waits, then sees the first's committed invoices and counts them as existing."""
        make_entry(active_contract, '2026-03-02')
        make_entry(make_contract(northstar, second_freelancer), '2026-03-02')
        freelancer_count = 2

        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            try:
                company = Company.objects.get(pk=northstar.pk)
                barrier.wait(timeout=10)
                results.append(run_billing(company=company, triggered_by=admin_user, period=MARCH))
            except Exception as exc:  # surfaced in the main thread below
                errors.append(exc)
            finally:
                connection.close()

        # Not testing email here: stop the post-commit hook from sending.
        with mock.patch('billing.services.send_deliveries'):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        assert errors == []
        assert len(results) == 2
        assert BillingRun.objects.filter(company=northstar, period=MARCH).count() == 2
        assert Invoice.objects.filter(company=northstar, period=MARCH).count() == freelancer_count
        assert sum(r.invoices_generated for r in results) == freelancer_count
        assert sorted(r.invoices_existing for r in results) == [0, freelancer_count]
