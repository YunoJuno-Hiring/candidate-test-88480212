"""API tests for the billing run and invoice endpoints.

Covers: validation, response shapes,
emails end-to-end and authorization.
"""
import datetime
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import CompanyAdmin, Freelancer
from billing.models import BillingRun, Invoice, InvoiceDelivery
from billing.services import run_billing

from billing.tests.data import MARCH

RUNS_URL = '/api/billing/runs/'
LIST_URL = '/api/invoices/'

pytestmark = pytest.mark.django_db


def detail_url(invoice_id):
    return f'/api/invoices/{invoice_id}/'


def current_month():
    return timezone.localdate().replace(day=1)


@pytest.fixture
def march_entries(make_entry, make_contract, active_contract, second_freelancer):
    """Two NorthStar freelancers with approved March hours.

    freelancer_user (£600/day): 8h + 4h = £600.00 + £300.00 = £900.00
    second_freelancer (£450/day): 8h = £450.00
    """
    second_contract = make_contract(active_contract.company, second_freelancer, rate='450.00')
    return [
        make_entry(active_contract, '2026-03-02', hours='8.0'),
        make_entry(active_contract, '2026-03-03', hours='4.0'),
        make_entry(second_contract, '2026-03-04', hours='8.0'),
    ]


@pytest.fixture
def meridian_admin(meridian):
    user = User.objects.create_user(
        username='admin@meridian.test', email='admin@meridian.test', password='testpass'
    )
    CompanyAdmin.objects.create(user=user, company=meridian)
    return user


@pytest.fixture
def meridian_invoice(make_entry, other_contract, meridian_admin):
    make_entry(other_contract, '2026-03-10')
    run = run_billing(company=other_contract.company, triggered_by=meridian_admin, period=MARCH)
    return run.invoices.get()


@pytest.fixture
def northstar_invoices(bill, active_contract, make_entry, march_entries):
    """Two NorthStar invoices for March and one for February."""
    company = active_contract.company
    make_entry(active_contract, '2026-02-10')
    bill(datetime.date(2026, 2, 1))
    bill()
    return Invoice.objects.filter(company=company)


