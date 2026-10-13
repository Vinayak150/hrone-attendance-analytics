"""Synchronized requests over real TCP/HTTP against uvicorn and MongoDB."""

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from threading import Barrier, Event

import httpx
import pytest

from tests.test_contract import employee, ms


@pytest.fixture
def live(mongo):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ | {"MONGO_URI": os.environ["TEST_MONGO_URI"], "MONGO_DB": mongo.name}
    proc = subprocess.Popen(
        [str(Path(sys.executable).with_name("uvicorn")), "app.main:app", "--port", str(port)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            while time.monotonic() - started < 20:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode())
                try:
                    if http.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Readiness exceeded 20 seconds")
            yield http, mongo
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stderr.close()


def parallel(http, method, path, bodies):
    barrier = Barrier(len(bodies))

    def send(body):
        barrier.wait(timeout=10)
        return http.request(method, path, json=body)

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        return list(pool.map(send, bodies))


def test_repeated_concurrent_writes(live):
    http, db = live
    for iteration in range(10):
        code = f"EMP{iteration + 7000:04}"
        responses = parallel(http, "POST", "/employees", [employee(code)] * 8)
        assert sorted(r.status_code for r in responses) == [201] + [409] * 7
        assert db.employees.count_documents({"emp_code": code}) == 1
        body = {"emp_code": code, "punched_at": ms("2026-07-06T10:05:00")}
        responses = parallel(http, "POST", "/attendance/punch-in", [body] * 8)
        assert sorted(r.status_code for r in responses) == [201] + [409] * 7
        assert db.attendance_logs.count_documents({"emp_code": code}) == 1
        body = {"emp_code": code, "punched_at": ms("2026-07-06T19:00:00")}
        responses = parallel(http, "POST", "/attendance/punch-out", [body] * 8)
        assert sorted(r.status_code for r in responses) == [200] + [409] * 7
        row = db.attendance_logs.find_one({"emp_code": code})
        assert row["work_hours"] == 8.92 and row["history"] == []
        edits = [
            {
                "reason": f"correction {n}",
                "regularized_by": "hr",
                "punch_in": ms(f"2026-07-06T09:{30 + n:02}:00"),
            }
            for n in range(8)
        ]
        responses = parallel(http, "PATCH", f"/attendance/{code}/2026-07-06", edits)
        assert all(r.status_code in (200, 409) for r in responses)
        wins = [r for r in responses if r.status_code == 200]
        assert wins
        row = db.attendance_logs.find_one({"emp_code": code})
        assert len(row["history"]) == len(wins)
        assert {h["reason"] for h in row["history"]} == {
            r.json()["history"][-1]["reason"] for r in wins
        }
        previous = ms("2026-07-06T10:05:00")
        for entry in row["history"]:
            change = entry["changes"]["punch_in"]
            assert int(change["from"].timestamp() * 1000) == previous
            previous = int(change["to"].timestamp() * 1000)
        assert int(row["punch_in"].timestamp() * 1000) == previous
        seconds = int((row["punch_out"] - row["punch_in"]).total_seconds())
        assert row["work_hours"] == float(
            (Decimal(seconds) / 3600).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        assert row["late_minutes"] == 0 and row["overtime_minutes"] == 30
        assert row["half_day"] is False and row["status"] == "PRESENT"


def test_restart_preserves_indexes(mongo):
    # Seed directly while only default _id indexes exist, then launch twice.
    mongo.employees.insert_one(employee())
    mongo.attendance_logs.insert_one(
        {"emp_code": "EMP1001", "date": "2026-07-06", "status": "ABSENT"}
    )
    assert set(mongo.employees.index_information()) == {"_id_"}
    for _ in range(2):
        gen = live.__wrapped__(mongo)
        try:
            http, _ = next(gen)
            assert http.get("/health").json() == {"status": "ok"}
            assert mongo.employees.index_information()["employee_code_uq"]["unique"]
            assert mongo.attendance_logs.index_information()["attendance_natural_key_uq"]["unique"]
        finally:
            try:
                next(gen)
            except StopIteration:
                pass


def test_regularization_same_snapshot_conflicts(api, monkeypatch):
    from pymongo.synchronous.collection import Collection

    http, db = api
    assert http.post("/employees", json=employee()).status_code == 201
    assert (
        http.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T10:05:00")},
        ).status_code
        == 201
    )
    original = Collection.find_one
    barrier = Barrier(2)

    def synchronized_read(collection, *args, **kwargs):
        result = original(collection, *args, **kwargs)
        if collection.full_name == db.attendance_logs.full_name:
            barrier.wait(timeout=10)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Collection, "find_one", synchronized_read)
        edits = [
            {
                "reason": "controlled race",
                "regularized_by": "hr",
                "punch_in": ms(f"2026-07-06T09:{minute}:00"),
            }
            for minute in (30, 35)
        ]
        results = parallel(http, "PATCH", "/attendance/EMP1001/2026-07-06", edits)
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(db.attendance_logs.find_one()["history"]) == 1


