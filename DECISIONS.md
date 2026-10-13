# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
   We created a unique index on `employees.emp_code` to guarantee identifier uniqueness and back fast lookups, and on `employees.department` and `employees.joined_on` to optimize department filtering and period headcount aggregations. For `attendance_logs`, we created a compound unique index on `(emp_code, date)` to enforce the single-record-per-day constraint under concurrency, a compound index on `(date: -1, emp_code: 1)` to support sorted pagination in `GET /attendance`, and `(date: 1, late_minutes: 1)` to accelerate leaderboard and trend aggregation matches. We considered and rejected a separate single index on `attendance_logs.emp_code` because prefix lookups are already covered by the `(emp_code, date)` index.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
   Both requests compute the same attendance date. Both issue an `insert_one()` call to MongoDB. Due to the compound unique index on `(emp_code, date)`, MongoDB's WiredTiger engine atomically inserts the first document and raises a duplicate key error (`code 11000`) for the second. Our code catches `DuplicateKeyError` and immediately maps it to HTTP 409, guaranteeing exactly one 201 response and one 409 conflict with zero data corruption.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
   Our leaderboard returns all tied employees at the cutoff rank, meaning the result can have more items than `limit`. The pipeline uses standard competition ranking via `$rank` (e.g., 1, 2, 2, 4), and filters with `rank <= limit`. Because the contract specifies that `limit` applies to the computed rank and not the row count, both tied employees qualify. Within the same rank, rows are deterministically ordered by `emp_code` ascending.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
   The department summary aggregation pipeline roots its query in the `employees` collection rather than `attendance_logs`. It matches all employees where `joined_on <= month_end`, groups by `department` to establish baseline `headcount` and employee codes, and performs a `$lookup` to join the monthly logs. Departments and employees with zero matching logs are preserved with full headcount, accurately contributing zero present days.

5. **One thing you would change** if this had to serve 100x the data.
   At 100x scale (~10 million records), running `$densify` and cross-collection `$lookup` aggregations in real-time on every analytics request would become expensive. We would maintain pre-aggregated daily department rollups and employee monthly summary documents updated asynchronously via MongoDB Change Streams or atomic increments at punch-out/regularization time, enabling sub-millisecond O(1) reads for all reporting dashboards.
