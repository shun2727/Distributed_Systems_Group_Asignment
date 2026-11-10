from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import socket
import sqlite3
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import test_support as support


class BookingStub(BaseHTTPRequestHandler):
    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = json.loads(raw) if raw else None
        with self.server.lock:
            self.server.calls.append(("POST", self.path, data, dict(self.headers)))
            if self.headers.get("X-Internal-Key") != "test-key":
                return self.reply(401, {"error": {"code": "UNAUTHORIZED"}})
            if self.path == "/internal/notifications":
                if self.server.notification_down:
                    return self.reply(503, {})
                first = data["event_id"] not in self.server.events
                self.server.events[data["event_id"]] = data
                return self.reply(201 if first else 200, {"event_id": data["event_id"], "status": "recorded"})
            if self.path.startswith("/internal/reservations/") and self.path.endswith("/release"):
                if self.server.availability_down:
                    return self.reply(503, {})
                booking_id = self.path.split("/")[-2]
                self.server.reservations.setdefault(booking_id, {"booking_id": booking_id})["status"] = "released"
                if getattr(self.server, "lose_release_reply", False):
                    self.server.lose_release_reply = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if getattr(self.server, "bad_release_reply", False):
                    return self.reply(200, {"booking_id": "wrong", "status": "released"})
                return self.reply(200, {"booking_id": booking_id, "status": "released"})
            if self.path != "/internal/reservations":
                return self.reply(404, {})
            if self.server.availability_down:
                return self.reply(503, {})
            if self.server.reject:
                return self.reply(409, {"error": {"code": "ROOM_UNAVAILABLE", "message": "Room unavailable."}})
            if self.server.reservations.get(data["booking_id"], {}).get("status") == "released":
                return self.reply(409, {"error": {"code": "RESERVATION_RELEASED"}})
            first = data["booking_id"] not in self.server.reservations
            reservation = self.server.reservations.setdefault(data["booking_id"], dict(
                data, status="reserved"))
            if self.server.lose_reply:
                self.server.lose_reply = False
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.server.bad_reply:
                return self.reply(201, dict(reservation, room_id="wrong-room"))
            return self.reply(201 if first else 200, reservation)

    def do_GET(self):
        with self.server.lock:
            self.server.calls.append(("GET", self.path, None, dict(self.headers)))
            if self.server.availability_down:
                return self.reply(503, {})
            booking_id = self.path.rsplit("/", 1)[-1]
            value = self.server.reservations.get(booking_id)
            if value is None:
                return self.reply(404, {"error": {"code": "NOT_FOUND"}})
            return self.reply(200, value)

    def log_message(self, *args):
        pass


class BookingCreateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        support.DatabaseTests.setUpClass.__func__(cls)
        cls.module.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BookingStub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        support.DatabaseTests.tearDownClass.__func__(cls)


    def setUp(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            for name in ("notification_events", "booking_requests"):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone():
                    db.execute(f"DELETE FROM {name}")
        support.DatabaseTests.setUp(self)
        self.key = str(uuid.uuid4())
        self.payload = {"guest_name": "Alex Tan", "room_id": "R101", "check_in": "2026-11-10", "check_out": "2026-11-12", "guest_count": 2}
        self.server.lock = threading.Lock()
        self.server.calls = []
        self.server.reservations = {}
        self.server.events = {}
        self.server.reject = self.server.availability_down = self.server.notification_down = False
        self.server.lose_reply = self.server.bad_reply = False
        url = f"http://127.0.0.1:{self.server.server_port}"
        self.module.app.config.update(AVAILABILITY_URL=url, NOTIFICATION_URL=url,
            INTERNAL_KEY="test-key", AVAILABILITY_TIMEOUT=.5, NOTIFICATION_TIMEOUT=.5)

    def create(self, key=None, payload=None, client=None):
        return (client or self.client).post("/api/bookings", json=payload or self.payload,
            headers={"Idempotency-Key": key or self.key})

    def test_create_confirms_and_sends_notification(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        booking = response.json
        self.assertEqual(booking["status"], "confirmed")
        self.assertEqual(booking["guest_name"], "Alex Tan")
        self.assertEqual(next(iter(self.server.events.values()))["guest_name"], "Alex Tan")
        self.assertEqual(booking["notification_status"], "sent")
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(set(booking), {"booking_id", "guest_name", "room_id", "check_in", "check_out", "guest_count", "status", "notification_status", "created_at", "updated_at"})
        for _, _, _, headers in self.server.calls:
            self.assertEqual(headers["X-Internal-Key"], "test-key")
            self.assertNotIn("X-Customer-Token", headers)

    def test_identical_retry_returns_same_order(self):
        first = self.create()
        second = self.create()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json["booking_id"], second.json["booking_id"])
        self.assertEqual(len(self.server.reservations), 1)
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(len([c for c in self.server.calls if c[0] == "POST" and c[1] == "/internal/reservations"]), 1)

    def test_changed_input_same_key_is_conflict(self):
        first = self.create()
        changed = self.create(payload=dict(self.payload, room_id="R102"))
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(changed.json["booking_id"], first.json["booking_id"])

    def test_definite_rejection_is_saved_and_replayed(self):
        self.server.reject = True
        first = self.create()
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json["error"]["code"], "ROOM_UNAVAILABLE")
        self.server.reject = False
        self.assertEqual(self.create().json, first.json)
        self.assertEqual(len(self.server.events), 0)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT status FROM bookings").fetchone()[0], "rejected")

    def test_offline_availability_stays_pending_then_recovers(self):
        self.server.availability_down = True
        first = self.create()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json["status"], "pending")
        self.assertEqual(self.create().status_code, 202)
        self.server.availability_down = False
        self.module.booking_workflow.retry_once()
        found = self.client.get("/api/bookings/" + first.json["booking_id"])
        self.assertEqual(found.json["status"], "confirmed")
        self.assertEqual(len(self.server.reservations), 1)

    def test_lost_response_recovers_without_second_reserve(self):
        self.server.lose_reply = True
        first = self.create()
        self.assertEqual(first.status_code, 202)
        self.module.booking_workflow.retry_once()
        repeated = self.create()
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["booking_id"], first.json["booking_id"])
        reserves = [c for c in self.server.calls if c[0] == "POST" and c[1] == "/internal/reservations"]
        self.assertEqual(len(reserves), 1)

    def test_notification_outage_does_not_fail_booking(self):
        self.server.notification_down = True
        first = self.create()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json["status"], "confirmed")
        self.assertEqual(first.json["notification_status"], "pending")
        sent_ids = [c[2]["event_id"] for c in self.server.calls if c[1] == "/internal/notifications"]
        self.server.notification_down = False
        self.module.booking_workflow.retry_once()
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(next(iter(self.server.events)), sent_ids[0])
        self.assertEqual(self.create().json["notification_status"], "sent")

    def test_view_by_id_needs_no_login(self):
        first = self.create()
        url = "/api/bookings/" + first.json["booking_id"]
        self.assertEqual(self.client.get(url).json, first.json)
        self.assertEqual(self.client.get("/api/bookings/missing").status_code, 404)

    def test_invalid_create_never_calls_services(self):
        cases = [dict(self.payload, guest_name=""), dict(self.payload, guest_name="   "),
            dict(self.payload, guest_name=None), dict(self.payload, guest_name="x" * 101),
            dict(self.payload, guest_count=True), dict(self.payload, guest_count="2"),
            dict(self.payload, guest_count=9), dict(self.payload, check_in="2026-02-30"),
            dict(self.payload, check_out="2026-11-10"), dict(self.payload, check_out="2026-12-11"),
            dict(self.payload, customer_id="someone-else"), dict(self.payload, total_price_cents=1)]
        for payload in cases:
            self.assertEqual(self.create(payload=payload).status_code, 400)
        self.assertEqual(self.create(key="not-a-uuid").status_code, 400)
        self.assertEqual(self.client.post("/api/bookings", json=self.payload,
            headers={}).status_code, 400)
        self.assertEqual(self.client.post("/api/bookings", json=self.payload).status_code, 400)
        self.assertEqual(self.client.post("/api/bookings", data="no-json",
            headers={"Idempotency-Key": self.key}).status_code, 415)
        self.assertEqual(self.server.calls, [])

    def test_bad_reservation_reply_cannot_confirm(self):
        self.server.bad_reply = True
        response = self.create()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "pending")
        self.assertEqual(len(self.server.events), 0)

    def test_concurrent_duplicate_requests_create_one_order(self):
        def submit(_):
            with self.module.app.test_client() as client:
                response = self.create(client=client)
                return response.status_code, response.json
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(submit, range(2)))
        self.assertTrue(all(status in (200, 201, 202) for status, _ in replies))
        self.assertEqual(len({body["booking_id"] for _, body in replies}), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)
        self.assertEqual(len(self.server.reservations), 1)

    def test_late_reply_does_not_restore_cancelled_order(self):
        self.server.availability_down = True
        first = self.create()
        self.assertEqual(first.status_code, 202)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE bookings SET status='cancelled'")
        self.server.availability_down = False
        self.module.booking_workflow.retry_once()
        self.assertEqual(self.create().json["status"], "cancelled")
        self.assertEqual(len(self.server.events), 0)

    def test_same_key_is_global(self):
        first = self.create()
        replay = self.create(client=self.module.app.test_client())
        self.assertEqual(replay.json["booking_id"], first.json["booking_id"])
        changed = self.create(payload=dict(self.payload, guest_name="Another Guest"))
        self.assertEqual(changed.status_code, 409)

    def test_saved_pending_event_survives_workflow_restart(self):
        self.server.notification_down = True
        first = self.create()
        new_workflow = type(self.module.booking_workflow)(self.module.app.config, self.module.get_db)
        self.server.notification_down = False
        new_workflow.retry_once()
        new_workflow.retry_once()
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(self.create().json["notification_status"], "sent")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 1)

    def test_confirm_compare_and_set_rejects_stale_reply(self):
        self.server.availability_down = True
        first = self.create()
        booking = first.json
        reservation = dict(booking, status="reserved")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE bookings SET status='cancelled'")
        self.module.booking_workflow.confirm(booking, reservation)
        self.assertEqual(self.module.booking_workflow.get_booking(booking["booking_id"])["status"], "cancelled")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 0)