@pytest.mark.parametrize("interleaving", ["punch_out", "correction", "history_cycle"])
def test_punch_out_and_correction_interleavings(api, monkeypatch, interleaving):
    """Hold one write after its read; a different endpoint commits first."""
    from pymongo.synchronous.collection import Collection

    http, db = api
    assert http.post("/employees", json=employee()).status_code == 201
    assert (
        http.post(
            "/attendance/punch-in",
            json={"emp_code": "EMP1001", "punched_at": ms("2026-07-06T09:30:00")},
        ).status_code
        == 201
    )
    correction_path = "/attendance/EMP1001/2026-07-06"
    metadata = {"reason": "correct clock", "regularized_by": "hr"}
    out = {"emp_code": "EMP1001", "punched_at": ms("2026-07-06T19:00:00")}
    edit = metadata | {"punch_in": ms("2026-07-06T09:45:00")}
    method, path, body = (
        ("PATCH", correction_path, edit)
        if interleaving == "correction"
        else ("POST", "/attendance/punch-out", out)
    )
    ready, release = Event(), Event()
    original = Collection.find_one_and_update

    def pause_first_write(collection, *args, **kwargs):
        if collection.full_name == db.attendance_logs.full_name and not ready.is_set():
            ready.set()
            assert release.wait(timeout=10), "test did not release the stale write"
        return original(collection, *args, **kwargs)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(Collection, "find_one_and_update", pause_first_write)
        pending = pool.submit(http.request, method, path, json=body)
        try:
            assert ready.wait(timeout=10), "request did not reach its write"
            if interleaving == "correction":
                assert http.post("/attendance/punch-out", json=out).status_code == 200
            else:
                assert (
                    http.patch(
                        correction_path,
                        json=metadata | {"punch_in": ms("2026-07-06T10:00:00")},
                    ).status_code
                    == 200
                )
                if interleaving == "history_cycle":
                    # Times/status return to the snapshot; only history exposes the change.
                    assert (
                        http.patch(
                            correction_path,
                            json=metadata | {"punch_in": ms("2026-07-06T09:30:00")},
                        ).status_code
                        == 200
                    )
            committed = db.attendance_logs.find_one()
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 409
    assert db.attendance_logs.find_one() == committed

    response = http.request(method, path, json=body)
    assert response.status_code == 200
    row = db.attendance_logs.find_one()
    expected_hours = {"punch_out": 9.0, "correction": 9.25, "history_cycle": 9.5}
    expected_late = {"punch_out": 30, "correction": 15, "history_cycle": 0}
    assert row["work_hours"] == expected_hours[interleaving]
    assert row["late_minutes"] == expected_late[interleaving]
    assert row["overtime_minutes"] == 30 and row["half_day"] is False
    assert row["history"][: len(committed["history"])] == committed["history"]
    assert len(row["history"]) == (2 if interleaving == "history_cycle" else 1)
