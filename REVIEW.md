# Starter Review

| # | Where | Defect and how to notice it | Fix |
|---|---|---|---|
| 1 | `compute_late_minutes` | Replacing punch-in clock fields ignores IST offsets and overnight attendance dates; an overnight punch is compared with the wrong shift start. | Convert instants to IST and compare with shift start on the R1 attendance date. |
| 2 | `compute_work_hours` | Built-in `round` uses ties-to-even, so exact half-cent-hour boundaries can disagree with R4. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Counts any positive time, omits the 30-minute threshold, and places overnight shift end on the wrong day. | Apply R3's threshold and calculate shift end on the correct local date. |
| 4 | `health` | Always returns ready, even when MongoDB is unreachable. | Ping MongoDB and return 503 on missing configuration or database failure. |
| 5 | `create_employee` | The pre-read duplicate check races; no unique-index guarantee exists. Creation time was naive local time and request validation was weak. | Validate inputs, store UTC, enforce a unique index, and map duplicate-key errors to 409. |
| 6 | `list_employees` | `skip(page * page_size)` skips page one, `total` ignores department filtering, and sorting/bounds are not enforced. | Use `(page - 1) * page_size`, count with the same filter, sort by code, and validate pagination. |
| 7 | `punch_in` | Unknown employees are dereferenced; invalid status/timestamp values pass; local naive time gives the wrong date; check-then-insert is race-prone. | Validate strictly, convert UTC/IST correctly, return 404, and rely on the unique natural-key index. |
| 8 | `punch_in` response | The response leaks MongoDB's generated `id`, contrary to the no-id natural-key contract. | Serialize only contract fields and never expose `_id`. |
| 9 | `list_attendance` | Loads and sorts the whole collection in Python, has no date validation, and returns the Mongo id; it will not scale to 100k records. | Count and page in MongoDB using indexed filters and the required sort. |
| 10 | Attendance ordering | Equal dates have no `emp_code` tie-breaker, so page contents can vary. | Sort by date descending and employee code ascending. |
| 11 | Request models | The starter does not enforce field patterns, bounds, enum values, or strict epoch-millisecond integers. | Add Pydantic v2 models and explicit calendar/timestamp validation. |
| 12 | Missing routes | Punch-out, regularization, four analytics routes, and explain are not implemented. | Implement the documented routes and cover request/response behavior in tests. |
| 13 | MongoDB lifecycle | No startup ping/indexes exist, and BSON dates are not converted at the API boundary. | Initialize indexes during lifespan, use timezone-aware PyMongo, and serialize instants as epoch milliseconds. |

**Checked and not a defect:** synchronous PyMongo is suitable because the route handlers are synchronous; the fix is to
use it consistently, not mix it with Motor. Calendar dates and shift times remaining strings matches the supplied data
model and avoids inventing a timezone for calendar-only values.
