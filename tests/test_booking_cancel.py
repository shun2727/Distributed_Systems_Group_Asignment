from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import unittest

import test_booking_create as creation


class BookingCancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        creation.BookingCreateTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        creation.BookingCreateTests.tearDownClass.__func__(cls)

    create = creation.BookingCreateTests.create

    def setUp(self):
        creation.BookingCreateTests.setUp(self)
        self.server.lose_release_reply = self.server.bad_release_reply = False

    def cancel(self, booking_id, client=None):
        return (client or self.client).post(f"/api/bookings/{booking_id}/cancel")

    def test_cancel_releases_then_preserves_order_details(self):
        original = self.create().json
        response = self.cancel(original["booking_id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "cancelled")
        for key in ("booking_id", "guest_name", "room_id", "created_at"):
            self.assertEqual(response.json[key], original[key])
        self.assertEqual(self.server.reservations[original["booking_id"]]["status"], "released")
        release = [c for c in self.server.calls if c[1].endswith("/release")][0]
        self.assertIsNone(release[2])
        self.assertEqual(release[3]["X-Internal-Key"], "test-key")
        self.assertNotIn("X-Customer-Token", release[3])

    def test_repeat_cancel_is_success_without_another_release(self):
        booking_id = self.create().json["booking_id"]
        self.assertEqual(self.cancel(booking_id).status_code, 200)
        self.assertEqual(self.cancel(booking_id).status_code, 200)
        self.assertEqual(len([c for c in self.server.calls if c[1].endswith("/release")]), 1)
        replay = self.create()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["status"], "cancelled")
        self.assertEqual(len(self.server.events), 1)

    def test_cancel_by_id_needs_no_login(self):
        booking_id = self.create().json["booking_id"]
        self.assertEqual(self.cancel("missing").status_code, 404)
        self.assertEqual(self.client.post(f"/api/bookings/{booking_id}/cancel").status_code, 200)

    def test_pending_cannot_be_cancelled(self):
        self.server.availability_down = True
        booking_id = self.create().json["booking_id"]
        response = self.cancel(booking_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"]["code"], "BOOKING_STATE_CONFLICT")
        self.assertEqual(response.json["booking_id"], booking_id)
        self.assertEqual(len([c for c in self.server.calls if c[1].endswith("/release")]), 0)

    def test_rejected_booking_cannot_be_cancelled(self):
        self.server.reject = True
        booking_id = self.create().json["booking_id"]
        response = self.cancel(booking_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"]["code"], "BOOKING_STATE_CONFLICT")
        self.assertEqual(len([c for c in self.server.calls if c[1].endswith("/release")]), 0)

    def test_outage_stays_cancelling_until_recovery(self):
        booking_id = self.create().json["booking_id"]
        self.server.availability_down = True
        first = self.cancel(booking_id)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json["status"], "cancelling")
        self.assertEqual(self.cancel(booking_id).status_code, 202)
        self.server.availability_down = False
        restarted = type(self.module.booking_workflow)(self.module.app.config, self.module.get_db)
        restarted.retry_once()
        self.assertEqual(self.cancel(booking_id).json["status"], "cancelled")

    def test_lost_release_reply_retries_same_id(self):
        booking_id = self.create().json["booking_id"]
        self.server.lose_release_reply = True
        response = self.cancel(booking_id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.server.reservations[booking_id]["status"], "released")
        self.module.booking_workflow.retry_once()
        self.assertEqual(self.cancel(booking_id).json["status"], "cancelled")
        paths = [c[1] for c in self.server.calls if c[1].endswith("/release")]
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(len(self.server.reservations), 1)

    def test_invalid_release_response_cannot_finish_cancel(self):
        booking_id = self.create().json["booking_id"]
        self.server.bad_release_reply = True
        self.assertEqual(self.cancel(booking_id).json["status"], "cancelling")
        self.server.bad_release_reply = False
        self.module.booking_workflow.retry_once()
        self.assertEqual(self.cancel(booking_id).json["status"], "cancelled")

    def test_cancel_has_no_request_body(self):
        booking_id = self.create().json["booking_id"]
        before = len(self.server.calls)
        response = self.client.post(f"/api/bookings/{booking_id}/cancel", json={"status": "cancelled"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.server.calls), before)

    def test_concurrent_cancellations_are_safe(self):
        booking_id = self.create().json["booking_id"]
        def submit(_):
            with self.module.app.test_client() as client:
                return self.cancel(booking_id, client=client).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, range(2)))
        self.assertTrue(all(s in (200, 202) for s in statuses))
        self.module.booking_workflow.retry_once()
        self.assertEqual(self.cancel(booking_id).json["status"], "cancelled")
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)
        self.assertEqual(self.server.reservations[booking_id]["status"], "released")
