"""Constants shared by billing tests.

Kept out of conftest.py: pytest loads that itself and it shouldn't be imported.
"""
import datetime

MARCH = datetime.date(2026, 3, 1)
