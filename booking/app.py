from flask import Flask, request
from werkzeug.exceptions import HTTPException
from datetime import date
from pathlib import Path
from booking.workflow import BookingWorkflow, init_workflow_tables, valid_booking
import os
import re
import requests
import sqlite3
import uuid

# Create the Flask app.
app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "BOOKING_DATABASE", str(Path(__file__).with_name("booking-v2.db"))
)
app.config["WEBSITE_ORIGIN"] = os.environ.get("WEBSITE_ORIGIN", "http://localhost:3000")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["AVAILABILITY_URL"] = os.environ.get("AVAILABILITY_URL", "http://localhost:5002")
app.config["AVAILABILITY_TIMEOUT"] = 3
app.config["NOTIFICATION_URL"] = os.environ.get("NOTIFICATION_URL", "http://localhost:5003")
app.config["NOTIFICATION_TIMEOUT"] = 3
app.config["RETRY_WORKER_ENABLED"] = os.environ.get("BOOKING_RETRY_WORKER", "1") == "1"

# Return the service status.
@app.get("/health")
def health():
    try:
        db = get_db()
        try:
            db.execute("SELECT booking_id FROM bookings LIMIT 0")
        finally:
            db.close()
    except sqlite3.Error:
        return {"service": "booking", "status": "error"}, 503
    return {"service": "booking", "status": "ok"}

# Open a database connection.
def get_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.execute("PRAGMA foreign_keys = ON")
    # Access results by column name.
    db.row_factory = sqlite3.Row
    return db

# Create the simplified schema in its own database; keep the old file intact.
def init_db():
    db = get_db()
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(bookings)")}
        if columns and "customer_id" in columns:
            raise RuntimeError("This database uses the old account schema. Use a new BOOKING_DATABASE file.")
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS bookings (
                booking_id TEXT PRIMARY KEY,
                guest_name TEXT NOT NULL,
                room_id TEXT NOT NULL,
                check_in TEXT NOT NULL,
                check_out TEXT NOT NULL,
                guest_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                notification_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
            init_workflow_tables(db)
    finally:
        db.close()


def api_error(code, message, status):
    return {"error": {"code": code, "message": message}}, status


# Check the dates and room type before asking Availability.
def read_room_search():
    fields = {"check_in", "check_out", "room_type"}
    if set(request.args) != fields or any(len(request.args.getlist(k)) != 1 for k in fields):
        return None
    values = request.args.to_dict()
    if request.content_length or any(
        not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", values[k])
        for k in ("check_in", "check_out")
    ):
        return None
    try:
        check_in = date.fromisoformat(values["check_in"])
        check_out = date.fromisoformat(values["check_out"])
    except ValueError:
        return None
    if check_out <= check_in:
        return None
    if values["room_type"] not in {"single", "double", "king_suite"}:
        return None
    return values


# Reject incomplete room data instead of reporting a false search result.
def valid_room(room, room_type):
    return (
        isinstance(room, dict)
        and isinstance(room.get("room_id"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", room["room_id"]) is not None
        and isinstance(room.get("room_number"), str)
        and room.get("room_type") == room_type
        and type(room.get("capacity")) is int
        and room["capacity"] > 0
    )


# Ask Availability for rooms; searching never reserves a room.
@app.get("/api/rooms")
def search_rooms():
    params = read_room_search()
    if params is None:
        return api_error("VALIDATION_ERROR", "Send valid dates with check_out after check_in, and room_type: single, double or king_suite.", 400)
    try:
        with requests.get(
            app.config["AVAILABILITY_URL"].rstrip("/") + "/internal/rooms",
            params=params,
            timeout=app.config["AVAILABILITY_TIMEOUT"],
            allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                return api_error("SERVICE_UNAVAILABLE", "Room search is temporarily unavailable.", 503)
            data = response.json()
    except (requests.RequestException, ValueError):
        return api_error("SERVICE_UNAVAILABLE", "Room search is temporarily unavailable.", 503)
    if (not isinstance(data, dict) or not isinstance(data.get("rooms"), list)
            or not all(valid_room(room, params["room_type"]) for room in data["rooms"])):
        return api_error("SERVICE_UNAVAILABLE", "Availability returned invalid room data.", 503)
    fields = ("room_id", "room_number", "room_type", "capacity")
    return {"rooms": [{field: room[field] for field in fields} for room in data["rooms"]]}


# Save a pending order before asking Availability to reserve it.
@app.post("/api/bookings")
def create_booking():
    if not request.is_json:
        return api_error("UNSUPPORTED_MEDIA_TYPE", "Use application/json.", 415)
    data = request.get_json(silent=True)
    if not valid_booking(data):
        return api_error("VALIDATION_ERROR", "Send a non-empty guest_name, room_id, valid dates with check_out after check_in, and a positive integer guest_count.", 400)
    try:
        request_key = str(uuid.UUID(request.headers.get("Idempotency-Key", "")))
    except ValueError:
        return api_error("VALIDATION_ERROR", "Send a UUID in Idempotency-Key.", 400)
    data["guest_name"] = data["guest_name"].strip()
    return booking_workflow.submit(data, request_key)


# Anyone with the booking ID can view it in this demo.
@app.get("/api/bookings/<booking_id>")
def view_booking(booking_id):
    booking = booking_workflow.get_booking(booking_id)
    if booking is None:
        return api_error("NOT_FOUND", "Booking not found.", 404)
    return booking


# Keep the order record; release the room before marking it cancelled.
@app.post("/api/bookings/<booking_id>/cancel")
def cancel_booking(booking_id):
    if request.content_length:
        return api_error("VALIDATION_ERROR", "Cancellation has no request body.", 400)
    return booking_workflow.cancel(booking_id)


@app.before_request
def start_booking_recovery():
    if not app.config["TESTING"] and app.config["RETRY_WORKER_ENABLED"]:
        booking_workflow.start_worker()


@app.cli.command("retry-pending")
def retry_pending():
    """Run one recovery pass for saved pending work."""
    booking_workflow.retry_once()


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


# Allow the team's website to call the API from a browser.
@app.after_request
def add_cors_headers(response):
    if request.headers.get("Origin") == app.config["WEBSITE_ORIGIN"]:
        response.headers["Access-Control-Allow-Origin"] = app.config["WEBSITE_ORIGIN"]
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Idempotency-Key"
        )
        response.vary.add("Origin")
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(HTTPException)
def handle_http_error(error):
    codes = {400: "VALIDATION_ERROR", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED",
             413: "VALIDATION_ERROR", 415: "UNSUPPORTED_MEDIA_TYPE"}
    response = error.get_response()
    response.set_data(app.json.dumps({"error": {
        "code": codes.get(error.code, "INTERNAL_ERROR"), "message": error.description
    }}))
    response.content_type = "application/json"
    return response


@app.errorhandler(sqlite3.Error)
def handle_database_error(error):
    app.logger.error("Booking storage unavailable: %s", type(error).__name__)
    return api_error("SERVICE_UNAVAILABLE", "Booking storage is unavailable.", 503)


init_db()
booking_workflow = BookingWorkflow(app.config, get_db)
