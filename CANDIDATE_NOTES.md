What I built and why:
- Tasks B and C seemed more interesting, with multiple components that could fail.
- Went with B as it naturally felt more extensible. There's a lot this could scale up to so felt like a good compromise of a task that could have a fast solution now but should keep the big picture in mind.

Key implementation notes:
- Database models
  - A BillingRun records who triggered each run and when, for auditing.
  - An Invoice is one company × freelancer × month.
  - Each InvoiceLine links one-to-one to the TimesheetEntry it bills; InvoiceDelivery is an outbox row per email.
  - Unique constraints and one-to-one links mean our database ensures uniqueness, not just the code.
  - Invoices copy rates, hours and names at issue time, so later edits or retries can't change what was billed.
  - Money is Decimal throughout: hours ÷ 8 × day rate, each line rounded half-up, total is the sum of the rounded lines.
- Triggering of a billing run
  - Synchronously create the invoices as this is quick, along with pending deliveries to both emails.
  - After commit, try each email once, stopping at the first failure so an outage costs one timeout. Anything unsent is retried by a management command.
- Idempotency and transactions:
  - Every click records a BillingRun, but invoices are only created where none exist. One transaction with the company's row locked, so concurrent runs wait their turn.
  - The post-commit send is robust: if it fails, the run still succeeds and the emails wait for the retry.
  - Each delivery is claimed with a conditional update, so only one process can send it.
  - Delivery is at-least-once: an email is only marked sent after it actually sends, so a crash can cause a duplicate, never a lost email.
- Deliberately left out of scope:
  - Hours approved after a month is invoiced aren't dealt with. (Also meant a change to not allow triggering of current month)
  - Billed timesheet entries can still be edited, causing mismatches between invoice and current state
  - Any specific currencies and better hourly rate handling in the database models.
  - Email formatting, just kept it simple
  - Scheduling the management command, as there was no existing place for a cron and it depends on how this gets deployed.

My Workflow:
- Wrote down my own thoughts first (structure, async processing, idempotency, handling changes for billed months).
- Used a Claude skill to turn those notes into a plan (with clarifying questions) and a prompt for a fresh session, which ran sub-agents to write, research and review the code.
- AI worked well for edge cases but went beyond scope (an edit lock, extra validation, too many duplicate tests, lots of obtuse conditional logic), which I removed.
- Then my own review: a broad pass for flow and key logic, a code-review skill for targeted fixes, then an in-depth read for style and domain structure, which AI struggles with. Kept comments only where useful.
- Trimmed tests to the critical ones (money, key flows), removing ones that only test built ins or things really unlikely to change.

Next steps:
- If I had four hours, I'd first ask "Is it actually already good enough?". If so then I'd spend those four hours elsewhere!
- Some immediate parts I was considering but was running low on time are:
  - Reconsider primary keys for all of these. Probably UUIDs.
  - Spend more time reviewing tests and useful edge cases.
  - Model naming. The current names were sensible but I think could be simpler.
- Lock billed entries against edits and add supplementary invoices for late approvals: a big gap, likely done alongside Task A.
- Automatically generate invoices. Why does someone need to push a button? Just do it for them at the start of each new month covering the last one (with a few days' grace for late logging).
- Move email (and PDF generation) onto an event queue with workers, replacing the post-commit attempt and cron, so large batches scale.
