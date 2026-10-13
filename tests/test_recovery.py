from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

import app.main as service
from tests.test_contract import employee


def test_initial_index_outage_retries_before_writes(mongo, monkeypatch):
    original = service.ensure_indexes
    attempts = []

    def fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise ServerSelectionTimeoutError("controlled startup failure")
        original()

    monkeypatch.setattr(service, "db", mongo)
    monkeypatch.setattr(service, "ensure_indexes", fail_once)
    with TestClient(service.app) as http:
        assert http.post("/employees", json=employee()).status_code == 201
        assert len(attempts) >= 2
        assert mongo.employees.index_information()["employee_code_uq"]["unique"]
        assert http.get("/health").status_code == 200


def test_ping_unavailable_returns_503(monkeypatch):
    # Dedicated closed port, never stops a user's MongoDB.
    connection = MongoClient(
        "mongodb://127.0.0.1:1", serverSelectionTimeoutMS=100, connectTimeoutMS=100
    )
    monkeypatch.setattr(service, "db", connection["hrone_test_unreachable"])
    try:
        with TestClient(service.app) as http:
            assert http.get("/health").status_code == 503
    finally:
        connection.close()
