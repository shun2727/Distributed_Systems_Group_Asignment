"""Room inventory and reservation service (http://localhost:5002)."""
import json
import hmac
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("AVAILABILITY_DB", ROOT / "availability.db"))
INTERNAL_KEY = os.environ.get("INTERNAL_KEY")
ROOMS = [
    ("R101", "101", "single", 1, 8000), ("R102", "102", "single", 1, 8000),
    ("R201", "201", "double", 2, 12000), ("R202", "202", "double", 2, 12000),
    ("R301", "301", "king_suite", 4, 20000), ("R302", "302", "king_suite", 4, 20000),
]

def connect():
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=10000")
    return c

def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rooms(room_id TEXT PRIMARY KEY, room_number TEXT NOT NULL, room_type TEXT NOT NULL, capacity INTEGER NOT NULL, price_per_night_cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'MYR');
        CREATE TABLE IF NOT EXISTS reservations(booking_id TEXT PRIMARY KEY, reservation_id TEXT, room_id TEXT, check_in TEXT, check_out TEXT, guest_count INTEGER, status TEXT NOT NULL, total_price_cents INTEGER, currency TEXT NOT NULL DEFAULT 'MYR');
        CREATE TABLE IF NOT EXISTS occupied_nights(room_id TEXT NOT NULL, stay_date TEXT NOT NULL, booking_id TEXT NOT NULL, PRIMARY KEY(room_id,stay_date), FOREIGN KEY(room_id) REFERENCES rooms(room_id));
        """)
        c.executemany("INSERT OR IGNORE INTO rooms(room_id,room_number,room_type,capacity,price_per_night_cents) VALUES(?,?,?,?,?)", ROOMS)

def utc_now(): return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
def error(code, message): return {"error": {"code": code, "message": message}}
def valid_id(value): return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is not None

class Handler(BaseHTTPRequestHandler):
    server_version = "Availability/1.0"
    def log_message(self, fmt, *args):
        # Avoid logging secrets or request bodies.
        super().log_message(fmt, *args)
    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.send_cors(); self.end_headers(); self.wfile.write(body)
    def send_cors(self):
        origin = self.headers.get("Origin")
        if origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
            self.send_header("Access-Control-Allow-Origin", origin); self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Internal-Key")
    def do_OPTIONS(self): self.send_response(204); self.send_cors(); self.end_headers()
    def do_GET(self):
        parsed = urlparse(self.path); path = parsed.path
        if path == "/health":
            try:
                with connect() as c: c.execute("SELECT 1")
                self.send_json(200, {"service":"availability","status":"ok"})
            except sqlite3.Error: self.send_json(503, {"service":"availability","status":"error"})
            return
        if path not in ("/internal/rooms",) and not re.fullmatch(r"/internal/reservations/[A-Za-z0-9_-]{1,64}", path):
            self.send_json(404, error("NOT_FOUND", "Route not found.")); return
        supplied_key = self.headers.get("X-Internal-Key", "")
        if not INTERNAL_KEY or not hmac.compare_digest(supplied_key.encode(), INTERNAL_KEY.encode()):
            self.send_json(401, error("UNAUTHORIZED", "Invalid internal key.")); return
        try:
            if path == "/internal/rooms":
                q = parse_qs(parsed.query)
                ci, co = q.get("check_in", [None])[0], q.get("check_out", [None])[0]
                guests = q.get("guest_count", [None])[0]
                if not valid_dates(ci, co) or not guests or not guests.isdigit() or not 1 <= int(guests) <= 8:
                    self.send_json(400, error("VALIDATION_ERROR", "Valid check_in, check_out and guest_count are required.")); return
                room_type = q.get("room_type", [None])[0]
                with connect() as c:
                    rooms = c.execute("""SELECT r.* FROM rooms r WHERE r.capacity>=? AND (? IS NULL OR r.room_type=?) AND NOT EXISTS (SELECT 1 FROM occupied_nights n WHERE n.room_id=r.room_id AND n.stay_date>=? AND n.stay_date<?) ORDER BY r.room_id""", (int(guests), room_type, room_type, ci, co)).fetchall()
                self.send_json(200, {"rooms":[dict(r) for r in rooms]}); return
            booking_id = path.rsplit("/", 1)[1]
            with connect() as c: row = c.execute("SELECT reservations.*, rooms.room_type FROM reservations LEFT JOIN rooms ON rooms.room_id=reservations.room_id WHERE reservations.booking_id=?", (booking_id,)).fetchone()
            if not row: self.send_json(404, error("NOT_FOUND", "Reservation not found.")); return
            self.send_json(200, reservation_json(row)); return
        except sqlite3.Error:
            self.send_json(503, error("SERVICE_UNAVAILABLE", "Availability storage is temporarily unavailable."))
    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/internal/reservations",) and not re.fullmatch(r"/internal/reservations/[A-Za-z0-9_-]{1,64}/release", path):
            self.send_json(404, error("NOT_FOUND", "Route not found.")); return
        supplied_key = self.headers.get("X-Internal-Key", "")
        if not INTERNAL_KEY or not hmac.compare_digest(supplied_key.encode(), INTERNAL_KEY.encode()):
            self.send_json(401, error("UNAUTHORIZED", "Invalid internal key.")); return
        try:
            if path.endswith("/release"):
                booking_id = path.split("/")[-2]
                with connect() as c:
                    c.execute("BEGIN IMMEDIATE")
                    row = c.execute("SELECT * FROM reservations WHERE booking_id=?", (booking_id,)).fetchone()
                    if row is None:
                        c.execute("INSERT INTO reservations(booking_id,status) VALUES(?, 'released')", (booking_id,))
                    else:
                        c.execute("DELETE FROM occupied_nights WHERE booking_id=?", (booking_id,))
                        c.execute("UPDATE reservations SET status='released' WHERE booking_id=?", (booking_id,))
                    c.commit()
                self.send_json(200, {"booking_id":booking_id,"status":"released"}); return
            body = self.read_json()
            if not isinstance(body, dict) or set(body) != {"booking_id","room_id","check_in","check_out","guest_count"}:
                self.send_json(400, error("VALIDATION_ERROR", "Invalid reservation fields.")); return
            bid, rid, ci, co, guests = (body.get(k) for k in ("booking_id","room_id","check_in","check_out","guest_count"))
            if not valid_id(bid) or not valid_id(rid) or not valid_dates(ci, co) or type(guests) is not int or not 1 <= guests <= 8:
                self.send_json(400, error("VALIDATION_ERROR", "Invalid reservation fields.")); return
            with connect() as c:
                c.execute("BEGIN IMMEDIATE")
                old = c.execute("SELECT * FROM reservations WHERE booking_id=?", (bid,)).fetchone()
                if old:
                    if old["status"] == "released": c.rollback(); self.send_json(409, error("RESERVATION_RELEASED", "This reservation was already released.")); return
                    fields = (old["room_id"], old["check_in"], old["check_out"], old["guest_count"])
                    if fields != (rid, ci, co, guests): c.rollback(); self.send_json(409, error("IDEMPOTENCY_CONFLICT", "Booking ID was used with different reservation details.")); return
                    c.commit(); self.send_json(200, reservation_json(old)); return
                room = c.execute("SELECT * FROM rooms WHERE room_id=?", (rid,)).fetchone()
                if room is None: c.rollback(); self.send_json(404, error("NOT_FOUND", "Room not found.")); return
                if guests > room["capacity"]: c.rollback(); self.send_json(400, error("VALIDATION_ERROR", "Guest count exceeds room capacity.")); return
                nights = (date.fromisoformat(co) - date.fromisoformat(ci)).days
                reservation_id = "RS" + uuid.uuid4().hex[:16]
                total = room["price_per_night_cents"] * nights
                c.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)", (bid,reservation_id,rid,ci,co,guests,"reserved",total,"MYR"))
                try:
                    c.executemany("INSERT INTO occupied_nights(room_id,stay_date,booking_id) VALUES(?,?,?)", ((rid,date.fromordinal(date.fromisoformat(ci).toordinal()+i).isoformat(),bid) for i in range(nights)))
                except sqlite3.IntegrityError:
                    c.rollback(); self.send_json(409, error("ROOM_UNAVAILABLE", "Room unavailable for these dates.")); return
                row = c.execute("SELECT * FROM reservations WHERE booking_id=?", (bid,)).fetchone(); c.commit()
            self.send_json(201, reservation_json(row))
        except (ValueError, json.JSONDecodeError): self.send_json(400, error("VALIDATION_ERROR", "Malformed JSON request."))
        except sqlite3.Error: self.send_json(503, error("SERVICE_UNAVAILABLE", "Availability storage is temporarily unavailable."))
    def read_json(self):
        if "application/json" not in self.headers.get("Content-Type", "").lower(): raise ValueError()
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16384: raise ValueError()
        raw = self.rfile.read(length)
        return json.loads(raw)

def valid_dates(ci, co):
    if not isinstance(ci,str) or not isinstance(co,str): return False
    try:
        a=date.fromisoformat(ci); b=date.fromisoformat(co)
        return a.isoformat()==ci and b.isoformat()==co and a < b and (b-a).days <= 30
    except ValueError: return False

def reservation_json(row):
    result = {"reservation_id":row["reservation_id"],"booking_id":row["booking_id"],"room_id":row["room_id"],"check_in":row["check_in"],"check_out":row["check_out"],"guest_count":row["guest_count"],"status":row["status"],"total_price_cents":row["total_price_cents"],"currency":row["currency"]}
    if "room_type" in row.keys(): result["room_type"] = row["room_type"]
    return result

if __name__ == "__main__":
    if not INTERNAL_KEY:
        raise SystemExit("Set INTERNAL_KEY before starting the Availability service.")
    init_db(); port=int(os.environ.get("PORT", "5002")); print(f"Availability service: http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
