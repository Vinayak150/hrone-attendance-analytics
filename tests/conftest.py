"""Each integration test owns a NEW random database; existing databases are never dropped."""

import os
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as service


@contextmanager
def disposable_database():
    uri = os.getenv("TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set TEST_MONGO_URI to run real MongoDB tests")
    connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=2500)
    connection.admin.command("ping")
    name = "hrone_test_" + uuid.uuid4().hex
    assert name != os.getenv("MONGO_DB")
    assert name not in connection.list_database_names()
    database = connection[name]
    try:
        yield database
    finally:
        # Only this newly generated database can be removed.
        assert name.startswith("hrone_test_") and len(name) == 43
        connection.drop_database(name)
        connection.close()


@pytest.fixture
def mongo():
    with disposable_database() as database:
        yield database


@pytest.fixture
def api(mongo, monkeypatch):
    monkeypatch.setattr(service, "db", mongo)
    with TestClient(service.app) as http:
        yield http, mongo
