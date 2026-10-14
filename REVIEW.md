# REVIEW.md — Bugs I Found and Fixed

| # | Where | What was wrong | How I caught it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Didn't handle IST or overnight shifts. Didn't truncate seconds. Used `int()` instead of `floor()` so edge cases like 09:40:01 gave 0 late instead of 10. | Tested with `punch_in` at 09:40:01 on a 09:30 shift — got 0 late instead of 10. | Rewrote the helper: convert to IST, compute shift start as a proper IST datetime, handle overnight date rollback, use `floor()`. |
| 2 | `compute_work_hours` | Used Python's `round()` which does banker's rounding (8.125 → 8.12 instead of 8.13). Didn't truncate seconds. | Checked 4.495 hours — could silently round wrong way. | Truncate seconds first, compute from integer seconds, use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Broke on overnight shifts (shift end is next day). No 30-minute minimum check. Used `int()` instead of `floor()`. | Tested overnight shift punch-out. Also 25 mins overtime returned 25 instead of 0. | Compute correct IST shift end (add 1 day if overnight), check ≥ 30 min threshold, use `floor()`. |
| 4 | `db` initialization | PyMongo client was naive (no timezone awareness). | Dates stored/retrieved had no tzinfo, causing silent timezone bugs. | Set `tz_aware=True` when creating the MongoClient. |
| 5 | `create_employee` | Race condition: check-then-insert on `emp_code`. No input validation. Used local time for `created_at`. Returned raw Mongo objects. | Two concurrent requests with same `emp_code` both got 201. Invalid emails accepted. | Added unique index, catch `DuplicateKeyError` for 409. Added Pydantic validation. Use UTC. Convert timestamps in response. |
| 6 | `list_employees` | Pagination was off by one (`skip = page * page_size`). `total` ignored filters. No sort. Raw datetimes in response. | `page=1` skipped the first 20 records. `total` didn't match filtered results. | Fixed offset to `(page - 1) * page_size`. Pass filter to `count_documents`. Sort by `emp_code`. Convert `created_at`. |
| 7 | `punch_in` | No 404 for unknown employees. Date calculation ignored IST/overnight. Race condition on concurrent punches. Returned internal `"id"`. | Non-existent `emp_code` → 500 crash. Overnight punch got wrong date. | Check employee exists first. Compute IST date properly. Unique index on `(emp_code, date)`. Removed `"id"`. |
| 8 | `list_attendance` | Loaded the entire collection into memory, then sorted/sliced in Python. Wrong sort order. No date validation. Raw datetimes leaked. | Super slow on 100k records. `date_from > date_to` silently accepted. | Use `find().sort().skip().limit()`. Validate dates. Convert BSON dates to epoch ms. Set defaults for legacy records. |
| 9 | `health` | Didn't actually ping MongoDB. | DB could be down and health still returned 200. | Added `db.command("ping")` and return 503 on failure. |
| 10 | `lifespan` | Didn't exist at all. | No indexes created on startup. | Added FastAPI lifespan context manager to create indexes idempotently. |

**Stuff I checked and left alone:**
- PyMongo synchronous client — fine per the requirements
- FastAPI default error responses (`{"detail": ...}`) — matches the spec
