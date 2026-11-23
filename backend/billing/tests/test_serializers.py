"""Month validation for the billing request serializers."""
import datetime
from unittest import mock

import pytest

from billing.serializers import BillingRunRequestSerializer, MonthField

from billing.tests.data import MARCH


class TestMonthField:
    def test_parses_to_first_of_month(self):
        assert MonthField().run_validation('2026-03') == MARCH

    @pytest.mark.parametrize('value', ['', '2026-3', '2026-13', '2026-00', '0000-03', '03-2026',
                                       '2026-03-01', 202603])
    def test_rejects_malformed_values(self, value):
        serializer = BillingRunRequestSerializer(data={'month': value})

        assert not serializer.is_valid()
        assert serializer.errors == {'month': ["Month must be in 'YYYY-MM' format."]}


class TestCompletedMonthsOnly:
    @pytest.fixture(autouse=True)
    def today_is_mid_april(self):
        with mock.patch('django.utils.timezone.localdate', return_value=datetime.date(2026, 4, 15)):
            yield

    def test_completed_month_is_billable(self):
        serializer = BillingRunRequestSerializer(data={'month': '2026-03'})

        assert serializer.is_valid()
        assert serializer.validated_data == {'month': MARCH}

    @pytest.mark.parametrize('value', ['2026-04', '2026-05'])
    def test_current_and_future_months_are_rejected(self, value):
        serializer = BillingRunRequestSerializer(data={'month': value})

        assert not serializer.is_valid()
        assert 'Only completed months' in serializer.errors['month'][0]
