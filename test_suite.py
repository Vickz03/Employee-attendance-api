import pytest
from fastapi.testclient import TestClient
from app.main import app, db, init_indexes
import sample_seed

client = TestClient(app)

@pytest.fixture(autouse=True, scope="session")
def setup_db():
    init_indexes()

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_employees_crud():
    # 1. Create employee
    emp_payload = {
        "emp_code": "EMP9999",
        "name": "Test User",
        "email": "test.user@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01"
    }
    # Clean up if exists
    db.employees.delete_one({"emp_code": "EMP9999"})
    db.attendance_logs.delete_many({"emp_code": "EMP9999"})
    
    res = client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] > 100000000000

    # 2. Duplicate employee -> 409
    res_dup = client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # 3. Invalid shift (start == end) -> 422
    invalid_payload = dict(emp_payload, emp_code="EMP9998", shift_end="09:30")
    res_inv = client.post("/employees", json=invalid_payload)
    assert res_inv.status_code == 422

    # 4. List employees with department filter and pagination
    res_list = client.get("/employees?department=Engineering&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert "items" in list_data
    assert list_data["page"] == 1
    assert any(e["emp_code"] == "EMP9999" for e in list_data["items"])

def test_punch_in_out_flow():
    emp_code = "EMP9999"
    # Punch in at 09:40:01 IST (10 min late) on 2026-07-15
    # 2026-07-15 09:40:01 IST = 2026-07-15 04:10:01 UTC
    # epoch ms:
    # 2026-07-15T04:10:01Z = 1784088601000
    in_ms = 1784088601000
    res_in = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": in_ms,
        "status": "PRESENT"
    })
    assert res_in.status_code == 201
    record = res_in.json()
    assert record["emp_code"] == emp_code
    assert record["date"] == "2026-07-15"
    assert record["late_minutes"] == 10
    assert record["punch_out"] is None
    assert record["work_hours"] is None

    # Duplicate punch in for same date -> 409
    res_in_dup = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": in_ms + 10000,
        "status": "PRESENT"
    })
    assert res_in_dup.status_code == 409

    # Punch out at 19:10:00 IST (40 min overtime) on 2026-07-15
    # 2026-07-15 19:10:00 IST = 2026-07-15 13:40:00 UTC = 1784122800000
    out_ms = 1784122800000
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": out_ms
    })
    assert res_out.status_code == 200
    out_record = res_out.json()
    assert out_record["overtime_minutes"] == 40
    assert out_record["work_hours"] == 9.5
    assert out_record["half_day"] is False

    # Second punch out -> 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": out_ms + 1000
    })
    assert res_out_dup.status_code == 409

def test_regularize_patch():
    emp_code = "EMP9999"
    date_str = "2026-07-15"
    
    # 1. No-op patch -> 422
    res_noop = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "reason": "testing no change",
        "regularized_by": "admin"
    })
    assert res_noop.status_code == 422

    # 2. Change status to WFH
    res_patch = client.patch(f"/attendance/{emp_code}/{date_str}", json={
        "status": "WFH",
        "reason": "worked remotely in afternoon",
        "regularized_by": "manager"
    })
    assert res_patch.status_code == 200
    patch_data = res_patch.json()
    assert patch_data["status"] == "WFH"
    assert len(patch_data["history"]) == 1
    assert patch_data["history"][0]["by"] == "manager"
    assert patch_data["history"][0]["changes"]["status"]["from"] == "PRESENT"
    assert patch_data["history"][0]["changes"]["status"]["to"] == "WFH"

def test_attendance_list():
    res = client.get("/attendance?emp_code=EMP9999")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    assert data["items"][0]["emp_code"] == "EMP9999"

    # date_from > date_to -> 422
    res_inv = client.get("/attendance?date_from=2026-07-20&date_to=2026-07-10")
    assert res_inv.status_code == 422

def test_analytics_endpoints():
    # 1. Employee monthly
    res_m = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res_m.status_code == 200
    m_data = res_m.json()
    assert m_data["emp_code"] == "EMP0001"
    assert m_data["working_days"] == 23

    # Unknown employee -> 404
    res_404 = client.get("/analytics/employees/EMP9990/monthly?month=2026-07")
    assert res_404.status_code == 404

    # 2. Department summary
    res_dept = client.get("/analytics/departments/summary?month=2026-07")
    assert res_dept.status_code == 200
    dept_data = res_dept.json()
    assert "items" in dept_data
    assert len(dept_data["items"]) >= 1

    # 3. Leaderboard
    res_lead = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res_lead.status_code == 200
    lead_data = res_lead.json()
    assert "items" in lead_data

    # 4. Department trend
    res_trend = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-10")
    assert res_trend.status_code == 200
    trend_data = res_trend.json()
    assert len(trend_data["items"]) == 5

    # Trend invalid dates -> 422
    res_trend_inv = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-06")
    assert res_trend_inv.status_code == 422

    # Trend unknown dept -> 404
    res_trend_404 = client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-06&to=2026-07-10")
    assert res_trend_404.status_code == 404

def test_admin_explain():
    # attendance_list explain
    res1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001")
    assert res1.status_code == 200
    assert res1.json()["endpoint"] == "attendance_list"

    # employee_monthly explain
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200

    # missing param -> 422
    res_inv = client.get("/admin/explain/employee_monthly")
    assert res_inv.status_code == 422
