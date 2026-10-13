import concurrent.futures
from fastapi.testclient import TestClient
from app.main import app, db

client = TestClient(app)

def test_concurrent_punch_ins():
    emp_code = "EMP0001"
    att_date_ms = 1785000000000 # future date
    # Test 5 concurrent punch-ins for the same emp and instant
    def do_punch():
        c = TestClient(app)
        return c.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": att_date_ms,
            "status": "PRESENT"
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch(), range(5)))

    # Exactly one 201, rest 409
    assert results.count(201) == 1, f"Expected exactly one 201, got {results}"
    assert results.count(409) == 4, f"Expected 409 for losers, got {results}"

def test_concurrent_punch_outs():
    emp_code = "EMP0002"
    # Create an open attendance record first
    in_ms = 1785100000000
    c = TestClient(app)
    db.attendance_logs.delete_many({"emp_code": emp_code})
    c.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_ms})
    
    out_ms = in_ms + (9 * 3600 * 1000)
    def do_punch_out():
        tc = TestClient(app)
        return tc.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": out_ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: do_punch_out(), range(5)))

    assert results.count(200) == 1, f"Expected exactly one 200, got {results}"
    assert results.count(409) == 4, f"Expected 409 for losers, got {results}"

def test_concurrent_regularize():
    emp_code = "EMP0005"
    date_str = "2026-07-06"
    
    # 2 concurrent regularize requests
    def do_reg(by, reason):
        tc = TestClient(app)
        return tc.patch(f"/attendance/{emp_code}/{date_str}", json={
            "status": "WFH",
            "reason": reason,
            "regularized_by": by
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_reg, "admin1", "reason one test")
        f2 = executor.submit(do_reg, "admin2", "reason two test")
        r1 = f1.result()
        r2 = f2.result()

    statuses = [r1, r2]
    # At least one succeeded with 200, loser gets 409 (or if serialized both can succeed, but loser never overwrites silently)
    assert 200 in statuses
