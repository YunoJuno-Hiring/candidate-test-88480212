"""Send pending invoice emails and retry failed ones.

In production this would run on a schedule (e.g. cron).
"""
from typing import Any

from django.core.management.base import BaseCommand

from billing.delivery import MAX_ATTEMPTS, count_needing_follow_up, process_outbox


class Command(BaseCommand):
    help = (
        'Send pending invoice emails and retry failed ones (up to '
        f'{MAX_ATTEMPTS} attempts), stopping at the first failed send.'
    )

    def handle(self, *args: Any, **options: Any) -> None:
        summary = process_outbox()
        follow_up = count_needing_follow_up()

        message = f'Sent {summary.sent}.'
        if summary.failed_id is not None:
            message += (
                f' Delivery {summary.failed_id} failed; stopped with'
                f' {summary.deferred} left for the next run.'
            )
        if follow_up:
            message += f' {follow_up} failed deliveries cannot be retried and need manual follow-up.'

        failed = summary.failed_id is not None or follow_up
        self.stdout.write((self.style.WARNING if failed else self.style.SUCCESS)(message))
