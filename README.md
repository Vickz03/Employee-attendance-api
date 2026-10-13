# Employee Attendance & Analytics API

A robust FastAPI backend for employee attendance tracking, regularization audits, and MongoDB aggregation analytics. Built for high concurrency, strict contract compliance, and deterministic data handling.

## Technology Stack
- **Python**: 3.11+ (Tested on Python 3.13)
- **Framework**: FastAPI, Pydantic v2
- **Database**: MongoDB 6.0+ (Tested on MongoDB 8.0)
- **Driver**: PyMongo 4.7+
- **Testing**: Pytest, HTTPX

## Prerequisites
- Python 3.11 or newer
- MongoDB 6.0 or newer running locally (`localhost:27017`) or on MongoDB Atlas

## Installation & Setup
1. Clone the repository and navigate to the project directory:
   ```bash
   cd candidate_kit
   ```
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # On Windows:
   .venv\Scripts\activate
   # On Linux/macOS:
   source .venv/bin/activate
   ```
3. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Configure environment variables (optional, defaults provided):
   ```bash
   # MONGO_URI defaults to mongodb://localhost:27017
   # MONGO_DB defaults to attendance_db
   ```

## Running the Application
Start the FastAPI server:
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Seeding Sample Data
To load the sample dataset into MongoDB:
```bash
python sample_seed.py
```

## Running Tests
Run the comprehensive test suite and concurrency verifications:
```bash
pytest test_suite.py test_concurrency.py
```

## Implemented Endpoints
- **System**:
  - `GET /health`: Readiness probe with live MongoDB ping (503 on database down).
- **Employees**:
  - `POST /employees`: Create employee with uniqueness check, shift validation, and instant serialization.
  - `GET /employees`: Filter by department, deterministic sort, and pagination.
- **Attendance**:
  - `POST /attendance/punch-in`: Race-safe punch-in with IST calendar date derivation, overnight shift handling, and late minutes calculation.
  - `POST /attendance/punch-out`: Atomic state transition, work hours (half-up rounding), overtime, and half-day computation.
  - `GET /attendance`: Database-level indexed filtering, descending date sort, and pagination.
  - `PATCH /attendance/{emp_code}/{date}`: Manual regularization with full audit history tracking and optimistic locking.
- **Analytics (MongoDB Aggregation Pipelines)**:
  - `GET /analytics/employees/{emp_code}/monthly`: Working days, present days, leave count, late minutes, and attendance percentage.
  - `GET /analytics/departments/summary`: Cross-collection aggregation including zero-log employees, headcount, and mean work hours.
  - `GET /analytics/leaderboard/late`: Standard competition ranking (`$rank`) with tie handling and limit filtering.
  - `GET /analytics/departments/{department}/trend`: Gap filling across date ranges via `$densify` and 7-day moving averages via `$setWindowFields`.
- **Admin**:
  - `GET /admin/explain/{endpoint}`: MongoDB query execution plan explain endpoint with `executionStats`.
