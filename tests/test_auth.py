from contextlib import closing
import hashlib
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the learning database untouched during imports and tests.
        cls.folder = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.folder.name) / "test.db")
        real_connect = sqlite3.connect
        with patch("sqlite3.connect", side_effect=lambda *a, **kw: real_connect(cls.db_path)):
            cls.module = importlib.import_module("booking.app")
        cls.module.app.config.update(TESTING=True, DATABASE=cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        db = sqlite3.connect(self.db_path)
        for table in ("sessions", "users", "bookings"):
            if db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                db.execute(f"DELETE FROM {table}")
        db.commit()
        db.close()
        self.client = self.module.app.test_client()

    def register(self, username="alice", password="ExamplePass123"):
        return self.client.post("/api/register", json={"username": username, "password": password})

    def login(self, username="alice", password="ExamplePass123"):
        return self.client.post("/api/login", json={"username": username, "password": password})

    def test_register_then_login(self):
        registered = self.register()
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(set(registered.json), {"customer_id", "username"})
        self.assertEqual(registered.json["username"], "alice")
        logged_in = self.login()
        self.assertEqual(logged_in.status_code, 200)
        self.assertEqual(set(logged_in.json), {"token", "customer_id", "expires_at"})
        self.assertEqual(logged_in.json["customer_id"], registered.json["customer_id"])
        self.assertTrue(logged_in.json["expires_at"].endswith("Z"))
        headers = {"X-Customer-Token": logged_in.json["token"]}
        self.assertEqual(self.client.get("/api/bookings", headers=headers).json, {"bookings": []})

    def test_duplicate_username_is_case_insensitive(self):
        self.assertEqual(self.register().status_code, 201)
        response = self.register(" ALICE ")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"]["code"], "USERNAME_TAKEN")

    def test_registration_validation(self):
        for data in ({}, [], None, {"username": "aa", "password": "longpassword"},
                     {"username": "alice", "password": "short"},
                     {"username": "alice", "password": None},
                     {"username": 123, "password": "longpassword"},
                     {"username": "alice", "password": "longpassword", "customer_id": "fake"},
                     {"username": "alice", "password": "x" * 129}):
            response = self.client.post("/api/register", json=data) if data is not None else self.client.post("/api/register", data="null", content_type="application/json")
            self.assertEqual(response.status_code, 400, data)
            self.assertEqual(response.json["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(self.client.post("/api/register", data="bad", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/register", data="username=alice").status_code, 415)

    def test_wrong_credentials_and_unknown_user_match(self):
        self.register()
        wrong = self.login(password="WrongPass123")
        unknown = self.login(username="nobody")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json, unknown.json)

    def test_missing_invalid_and_expired_tokens(self):
        self.assertEqual(self.client.get("/api/bookings").status_code, 401)
        self.assertEqual(self.client.get("/api/bookings", headers={"X-Customer-Token": "invalid"}).status_code, 401)
        self.register()
        token = self.login().json["token"]
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE sessions SET expires_at = ?", ("2000-01-01T00:00:00Z",))
        self.assertEqual(self.client.get("/api/bookings", headers={"X-Customer-Token": token}).status_code, 401)

    def test_password_and_token_are_hashed(self):
        self.register()
        token = self.login().json["token"]
        with closing(sqlite3.connect(self.db_path)) as db, db:
            password_hash = db.execute("SELECT password_hash FROM users").fetchone()[0]
            token_hash = db.execute("SELECT token_hash FROM sessions").fetchone()[0]
        self.assertNotEqual(password_hash, "ExamplePass123")
        self.assertTrue(self.module.check_password_hash(password_hash, "ExamplePass123"))
        self.assertEqual(token_hash, hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(self.login().json["token"], token)

    def test_customers_see_only_their_orders_in_order(self):
        alice = self.register().json["customer_id"]
        bob = self.register("bob").json["customer_id"]
        with closing(sqlite3.connect(self.db_path)) as db, db:
            for booking_id, customer_id, created_at in (("A1", alice, "2026-11-01T00:00:00Z"), ("A2", alice, "2026-11-02T00:00:00Z"), ("A3", alice, "2026-11-02T00:00:00Z"), ("B1", bob, "2026-11-03T00:00:00Z")):
                db.execute("INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (booking_id, customer_id, "R101", "2026-11-10", "2026-11-12", 2, "confirmed", "RS1", 24000, "MYR", "pending", created_at, created_at))
        a_token = self.login().json["token"]
        b_token = self.login("bob").json["token"]
        a_rows = self.client.get("/api/bookings", headers={"X-Customer-Token": a_token}).json["bookings"]
        b_rows = self.client.get("/api/bookings", headers={"X-Customer-Token": b_token}).json["bookings"]
        self.assertEqual([r["booking_id"] for r in a_rows], ["A3", "A2", "A1"])
        self.assertEqual([r["booking_id"] for r in b_rows], ["B1"])
        self.assertTrue(all(r["customer_id"] == alice for r in a_rows))

    def test_cors_and_json_errors(self):
        response = self.client.options("/api/bookings", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Headers": "X-Customer-Token"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:3000")
        denied = self.client.options("/api/bookings", headers={"Origin": "http://untrusted.example"})
        self.assertNotIn("Access-Control-Allow-Origin", denied.headers)
        self.assertEqual(self.client.get("/missing").json["error"]["code"], "NOT_FOUND")
        self.assertEqual(self.client.get("/api/register").json["error"]["code"], "METHOD_NOT_ALLOWED")

    def test_health_checks_own_database(self):
        self.assertEqual(self.client.get("/health").json, {"service": "booking", "status": "ok"})
        with patch.object(self.module, "get_db", side_effect=sqlite3.OperationalError("unavailable")):
            response = self.client.get("/health")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json, {"service": "booking", "status": "error"})


if __name__ == "__main__":
    unittest.main()
