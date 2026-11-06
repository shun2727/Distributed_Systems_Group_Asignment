from flask import Flask, request
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import os
import re
import secrets
import sqlite3
import uuid

# Create the Flask app.
app = Flask(__name__)
app.config["DATABASE"] = os.environ.get(
    "BOOKING_DATABASE", str(Path(__file__).with_name("booking.db"))
)
app.config["WEBSITE_ORIGIN"] = os.environ.get("WEBSITE_ORIGIN", "http://localhost:3000")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

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

# Create the bookings table if needed.
def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            booking_id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT NOT NULL,
            guest_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            reservation_id TEXT,
            total_price_cents INTEGER,
            currency TEXT NOT NULL,
            notification_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Store accounts and login sessions separately from bookings.
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            customer_id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL REFERENCES users(customer_id),
            expires_at TEXT NOT NULL
        )
    """)
    db.commit()
    db.close()

# Read one customer's bookings, newest first.
def get_bookings(customer_id):
    db = get_db()
    try:
        rows = db.execute(
            """SELECT * FROM bookings
               WHERE customer_id = ?
               ORDER BY created_at DESC, booking_id DESC""",
            (customer_id,)
        ).fetchall()
        # Convert rows to dictionaries for JSON responses.
        return [dict(row) for row in rows]
    finally:
        db.close()

def utc_now():
    return datetime.now(timezone.utc)


def utc_text(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def api_error(code, message, status):
    return {"error": {"code": code, "message": message}}, status


# Check the JSON fields before using account details.
def read_credentials():
    if not request.is_json:
        return None, api_error("UNSUPPORTED_MEDIA_TYPE", "Use application/json.", 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {"username", "password"}:
        return None, api_error("VALIDATION_ERROR", "Send only username and password.", 400)
    username, password = data["username"], data["password"]
    if not isinstance(username, str) or not isinstance(password, str):
        return None, api_error("VALIDATION_ERROR", "Username and password must be text.", 400)
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{3,32}", username) or not 8 <= len(password) <= 128:
        return None, api_error(
            "VALIDATION_ERROR",
            "Username: 3-32 letters, digits, underscores or hyphens. Password: 8-128 characters.",
            400,
        )
    return (username, password), None


# Create an account; never save the original password.
@app.post("/api/register")
def register():
    credentials, error = read_credentials()
    if error:
        return error
    username, password = credentials
    customer_id = uuid.uuid4().hex
    password_hash = generate_password_hash(password)
    db = get_db()
    try:
        with db:
            db.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?)",
                (customer_id, username, password_hash, utc_text(utc_now())),
            )
    except sqlite3.IntegrityError:
        return api_error("USERNAME_TAKEN", "Username is already in use.", 409)
    finally:
        db.close()
    return {"customer_id": customer_id, "username": username}, 201


# Issue a random token that expires after two hours.
@app.post("/api/login")
def login():
    credentials, error = read_credentials()
    if error:
        return error
    username, password = credentials
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            return api_error("UNAUTHORIZED", "Invalid username or password.", 401)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = utc_text(utc_now() + timedelta(hours=2))
        with db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_text(utc_now()),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (token_hash, user["customer_id"], expires_at))
        return {"token": token, "customer_id": user["customer_id"], "expires_at": expires_at}
    finally:
        db.close()


# Find the customer using a valid, unexpired login token.
def get_customer_id():
    token = request.headers.get("X-Customer-Token")
    if not token or len(token) > 256:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    try:
        session = db.execute(
            "SELECT customer_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, utc_text(utc_now())),
        ).fetchone()
        return session["customer_id"] if session else None
    finally:
        db.close()


# Return only the signed-in customer's bookings.
@app.get("/api/bookings")
def list_bookings():
    customer_id = get_customer_id()
    if customer_id is None:
        return api_error("UNAUTHORIZED", "Invalid or missing customer token.", 401)
    return {"bookings": get_bookings(customer_id)}


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
            "Content-Type, X-Customer-Token, Idempotency-Key"
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
