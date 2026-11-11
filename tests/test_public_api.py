from unittest.mock import patch
from contextlib import closing
from pathlib import Path
import sqlite3
import uuid

from test_support import DatabaseTests


class PublicApiTests(DatabaseTests):
    def test_accounts_and_list_routes_removed(self):
        for path, method in (("/api/register", "post"), ("/api/login", "post"),
                             ("/api/bookings", "get")):
            response = getattr(self.client, method)(path)
            self.assertIn(response.status_code, (404, 405))

    def test_create_without_login_accepts_guest_name(self):
        response = self.client.post("/api/bookings", headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"guest_name": "  陈 Alex  ", "room_id": "R101", "check_in": "2026-11-15",
                  "check_out": "2026-11-17", "guest_count": 2})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["guest_name"], "陈 Alex")
        self.assertEqual(set(response.json), {"booking_id", "guest_name", "room_id", "check_in",
            "check_out", "guest_count", "status", "notification_status", "created_at", "updated_at"})
        self.assertEqual(self.client.get("/api/bookings/" + response.json["booking_id"]).status_code, 200)

    def test_cors_and_error_json(self):
        response = self.client.options("/api/bookings", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Headers"], "Content-Type, Idempotency-Key")
        self.assertEqual(self.client.get("/missing").json["error"]["code"], "NOT_FOUND")

    def test_health_reports_own_storage_failure(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        with patch.object(self.module, "get_db", side_effect=sqlite3.OperationalError("offline")):
            self.assertEqual(self.client.get("/health").status_code, 503)

    def test_database_failure_is_safe_json(self):
        with patch.object(self.module.booking_workflow, "get_db", side_effect=sqlite3.OperationalError("private path")):
            response = self.client.get("/api/bookings/B001")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private path", response.get_data(as_text=True))

    def test_old_database_is_refused_without_modification(self):
        old_path = str(Path(self.folder.name) / "legacy.db")
        with closing(sqlite3.connect(old_path)) as db, db:
            db.execute("CREATE TABLE bookings (booking_id TEXT, customer_id TEXT)")
            db.execute("INSERT INTO bookings VALUES ('old-booking', 'old-customer')")
        with patch.dict(self.module.app.config, DATABASE=old_path):
            with self.assertRaisesRegex(RuntimeError, "old account schema"):
                self.module.init_db()
        with closing(sqlite3.connect(old_path)) as db:
            self.assertEqual(db.execute("SELECT * FROM bookings").fetchall(),
                             [('old-booking', 'old-customer')])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 1)
