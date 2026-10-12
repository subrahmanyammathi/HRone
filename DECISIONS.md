# Engineering Decisions

Indexes cover unique employee codes, department/code employee listings, department and joined-date analytics, and the
attendance natural key `(emp_code, date)`. Attendance also has date/code, status/date/code, and employee/punch-time
indexes for listing, filtered analytics, and punch-out lookup. I considered a standalone `late_minutes` index but
rejected it: leaderboard requests first constrain a month, so the date-leading index is more useful.

For concurrent punch-ins, both requests may reach the insert. The unique `(emp_code, date)` index is authoritative:
MongoDB accepts one insert and rejects the other with `DuplicateKeyError`, which the API maps to 409. The earlier read is
not used as the concurrency guarantee.

The leaderboard groups totals, applies MongoDB `$rank` over total minutes, and filters `rank <= limit`; employees tied
at the cutoff are all returned with the same rank. The final sort uses `emp_code` for deterministic tie order.

Department summary starts from eligible employees and looks up logs while preserving employees with no matching logs;
headcount is accumulated before log metrics, so zero-log people count. At 100 times the data, I would materialize daily
department aggregates and refresh them incrementally, trading some freshness and write complexity for lower query cost.
