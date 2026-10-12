# Employee Attendance & Analytics API

FastAPI service for employee records, attendance punch-in/out, manual corrections, and monthly, department, leaderboard,
and daily trend analytics. The HTTP contract and business rules are in `openapi.yaml`; MongoDB document shapes are in
`DATA_MODEL.md`. All application code is in `app/main.py` as required by the assignment.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+. From the project root, in Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set MONGO_URI and MONGO_DB in .env; real environment variables take precedence.
uvicorn app.main:app --port 8000
```

On macOS/Linux, activate with `source .venv/bin/activate` and copy the example with `cp .env.example .env`. For Atlas,
set `MONGO_URI` to your cluster connection string. For local MongoDB, `docker run -p 27017:27017 mongo:7` starts a server.
Indexes are created automatically at app startup. Never commit `.env` or credentials.
The candidate kit's local `sample_seed.py` helper deletes and replaces both collections; it and the local `sample_data/`
files are excluded from the public submission. Use the helper only with a disposable database.

The API runs at `http://127.0.0.1:8000`; Swagger UI is `/docs`, ReDoc is `/redoc`, and readiness is `/health`.

## Routes

| Method | Path | Description |
|---|---|---|
| GET | `/health` | MongoDB readiness |
| POST, GET | `/employees` | Create and filter/page employee records |
| POST | `/attendance/punch-in` | Create a day's attendance record |
| POST | `/attendance/punch-out` | Close the latest eligible open record |
| GET | `/attendance` | Filter and page attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct a record and append audit history |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly summary |
| GET | `/analytics/departments/summary` | Monthly department summary |
| GET | `/analytics/leaderboard/late` | Monthly late leaderboard with competition ranks |
| GET | `/analytics/departments/{department}/trend` | Daily gap-filled department trend |
| GET | `/admin/explain/{endpoint}` | MongoDB execution statistics for supported queries |

Punch-in example (instants use epoch milliseconds):

```json
{"emp_code":"EMP0001","punched_at":1783312500000,"status":"PRESENT"}
```

Run tests with `python -m pytest -q`. They mock MongoDB; aggregation execution plans and database concurrency require a
MongoDB 6.0+ integration run. Authentication and a holiday calendar are outside the assignment contract.
