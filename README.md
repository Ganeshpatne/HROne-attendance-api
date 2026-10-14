# Employee Attendance & Analytics API

## What is this?
This is a backend API I built for the HROne assignment. It's a single-file FastAPI app that talks to MongoDB. Basically it handles employee attendance — punch-ins, punch-outs, manual corrections, and a bunch of analytics like monthly summaries, department trends, and a "most late" leaderboard.

## What it does
- **Punch in / punch out** — employees can clock in and out, and the system auto-calculates late minutes, work hours, overtime, and half-day status
- **Manual corrections (PATCH)** — HR can fix attendance records with full audit history, and it won't break if two people try to edit the same record at once
- **Analytics** — monthly employee reports, department summaries, late leaderboard, and daily attendance trends with 7-day moving averages
- **Overnight shifts** — handles shifts like 22:00–06:00 properly (the date belongs to the day the shift started)
- **IST timezone** — all shift times and dates are in IST (UTC+05:30)

## How to run it

You need **Python 3.11+** and **MongoDB 7.0+** running locally.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) Set env variables — defaults work fine for local dev
# MONGO_URI="mongodb://localhost:27017"
# MONGO_DB="attendance_db"

# 3. Start the server
uvicorn app.main:app --port 8000
```

The server creates all the database indexes automatically when it starts up, so you don't need to do anything manually.

## Project structure
```
app/main.py          — the entire API (single file, as required)
openapi.yaml         — the API spec we had to follow
DATA_MODEL.md        — describes what's stored in MongoDB
DECISIONS.md         — why I made certain design choices
REVIEW.md            — bugs I found and fixed in the starter code
sample_data/         — sample MongoDB documents
requirements.txt     — Python dependencies
```

## Running tests
```bash
python test_edge_cases.py
python test_all_requirements.py
python prove_100k_indexes.py   # checks that queries use indexes
```
