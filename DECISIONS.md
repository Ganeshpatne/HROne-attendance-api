# Design Decisions

Here's why I did things the way I did:

## 1. Timestamps and timezones
All the timestamps in the database are stored as UTC BSON dates, and they come in/out of the API as epoch milliseconds. Shift times are in IST though, so whenever I need to figure out the attendance date or calculate late/overtime, I convert to IST first. For overnight shifts (like 22:00–06:00), if someone punches in after midnight but before the shift ends, I roll the date back by one day so it counts as the previous day's shift.

## 2. Rounding with Decimal (not float)
Python's built-in `round()` uses "banker's rounding" — so 8.125 rounds to 8.12 instead of 8.13. The spec says to use half-up rounding (the normal kind everyone learns in school). So I used Python's `Decimal` type with `ROUND_HALF_UP` everywhere — for work hours, attendance percentages, averages, etc. I also compute work hours from integer seconds (not float) to avoid floating point weirdness.

## 3. Race-safe updates (optimistic locking)
Instead of using app-level locks or semaphores, I use MongoDB's `find_one_and_update` for all state changes. For PATCH (manual corrections), I check the length of the `history` array in the query itself — if someone else edited the record between my read and my write, the array length won't match and the update just won't apply (returns 409 Conflict). This way two HR admins editing the same record at the same time won't overwrite each other.

For legacy records that don't have a `history` field at all, I use `$ifNull` to treat the missing field as an empty array, so it doesn't crash.

## 4. Analytics run inside MongoDB
All the heavy analytics (monthly summaries, department stats, leaderboard, trends) are done as MongoDB aggregation pipelines. This means the data never gets pulled into Python just to be crunched — MongoDB does the grouping, sorting, ranking, and averaging directly. I used `$setWindowFields` for the leaderboard ranking and the 7-day moving average, and `$densify` to fill in missing dates in the trend endpoint.

## 5. Indexes created at startup
I create all the composite indexes (like `(emp_code, date)`, `(department, joined_on)`, `(date, late_minutes)`) inside FastAPI's `lifespan` hook. It runs once when the server starts, and `create_index` is idempotent so it's safe to restart multiple times. This makes sure queries hit indexes instead of doing full collection scans, even on 100k+ records.
