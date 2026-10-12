from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app import main

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture

def api(monkeypatch):
    database = MagicMock()
    database.employees.name = "employees"
    database.attendance_logs.name = "attendance_logs"
    database.employees.database.command.return_value = {"ok": 1}
    database.attendance_logs.database.command.return_value = {"ok": 1}
    database.command.return_value = {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}
    mongo_client = MagicMock()
    mongo_client.admin.command.return_value = {"ok": 1}
    monkeypatch.setattr(main, "db", database)
    monkeypatch.setattr(main, "client", mongo_client)
    with TestClient(main.app) as test_client:
        yield test_client, database, mongo_client


def employee_document(**overrides):
    document = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=UTC),
    }
    document.update(overrides)
    return document


def attendance_document(**overrides):
    document = {
        "_id": "internal-only",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=UTC),
        "punch_out": datetime(2026, 7, 6, 13, 5, tzinfo=UTC),
        "work_hours": 9.12,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    document.update(overrides)
    return document


def epoch_ms(value):
    return int(value.timestamp()) * 1000


def test_time_rules_handle_ist_overnight_grace_and_half_up():
    day = date(2026, 7, 6)
    at_grace = datetime(2026, 7, 6, 4, 10, tzinfo=UTC)
    after_grace = at_grace + timedelta(seconds=1)
    overnight_after_midnight = datetime(2026, 7, 6, 19, 30, tzinfo=UTC)

    assert main.compute_late_minutes(at_grace, "09:30", day) == 0
    assert main.compute_late_minutes(after_grace, "09:30", day) == 10
    assert main.attendance_date_for(overnight_after_midnight, "22:00", "06:00") == date(2026, 7, 6)
    assert main.compute_work_hours(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 0, 18, tzinfo=UTC)) == 0.01
    assert main.compute_overtime(datetime(2026, 7, 6, 13, 0, tzinfo=UTC), "09:30", "18:30", day) == 0


def test_health_requires_database_ping(api):
    client, database, mongo_client = api
    index_names = {call.kwargs["name"] for call in database.employees.create_index.call_args_list}
    assert "employee_code_unique" in index_names
    assert "employee_joined_department" in index_names
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    mongo_client.admin.command.side_effect = main.PyMongoError("offline")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


def test_employee_create_list_and_validation(api):
    client, database, _ = api
    database.employees.insert_one.return_value = MagicMock()
    created = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    })
    assert created.status_code == 201
    assert created.json()["emp_code"] == "EMP0001"
    assert "_id" not in created.json()
    inserted = database.employees.insert_one.call_args.args[0]
    assert inserted["shift_start"] == "09:30"
    assert inserted["created_at"].tzinfo == UTC

    database.employees.insert_one.side_effect = DuplicateKeyError("duplicate")
    assert client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }).status_code == 409
    assert client.post("/employees", json={"emp_code": "bad"}).status_code == 422

    database.employees.count_documents.return_value = 1
    database.employees.find.return_value.sort.return_value.skip.return_value.limit.return_value = [employee_document()]
    listed = client.get("/employees", params={"department": "Engineering", "page": 2})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["page"] == 2
    database.employees.count_documents.assert_called_with({"department": "Engineering"})
    database.employees.find.return_value.sort.return_value.skip.assert_called_with(20)


def test_punch_in_is_validated_and_assigns_ist_attendance_date(api):
    client, database, _ = api
    database.employees.find_one.return_value = employee_document()
    punched_at = datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC)
    response = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": epoch_ms(punched_at),
        "status": "WFH",
    })
    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["late_minutes"] == 10
    assert response.json()["punch_in"] == epoch_ms(punched_at.replace(microsecond=0))
    assert "id" not in response.json()

    database.employees.find_one.return_value = None
    assert client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN"}).status_code == 404
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": None}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "LEAVE"}).status_code == 422


def test_attendance_list_filters_and_rejects_invalid_dates(api):
    client, database, _ = api
    database.attendance_logs.count_documents.return_value = 1
    database.attendance_logs.find.return_value.sort.return_value.skip.return_value.limit.return_value = [attendance_document()]
    response = client.get("/attendance", params={
        "emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT",
    })
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert "_id" not in response.json()["items"][0]
    database.attendance_logs.count_documents.assert_called_with({
        "emp_code": "EMP0001",
        "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
        "status": "PRESENT",
    })
    assert client.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-07-02", "date_to": "2026-07-01"}).status_code == 422
    assert client.get("/attendance", params={"page_size": 101}).status_code == 422


