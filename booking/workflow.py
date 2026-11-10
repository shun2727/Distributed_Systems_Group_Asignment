"""Booking creation and recovery; Availability owns room inventory."""
from datetime import date, datetime, timezone
import json
import logging
import re
import sqlite3
import threading
import uuid

import requests


FIELDS = ("room_id", "check_in", "check_out", "guest_count")
ERRORS = {
    "ROOM_UNAVAILABLE": (409, "Room unavailable for these dates."),
    "IDEMPOTENCY_CONFLICT": (409, "Reservation request conflicts with an earlier request."),
    "RESERVATION_RELEASED": (409, "Reservation was already released."),
    "VALIDATION_ERROR": (400, "The reservation details were rejected."),
    "NOT_FOUND": (404, "Room not found."),
}


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_reply(code, message, status, booking_id=None):
    body = {"error": {"code": code, "message": message}}
    if booking_id:
        body["booking_id"] = booking_id
    return body, status


def valid_booking(data):
    if not isinstance(data, dict) or set(data) != set(FIELDS) | {"guest_name"}:
        return False
    if not isinstance(data["guest_name"], str) or not 1 <= len(data["guest_name"].strip()) <= 100:
        return False
    if not isinstance(data["room_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data["room_id"]):
        return False
    if type(data["guest_count"]) is not int or not 1 <= data["guest_count"] <= 8:
        return False
    try:
        for key in ("check_in", "check_out"):
            if not isinstance(data[key], str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data[key]):
                return False
        nights = (date.fromisoformat(data["check_out"]) - date.fromisoformat(data["check_in"])).days
        return 1 <= nights <= 30
    except ValueError:
        return False


def init_workflow_tables(db):
    # One global request key, saved together with its booking.
    db.execute("""CREATE TABLE IF NOT EXISTS booking_requests (
        request_key TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        booking_id TEXT NOT NULL UNIQUE REFERENCES bookings(booking_id),
        error_code TEXT,
        error_status INTEGER,
        error_message TEXT
    )""")
    # A saved event survives a notification outage or process restart.
    db.execute("""CREATE TABLE IF NOT EXISTS notification_events (
        event_id TEXT PRIMARY KEY,
        booking_id TEXT NOT NULL UNIQUE REFERENCES bookings(booking_id),
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )""")


class BookingWorkflow:
    def __init__(self, config, get_db):
        self.config = config
        self.get_db = get_db
        self.worker_lock = threading.Lock()
        self.retry_lock = threading.Lock()
        self.worker_started = False
        self.logger = logging.getLogger(__name__)

    def get_booking(self, booking_id):
        db = self.get_db()
        try:
            sql, params = "SELECT * FROM bookings WHERE booking_id = ?", [booking_id]
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def result(self, booking_id, new=False):
        booking = self.get_booking(booking_id)
        if booking["status"] == "rejected":
            db = self.get_db()
            try:
                saved = db.execute("SELECT * FROM booking_requests WHERE booking_id = ?", (booking_id,)).fetchone()
                return error_reply(saved["error_code"], saved["error_message"], saved["error_status"], booking_id)
            finally:
                db.close()
        if booking["status"] in ("pending", "cancelling"):
            return booking, 202
        return booking, 201 if new and booking["status"] == "confirmed" else 200

    def submit(self, data, request_key):
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        db = self.get_db()
        try:
            # Lock only local writes; never hold a database lock during HTTP calls.
            with db:
                db.execute("BEGIN IMMEDIATE")
                saved = db.execute(
                    "SELECT * FROM booking_requests WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if saved:
                    if saved["payload_json"] != payload:
                        return error_reply("IDEMPOTENCY_CONFLICT", "Use a new key for a different booking.", 409, saved["booking_id"])
                    booking_id, new = saved["booking_id"], False
                else:
                    booking_id, new = uuid.uuid4().hex, True
                    now = timestamp()
                    db.execute("""INSERT INTO bookings (
                        booking_id, guest_name, room_id, check_in, check_out, guest_count,
                        status, notification_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 'pending', ?, ?)""",
                        (booking_id, data["guest_name"], data["room_id"], data["check_in"],
                         data["check_out"], data["guest_count"], now, now))
                    db.execute("""INSERT INTO booking_requests
                        (request_key, payload_json, booking_id) VALUES (?, ?, ?)""",
                        (request_key, payload, booking_id))
        finally:
            db.close()
        if new:
            self.reserve(self.get_booking(booking_id))
            self.send_notifications(booking_id)
        return self.result(booking_id, new)

    def cancel(self, booking_id):
        db = self.get_db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM bookings WHERE booking_id=?",
                                 (booking_id,)).fetchone()
                if row is None:
                    return error_reply("NOT_FOUND", "Booking not found.", 404)
                booking = dict(row)
                if booking["status"] in ("pending", "rejected"):
                    return error_reply("BOOKING_STATE_CONFLICT", "Only a confirmed booking can be cancelled.", 409, booking_id)
                if booking["status"] == "cancelled":
                    return booking, 200
                if booking["status"] == "cancelling":
                    return booking, 202
                # Save the intent first so recovery can finish after a restart.
                db.execute("UPDATE bookings SET status='cancelling', updated_at=? WHERE booking_id=? AND status='confirmed'",
                           (timestamp(), booking_id))
        finally:
            db.close()
        self.release(self.get_booking(booking_id))
        booking = self.get_booking(booking_id)
        return booking, 200 if booking["status"] == "cancelled" else 202

    def release(self, booking):
        if booking is None or booking["status"] != "cancelling":
            return
        status, body = self.call("POST", "AVAILABILITY",
            "/internal/reservations/" + booking["booking_id"] + "/release")
        if (status != 200 or not isinstance(body, dict)
                or body.get("booking_id") != booking["booking_id"] or body.get("status") != "released"):
            return
        db = self.get_db()
        try:
            with db:
                # Only finish cancellation after Availability confirms release.
                db.execute("UPDATE bookings SET status='cancelled', updated_at=? WHERE booking_id=? AND status='cancelling'",
                           (timestamp(), booking["booking_id"]))
        finally:
            db.close()

    def call(self, method, service, path, data=None):
        if not self.config["INTERNAL_KEY"]:
            return None, None
        try:
            with requests.request(
                method, self.config[service + "_URL"].rstrip("/") + path,
                json=data, headers={"X-Internal-Key": self.config["INTERNAL_KEY"]},
                timeout=self.config[service + "_TIMEOUT"], allow_redirects=False,
            ) as response:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                return response.status_code, body
        except requests.RequestException:
            return None, None

    def valid_reservation(self, booking, reply):
        return (
            isinstance(reply, dict)
            and reply.get("booking_id") == booking["booking_id"]
            and reply.get("status") == "reserved"
            and all(reply.get(k) == booking[k] for k in FIELDS)
            and type(reply.get("guest_count")) is int
        )

    def confirm(self, booking, reservation):
        db = self.get_db()
        try:
            with db:
                changed = db.execute("""UPDATE bookings SET status = 'confirmed', updated_at = ?
                    WHERE booking_id = ? AND status = 'pending'""",
                    (timestamp(), booking["booking_id"])).rowcount
                if changed:
                    event_id = uuid.uuid4().hex
                    event = {k: booking[k] for k in ("booking_id", "guest_name", "room_id", "check_in", "check_out")}
                    event.update(event_id=event_id, event_type="booking.confirmed")
                    db.execute("INSERT INTO notification_events (event_id, booking_id, payload_json) VALUES (?, ?, ?)",
                               (event_id, booking["booking_id"], json.dumps(event)))
        finally:
            db.close()

    def reject(self, booking_id, code):
        status, message = ERRORS[code]
        db = self.get_db()
        try:
            with db:
                changed = db.execute("UPDATE bookings SET status='rejected', updated_at=? WHERE booking_id=? AND status='pending'",
                                     (timestamp(), booking_id)).rowcount
                if changed:
                    db.execute("UPDATE booking_requests SET error_code=?, error_status=?, error_message=? WHERE booking_id=?",
                               (code, status, message, booking_id))
        finally:
            db.close()

    def reserve(self, booking):
        if booking is None or booking["status"] != "pending":
            return
        payload = {k: booking[k] for k in FIELDS}
        payload["booking_id"] = booking["booking_id"]
        status, body = self.call("POST", "AVAILABILITY", "/internal/reservations", payload)
        if status in (200, 201) and self.valid_reservation(booking, body):
            self.confirm(booking, body)
        elif isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code")
            if code in ERRORS and status == ERRORS[code][0]:
                self.reject(booking["booking_id"], code)

    def recover(self, booking):
        status, body = self.call("GET", "AVAILABILITY", "/internal/reservations/" + booking["booking_id"])
        if status == 200 and self.valid_reservation(booking, body):
            self.confirm(booking, body)
        elif status == 200 and isinstance(body, dict) and body.get("booking_id") == booking["booking_id"] and body.get("status") == "released":
            self.reject(booking["booking_id"], "RESERVATION_RELEASED")
        elif status == 404:
            # The original request may not have arrived; reuse the same booking ID.
            self.reserve(self.get_booking(booking["booking_id"]))

    def send_notifications(self, booking_id=None):
        db = self.get_db()
        try:
            sql = "SELECT * FROM notification_events WHERE status = 'pending'"
            params = []
            if booking_id:
                sql += " AND booking_id = ?"
                params.append(booking_id)
            events = db.execute(sql + " LIMIT 100", params).fetchall()
        finally:
            db.close()
        for event in events:
            status, body = self.call("POST", "NOTIFICATION", "/internal/notifications", json.loads(event["payload_json"]))
            if (status not in (200, 201) or not isinstance(body, dict)
                    or body.get("event_id") != event["event_id"] or body.get("status") != "recorded"):
                continue
            db = self.get_db()
            try:
                with db:
                    db.execute("UPDATE notification_events SET status='sent' WHERE event_id=?", (event["event_id"],))
                    db.execute("UPDATE bookings SET notification_status='sent', updated_at=? WHERE booking_id=?",
                               (timestamp(), event["booking_id"]))
            finally:
                db.close()

    def retry_once(self):
        if not self.retry_lock.acquire(blocking=False):
            return
        try:
            db = self.get_db()
            try:
                pending = db.execute("SELECT * FROM bookings WHERE status IN ('pending', 'cancelling') LIMIT 100").fetchall()
            finally:
                db.close()
            for booking in pending:
                if booking["status"] == "cancelling":
                    self.release(dict(booking))
                else:
                    self.recover(dict(booking))
            self.send_notifications()
        finally:
            self.retry_lock.release()

    def start_worker(self):
        with self.worker_lock:
            if self.worker_started:
                return
            self.worker_started = True
            threading.Thread(target=self.worker_loop, name="booking-retry", daemon=True).start()

    def worker_loop(self):
        timer = threading.Event()
        while not timer.wait(5):
            try:
                self.retry_once()
            except Exception as error:
                # Leave saved work pending, so the next pass can try again.
                self.logger.warning("Booking recovery deferred: %s", type(error).__name__)