class TestRunBilling:
    """POST /api/billing/runs/."""

    # Month rules are covered in test_serializers.py; this checks the view applies them.
    def test_run_rejects_a_missing_month(self, admin_client):
        resp = admin_client.post(RUNS_URL, {}, format='json')

        assert resp.status_code == 400
        assert resp.json() == {'month': ['This field is required.']}
        assert BillingRun.objects.count() == 0

    def test_run_returns_201_with_run_summary(self, admin_client, march_entries):
        resp = admin_client.post(RUNS_URL, {'month': '2026-03'}, format='json')

        assert resp.status_code == 201
        run = BillingRun.objects.get()
        assert resp.json() == {
            'run_id': run.pk,
            'invoices_generated': 2,
            'invoices_existing': 0,
            'status': 'completed',
        }

    def test_run_emails_each_invoice_to_company_and_freelancer_after_commit(
        self, admin_client, march_entries, mailoutbox, django_capture_on_commit_callbacks,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            resp = admin_client.post(RUNS_URL, {'month': '2026-03'}, format='json')
        assert resp.status_code == 201

        invoices = list(Invoice.objects.select_related('freelancer__user'))
        assert len(invoices) == 2
        assert len(mailoutbox) == 4
        assert all(len(message.to) == 1 for message in mailoutbox)

        # Email content is covered in test_delivery.py.
        for invoice in invoices:
            messages = [m for m in mailoutbox if invoice.reference in m.subject]
            assert sorted(m.to[0] for m in messages) == sorted(
                ['billing@northstar.test', invoice.freelancer.user.email]
            )

        assert set(InvoiceDelivery.objects.values_list('status', flat=True)) == {InvoiceDelivery.STATUS_SENT}

        # Re-running the month issues nothing new, so nothing more is sent.
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            resp = admin_client.post(RUNS_URL, {'month': '2026-03'}, format='json')
        assert resp.status_code == 201
        assert callbacks == []
        assert len(mailoutbox) == 4

    def test_crash_in_post_commit_send_still_returns_201(
        self, admin_client, active_contract, make_entry, django_capture_on_commit_callbacks,
    ):
        """robust=True: a crash after commit must not turn a committed run into a 500.
        SMTP failures never get this far; they are recorded inside attempt_delivery."""
        make_entry(active_contract, '2026-03-02')

        with mock.patch('billing.delivery.attempt_delivery', side_effect=RuntimeError('db went away')):
            with django_capture_on_commit_callbacks(execute=True) as callbacks:
                resp = admin_client.post(RUNS_URL, {'month': '2026-03'}, format='json')

        assert len(callbacks) == 1
        assert resp.status_code == 201
        assert set(InvoiceDelivery.objects.values_list('status', flat=True)) == {InvoiceDelivery.STATUS_PENDING}

    def test_run_skips_an_entry_already_billed_in_another_month(self, admin_client, active_contract, make_entry):
        """Known issue: billed entries can still be edited. One moved into a later
        month stays on its original invoice and is not billed again."""
        moved = make_entry(active_contract, '2026-03-02')
        march = admin_client.post(RUNS_URL, {'month': '2026-03'}, format='json')
        assert march.status_code == 201
        resp = admin_client.patch(f'/api/timesheets/{moved.pk}/', {'date': '2026-04-02'}, format='json')
        assert resp.status_code == 200
        new = make_entry(active_contract, '2026-04-03')

        resp = admin_client.post(RUNS_URL, {'month': '2026-04'}, format='json')

        assert resp.status_code == 201
        assert resp.json()['invoices_generated'] == 1
        april = Invoice.objects.get(period=datetime.date(2026, 4, 1))
        assert [line.timesheet_entry_id for line in april.lines.all()] == [new.pk]
        assert moved.invoice_line.invoice.period == MARCH


class TestAuthorization:
    @pytest.mark.parametrize('method, url', [
        ('post', RUNS_URL),
        ('get', LIST_URL),
        ('get', detail_url(1)),
    ])
    def test_non_admin_is_forbidden(self, freelancer_client, method, url):
        resp = getattr(freelancer_client, method)(url, {'month': '2026-03'}, format='json')

        assert resp.status_code == 403
        assert resp.json()['detail'] == 'Only company admins can do this.'
        assert BillingRun.objects.count() == 0

    def test_admin_gets_404_for_another_companys_invoice(self, admin_client, meridian_invoice):
        resp = admin_client.get(detail_url(meridian_invoice.pk))

        assert resp.status_code == 404


class TestInvoiceList:
    """GET /api/invoices/."""

    def test_list_filters_by_month_within_the_admins_company(self, admin_client, northstar_invoices, meridian_invoice):
        resp = admin_client.get(LIST_URL, {'month': '2026-03'})

        assert resp.status_code == 200
        data = resp.json()
        assert [row['freelancer_name'] for row in data] == ['Second Freelancer', 'Test Freelancer']
        assert {row['period'] for row in data} == {'2026-03'}
        assert {row['company_name'] for row in data} == {'NorthStar Consulting'}
        assert meridian_invoice.pk not in {row['id'] for row in data}

    def test_list_without_month_returns_all_of_the_companys_invoices(
        self, admin_client, northstar_invoices, meridian_invoice,
    ):
        resp = admin_client.get(LIST_URL)

        assert resp.status_code == 200
        # Model ordering: newest period first, then freelancer name.
        assert [(row['period'], row['freelancer_name']) for row in resp.json()] == [
            ('2026-03', 'Second Freelancer'),
            ('2026-03', 'Test Freelancer'),
            ('2026-02', 'Test Freelancer'),
        ]

    def test_list_row_shape(self, admin_client, bill, active_contract, make_entry):
        make_entry(active_contract, '2026-03-02')
        bill()
        invoice = Invoice.objects.get()

        resp = admin_client.get(LIST_URL, {'month': '2026-03'})

        assert resp.json() == [{
            'id': invoice.pk,
            'reference': invoice.reference,
            'freelancer': active_contract.freelancer_id,
            'freelancer_name': 'Test Freelancer',
            'company_name': 'NorthStar Consulting',
            'period': '2026-03',
            'currency': 'GBP',
            'total': '600.00',
            'created_at': resp.json()[0]['created_at'],
            'delivery_status': 'pending',
        }]
        assert datetime.datetime.fromisoformat(resp.json()[0]['created_at']) == invoice.created_at

    def test_list_rejects_malformed_month(self, admin_client):
        # Other malformed values are covered by TestMonthField.
        resp = admin_client.get(LIST_URL, {'month': '2026-3'})

        assert resp.status_code == 400
        assert resp.json() == {'month': ["Month must be in 'YYYY-MM' format."]}

    def test_list_allows_the_current_month(self, admin_client, northstar_invoices):
        resp = admin_client.get(LIST_URL, {'month': f'{current_month():%Y-%m}'})

        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_query_count_does_not_grow_with_invoices(
        self, admin_client, bill, active_contract, make_entry, make_contract,
    ):
        company = active_contract.company

        def count_list_queries():
            with CaptureQueriesContext(connection) as ctx:
                resp = admin_client.get(LIST_URL)
            assert resp.status_code == 200
            return len(resp.json()), len(ctx.captured_queries)

        make_entry(active_contract, '2026-01-05')
        bill(datetime.date(2026, 1, 1))
        rows_before, queries_before = count_list_queries()

        for i in range(3):
            user = User.objects.create_user(username=f'extra{i}@test.test', email=f'extra{i}@test.test')
            freelancer = Freelancer.objects.create(user=user, name=f'Extra {i}')
            make_entry(make_contract(company, freelancer), '2026-02-05')
        bill(datetime.date(2026, 2, 1))
        rows_after, queries_after = count_list_queries()

        assert (rows_before, rows_after) == (1, 4)
        assert queries_after == queries_before


class TestInvoiceDetail:
    """GET /api/invoices/<id>/."""

    def test_detail_returns_invoice_with_lines_and_deliveries(self, admin_client, march_entries):
        run_resp = admin_client.post(RUNS_URL, {'month': '2026-03'}, format='json')
        first_entry, second_entry, _ = march_entries
        contract = first_entry.contract
        invoice = Invoice.objects.get(freelancer=contract.freelancer)

        resp = admin_client.get(detail_url(invoice.pk))

        assert resp.status_code == 200
        data = resp.json()
        assert {k: data[k] for k in [
            'id', 'reference', 'freelancer', 'freelancer_name', 'company_name', 'period',
            'currency', 'total', 'delivery_status', 'billing_run', 'company_billing_email',
        ]} == {
            'id': invoice.pk,
            'reference': invoice.reference,
            'freelancer': contract.freelancer_id,
            'freelancer_name': 'Test Freelancer',
            'company_name': 'NorthStar Consulting',
            'period': '2026-03',
            'currency': 'GBP',
            'total': '900.00',
            'delivery_status': 'pending',
            'billing_run': run_resp.json()['run_id'],
            'company_billing_email': 'billing@northstar.test',
        }
        assert 'created_at' in data

        line_ids = list(invoice.lines.values_list('pk', flat=True))
        assert data['lines'] == [
            {
                'id': line_ids[0], 'date': '2026-03-02', 'hours': '8.00', 'daily_rate': '600.00',
                'amount': '600.00', 'contract': contract.pk, 'timesheet_entry': first_entry.pk,
            },
            {
                'id': line_ids[1], 'date': '2026-03-03', 'hours': '4.00', 'daily_rate': '600.00',
                'amount': '300.00', 'contract': contract.pk, 'timesheet_entry': second_entry.pk,
            },
        ]
        assert data['deliveries'] == [
            {
                'recipient_type': 'company', 'email': 'billing@northstar.test', 'status': 'pending',
                'attempts': 0, 'last_error': '', 'last_attempt_at': None, 'sent_at': None,
            },
            {
                'recipient_type': 'freelancer', 'email': 'freelancer@test.test', 'status': 'pending',
                'attempts': 0, 'last_error': '', 'last_attempt_at': None, 'sent_at': None,
            },
        ]

    def test_detail_query_count_does_not_grow_with_lines(
        self, admin_client, bill, active_contract, make_entry, make_contract, second_freelancer,
    ):
        company = active_contract.company
        make_entry(active_contract, '2026-03-02')
        second_contract = make_contract(company, second_freelancer)
        for day in ('2026-03-02', '2026-03-03', '2026-03-04'):
            make_entry(second_contract, day)
        bill()
        one_line = Invoice.objects.get(freelancer=active_contract.freelancer)
        three_lines = Invoice.objects.get(freelancer=second_freelancer)
        assert (one_line.lines.count(), three_lines.lines.count()) == (1, 3)

        def count_detail_queries(invoice):
            with CaptureQueriesContext(connection) as ctx:
                resp = admin_client.get(detail_url(invoice.pk))
            assert resp.status_code == 200
            assert len(resp.json()['deliveries']) == 2
            return len(ctx.captured_queries)

        assert count_detail_queries(three_lines) == count_detail_queries(one_line)