def test_punch_out_updates_record_and_detects_missing_or_closed(api):
    client, database, _ = api
    database.employees.find_one.return_value = employee_document()
    opened = attendance_document(punch_in=datetime(2026, 7, 6, 3, 58, tzinfo=UTC), punch_out=None, work_hours=None)
    database.attendance_logs.find_one.return_value = opened
    closed = {**opened, "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=UTC), "work_hours": 9.03}
    database.attendance_logs.find_one_and_update.return_value = closed
    response = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": epoch_ms(closed["punch_out"]),
    })
    assert response.status_code == 200
    assert response.json()["work_hours"] == 9.03
    assert response.json()["history"] == []
    update = database.attendance_logs.find_one_and_update.call_args.args[1]["$set"]
    assert update["overtime_minutes"] == 0

    database.attendance_logs.find_one.return_value = None
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    database.attendance_logs.find_one.return_value = {**opened, "punch_out": closed["punch_out"]}
    assert client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": epoch_ms(closed["punch_out"]),
    }).status_code == 409


def test_regularization_recomputes_and_appends_history(api):
    client, database, _ = api
    database.employees.find_one.return_value = employee_document()
    old_in = datetime(2026, 7, 6, 4, 35, tzinfo=UTC)
    old_out = datetime(2026, 7, 6, 13, 0, tzinfo=UTC)
    current = attendance_document(
        _id="record-key", punch_in=old_in, punch_out=old_out, work_hours=8.42, late_minutes=35,
    )
    corrected_in = datetime(2026, 7, 6, 3, 58, tzinfo=UTC)
    history_entry = {
        "at": datetime(2026, 7, 8, 5, 0, tzinfo=UTC),
        "by": "hr.admin",
        "reason": "biometric glitch",
        "changes": {
            "punch_in": {"from": old_in, "to": corrected_in},
            "late_minutes": {"from": 35, "to": 0},
            "work_hours": {"from": 8.42, "to": 9.03},
        },
    }
    database.attendance_logs.find_one.return_value = current
    database.attendance_logs.find_one_and_update.return_value = {
        **current,
        "punch_in": corrected_in,
        "late_minutes": 0,
        "work_hours": 9.03,
        "history": [history_entry],
    }
    response = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": epoch_ms(corrected_in), "reason": "biometric glitch", "regularized_by": "hr.admin",
    })
    assert response.status_code == 200
    assert response.json()["history"][0]["changes"]["punch_in"] == {
        "from": epoch_ms(old_in), "to": epoch_ms(corrected_in),
    }
    update = database.attendance_logs.find_one_and_update.call_args.args[1]
    assert update["$set"]["late_minutes"] == 0
    assert len(update["$push"]["history"]["changes"]) == 3


def test_regularization_rejects_noop_and_invalid_status_times(api):
    client, database, _ = api
    database.employees.find_one.return_value = employee_document()
    current = attendance_document(_id="record-key")
    database.attendance_logs.find_one.return_value = current
    response = client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "no change", "regularized_by": "hr.admin",
    })
    assert response.status_code == 422
    response = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "LEAVE", "punch_in": epoch_ms(current["punch_in"]),
        "reason": "mark leave", "regularized_by": "hr.admin",
    })
    assert response.status_code == 422
    assert client.patch("/attendance/EMP0001/not-a-date", json={
        "reason": "valid reason", "regularized_by": "hr.admin",
    }).status_code == 422


def test_monthly_analytics_runs_aggregation_and_returns_result(api):
    client, database, _ = api
    summary = {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 20, "present_days": 1,
        "leave_days": 1, "late_count": 2, "total_late_minutes": 35,
        "total_overtime_minutes": 40, "attendance_pct": 5.0,
    }
    database.employees.aggregate.return_value = iter([summary])
    response = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert response.status_code == 200
    assert response.json() == summary
    pipeline = database.employees.aggregate.call_args.args[0]
    assert pipeline[0] == {"$match": {"emp_code": "EMP0001"}}
    assert any("$lookup" in stage for stage in pipeline)
    database.employees.aggregate.return_value = iter([])
    assert client.get("/analytics/employees/EMP0001/monthly?month=2026-07").status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly?month=2026-13").status_code == 422


