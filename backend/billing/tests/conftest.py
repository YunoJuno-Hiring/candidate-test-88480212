"""Shared fixtures for billing tests."""
import datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from accounts.models import Freelancer
from billing.services import run_billing
from billing.tests.data import MARCH
from contracts.models import Contract, TimesheetEntry


@pytest.fixture
def bill(northstar, admin_user):
    """bill(period=MARCH, company=northstar) -> BillingRun"""
    def _bill(period=MARCH, company=northstar):
        return run_billing(company=company, triggered_by=admin_user, period=period)
    return _bill


@pytest.fixture
def make_entry():
    """make_entry(contract, 'YYYY-MM-DD' | date, hours='8.0', status='approved')"""
    def _make(contract, date, hours='8.0', status=TimesheetEntry.STATUS_APPROVED):
        if isinstance(date, str):
            date = datetime.date.fromisoformat(date)
        return TimesheetEntry.objects.create(
            contract=contract, date=date, hours=Decimal(hours), status=status
        )
    return _make


@pytest.fixture
def second_freelancer():
    user = User.objects.create_user(
        username='second@test.test', email='second@test.test', password='testpass'
    )
    return Freelancer.objects.create(user=user, name='Second Freelancer')


@pytest.fixture
def make_contract():
    """make_contract(company, freelancer, rate='450.00', status='active')"""
    def _make(company, freelancer, rate='450.00', status=Contract.STATUS_ACTIVE):
        return Contract.objects.create(
            company=company,
            freelancer=freelancer,
            daily_rate=Decimal(rate),
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status=status,
        )
    return _make