def test_department_summary_and_leaderboard_use_required_pipeline_stages(api):
    client, database, _ = api
    database.employees.aggregate.return_value = iter([])
    response = client.get("/analytics/departments/summary?month=2026-07")
    assert response.status_code == 200
    assert response.json() == {"month": "2026-07", "items": []}
    summary_pipeline = database.employees.aggregate.call_args.args[0]
    assert summary_pipeline[0]["$match"]["joined_on"] == {"$lte": "2026-07-31"}
    assert any("$lookup" in stage for stage in summary_pipeline)

    database.attendance_logs.aggregate.return_value = iter([])
    response = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert response.status_code == 200
    leaderboard = database.attendance_logs.aggregate.call_args.args[0]
    rank_stage = next(index for index, stage in enumerate(leaderboard) if "$setWindowFields" in stage)
    rank_filter = next(index for index, stage in enumerate(leaderboard) if stage.get("$match", {}).get("rank"))
    assert rank_stage < rank_filter
    assert leaderboard[rank_filter]["$match"]["rank"]["$lte"] == 2
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422


def test_department_trend_has_range_gap_fill_window_and_department_filter(api):
    client, database, _ = api
    database.employees.find_one.return_value = {"_id": "employee"}
    database.employees.aggregate.return_value = iter([{
        "date": "2026-07-06", "is_working_day": True, "headcount": 1,
        "present_count": 0, "late_count": 0, "attendance_rate": 0, "moving_avg_7d": 0,
    }])
    response = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-06")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    pipeline = database.employees.aggregate.call_args.args[0]
    assert pipeline[0] == {"$match": {"department": "Engineering"}}
    lookup = next(stage["$lookup"] for stage in pipeline if "$lookup" in stage)
    assert lookup["let"]["employee_codes"]["$map"]["input"] == "$employees"
    assert any("$setWindowFields" in stage for stage in pipeline)
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-10-01").status_code == 422
    database.employees.find_one.return_value = None
    assert client.get("/analytics/departments/Unknown/trend?from=2026-07-06&to=2026-07-06").status_code == 404


def test_explain_routes_require_parameters_and_dispatch_explain(api):
    client, database, _ = api
    database.employees.aggregate.return_value = iter([])
    database.attendance_logs.aggregate.return_value = iter([])

    response = client.get("/admin/explain/attendance_list?date_from=2026-07-01&page=2")
    assert response.status_code == 200
    assert response.json()["collection"] == "attendance_logs"
    assert response.json()["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"
    explain_command = database.command.call_args.args[1]
    assert explain_command["skip"] == 20
    assert explain_command["limit"] == 20
    assert explain_command["filter"] == {"date": {"$gte": "2026-07-01"}}
    assert client.get("/admin/explain/employee_monthly").status_code == 422

    requests = [
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-02",
    ]
    for path in requests:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["explain"] == {"ok": 1}


def test_openapi_exposes_exact_contract_routes_and_methods():
    schema = main.app.openapi()
    paths = schema["paths"]
    assert set(paths) == {
        "/health",
        "/employees",
        "/attendance/punch-in",
        "/attendance/punch-out",
        "/attendance",
        "/attendance/{emp_code}/{date}",
        "/analytics/employees/{emp_code}/monthly",
        "/analytics/departments/summary",
        "/analytics/leaderboard/late",
        "/analytics/departments/{department}/trend",
        "/admin/explain/{endpoint}",
    }
    assert set(paths["/attendance/punch-out"]) == {"post"}
    assert set(paths["/attendance/{emp_code}/{date}"]) == {"patch"}
    assert paths["/analytics/departments/{department}/trend"]["get"]["operationId"] == "departmentTrend"
    punch_at_schema = schema["components"]["schemas"]["PunchInRequest"]["properties"]["punched_at"]
    assert punch_at_schema["type"] == "integer"
    assert "anyOf" not in punch_at_schema
    assert punch_at_schema["minimum"] == main.MIN_EPOCH_MS
    assert punch_at_schema["maximum"] == main.MAX_EPOCH_MS
    assert "default" not in punch_at_schema
    assert schema["components"]["schemas"]["AttendanceResponse"]["properties"]["punch_in"]["anyOf"][-1] == {
        "type": "null",
    }
    attendance_parameters = {
        parameter["name"]: parameter["schema"]
        for parameter in paths["/attendance"]["get"]["parameters"]
    }
    assert attendance_parameters["status"]["enum"] == list(main.ALL_STATUSES)
    assert "anyOf" not in attendance_parameters["status"]
    assert attendance_parameters["date_from"]["format"] == "date"
    assert schema["components"]["schemas"]["AttendanceResponse"]["properties"]["work_hours"]["readOnly"] is True
    assert schema["components"]["schemas"]["EmployeeResponse"]["properties"]["created_at"]["readOnly"] is True
