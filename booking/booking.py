"""Booking API (http://localhost:5001), coordinating inventory and notices."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get("BOOKING_DB",ROOT/"booking.db"))
AVAILABILITY_URL=os.environ.get("AVAILABILITY_URL","http://localhost:5002").rstrip("/")
NOTIFICATION_URL=os.environ.get("NOTIFICATION_URL","http://localhost:5003").rstrip("/")
INTERNAL_KEY=os.environ.get("INTERNAL_KEY")
CLIENT_ORIGINS=tuple(origin.strip() for origin in os.environ.get("CLIENT_ORIGINS","http://localhost:3000,http://127.0.0.1:3000").split(",") if origin.strip())
TOKEN_TTL=timedelta(hours=2)
BODY_LIMIT=16384

def connect():
    c=sqlite3.connect(DB,timeout=10,isolation_level=None); c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=10000"); return c
def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS customers(customer_id TEXT PRIMARY KEY, phone_number TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, customer_id TEXT NOT NULL, expires_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES customers(customer_id));
        CREATE TABLE IF NOT EXISTS bookings(booking_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, room_id TEXT NOT NULL, check_in TEXT NOT NULL, check_out TEXT NOT NULL, guest_count INTEGER NOT NULL, status TEXT NOT NULL, reservation_id TEXT, total_price_cents INTEGER, currency TEXT NOT NULL DEFAULT 'MYR', notification_status TEXT, error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(customer_id,request_key), FOREIGN KEY(customer_id) REFERENCES customers(customer_id));
        CREATE INDEX IF NOT EXISTS bookings_customer_created ON bookings(customer_id,created_at DESC);
        """)

def now(): return datetime.now(timezone.utc)
def stamp(d=None): return (d or now()).isoformat(timespec="seconds").replace("+00:00","Z")
def error(code,message): return {"error":{"code":code,"message":message}}
def valid_id(s): return isinstance(s,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}",s) is not None
def valid_dates(ci,co):
    if not isinstance(ci,str) or not isinstance(co,str): return False
    try:
        a=date.fromisoformat(ci); b=date.fromisoformat(co)
        return a.isoformat()==ci and b.isoformat()==co and a<b and (b-a).days<=30
    except ValueError: return False
def normalize_phone(value):
    if not isinstance(value,str): return None
    s=value.strip()
    if not re.fullmatch(r"\+?[0-9 ()-]+",s): return None
    digits=re.sub(r"\D","",s)
    if not 8<=len(digits)<=15: return None
    return "+"+digits
def token_digest(token): return hashlib.sha256(token.encode()).hexdigest()

def service_request(base,path,method="GET",body=None,timeout=3):
    data=None if body is None else json.dumps(body,separators=(",",":")).encode()
    headers={"X-Internal-Key":INTERNAL_KEY}
    if body is not None: headers["Content-Type"]="application/json"
    req=urllib.request.Request(base+path,data=data,headers=headers,method=method)
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            return response.status,json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: payload=json.loads(e.read().decode())
        except Exception: payload={}
        return e.code,payload
    except (urllib.error.URLError,TimeoutError,OSError,json.JSONDecodeError): return None,None

def as_booking(row):
    return {"booking_id":row["booking_id"],"reservation_id":row["reservation_id"],"customer_id":row["customer_id"],"room_id":row["room_id"],"check_in":row["check_in"],"check_out":row["check_out"],"guest_count":row["guest_count"],"status":row["status"],"total_price_cents":row["total_price_cents"],"currency":row["currency"],"notification_status":row["notification_status"],"created_at":row["created_at"],"updated_at":row["updated_at"]}
def load_booking(booking_id):
    with connect() as c: return c.execute("SELECT * FROM bookings WHERE booking_id=?",(booking_id,)).fetchone()
def update_booking(booking_id,**changes):
    if not changes: return
    changes["updated_at"]=stamp()
    sets=",".join(f"{k}=?" for k in changes)
    with connect() as c: c.execute(f"UPDATE bookings SET {sets} WHERE booking_id=?",(*changes.values(),booking_id))

def deliver_notification(row):
    if not row or row["status"]!="confirmed" or row["notification_status"]!="pending": return
    event_id="E"+row["booking_id"][1:]
    event={"event_id":event_id,"event_type":"booking.confirmed","booking_id":row["booking_id"],"customer_id":row["customer_id"],"room_id":row["room_id"],"check_in":row["check_in"],"check_out":row["check_out"]}
    status,data=service_request(NOTIFICATION_URL,"/internal/notifications","POST",event)
    if status in (200,201) and data and data.get("status")=="recorded": update_booking(row["booking_id"],notification_status="sent")

def recover_booking(row):
    """Resolve a previously pending reserve using its stable booking ID."""
    if not row: return row
    if row["status"]=="cancelling":
        code,_=service_request(AVAILABILITY_URL,"/internal/reservations/"+urllib.parse.quote(row["booking_id"])+"/release","POST")
        if code==200: update_booking(row["booking_id"],status="cancelled")
        return load_booking(row["booking_id"])
    if row["status"]!="pending": return row
    status,res=service_request(AVAILABILITY_URL,"/internal/reservations/"+urllib.parse.quote(row["booking_id"]))
    if status==200 and res:
        if res.get("status")=="reserved":
            update_booking(row["booking_id"],status="confirmed",reservation_id=res.get("reservation_id"),total_price_cents=res.get("total_price_cents"),currency=res.get("currency","MYR"),notification_status="pending")
        elif res.get("status")=="released": update_booking(row["booking_id"],status="rejected",error_json=json.dumps(error("RESERVATION_RELEASED","This reservation was already released.")))
    elif status==404:
        body=json.loads(row["request_json"])
        code,result=service_request(AVAILABILITY_URL,"/internal/reservations","POST",dict(body,booking_id=row["booking_id"]))
        if code in (200,201) and result:
            update_booking(row["booking_id"],status="confirmed",reservation_id=result.get("reservation_id"),total_price_cents=result.get("total_price_cents"),currency=result.get("currency","MYR"),notification_status="pending")
        elif code==409:
            update_booking(row["booking_id"],status="rejected",error_json=json.dumps(result))
    current=load_booking(row["booking_id"])
    deliver_notification(current)
    return load_booking(row["booking_id"])

class Handler(BaseHTTPRequestHandler):
    server_version="Booking/1.0"
    def send_json(self,status,payload):
        body=json.dumps(payload,separators=(",",":")).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.cors(); self.end_headers(); self.wfile.write(body)
    def cors(self):
        origin=self.headers.get("Origin")
        if origin in CLIENT_ORIGINS: self.send_header("Access-Control-Allow-Origin",origin); self.send_header("Vary","Origin")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS"); self.send_header("Access-Control-Allow-Headers","Content-Type, X-Customer-Token, Idempotency-Key")
    def do_OPTIONS(self): self.send_response(204); self.cors(); self.end_headers()
    def read_json(self):
        ctype=self.headers.get("Content-Type","").lower()
        if "application/json" not in ctype: return None,"415"
        try: n=int(self.headers.get("Content-Length","0"))
        except ValueError: return None,"400"
        if n>BODY_LIMIT: return None,"413"
        try: return json.loads(self.rfile.read(n)),None
        except (json.JSONDecodeError,UnicodeDecodeError): return None,"400"
    def customer(self):
        token=self.headers.get("X-Customer-Token","")
        if not token: return None
        with connect() as c: row=c.execute("SELECT customer_id,expires_at FROM sessions WHERE token_hash=?",(token_digest(token),)).fetchone()
        if not row or row["expires_at"]<=stamp(): return None
        return row["customer_id"]
    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path); path=parsed.path
        if path=="/health":
            try:
                with connect() as c: c.execute("SELECT 1")
                self.send_json(200,{"service":"booking","status":"ok"})
            except sqlite3.Error: self.send_json(503,{"service":"booking","status":"error"})
            return
        customer_id=self.customer()
        if not customer_id: self.send_json(401,error("UNAUTHORIZED","Missing, invalid or expired customer token.")); return
        try:
            if path=="/api/rooms":
                q=urllib.parse.parse_qs(parsed.query); ci=q.get("check_in",[None])[0]; co=q.get("check_out",[None])[0]; guests=q.get("guest_count",[None])[0]; room_type=q.get("room_type",[None])[0]
                if not valid_dates(ci,co) or not guests or not guests.isdigit() or not 1<=int(guests)<=8:
                    self.send_json(400,error("VALIDATION_ERROR","Valid check_in, check_out and guest_count are required.")); return
                query=urllib.parse.urlencode({"check_in":ci,"check_out":co,"guest_count":guests,**({"room_type":room_type} if room_type else {})})
                code,data=service_request(AVAILABILITY_URL,"/internal/rooms?"+query)
                if code==200: self.send_json(200,data)
                elif code==None: self.send_json(503,error("SERVICE_UNAVAILABLE","Availability service is unavailable."))
                else: self.send_json(code,data or error("SERVICE_UNAVAILABLE","Availability service failed."))
                return
            if path=="/api/bookings":
                with connect() as c: rows=c.execute("SELECT * FROM bookings WHERE customer_id=? ORDER BY created_at DESC,booking_id DESC",(customer_id,)).fetchall()
                self.send_json(200,{"bookings":[as_booking(r) for r in rows]}); return
            match=re.fullmatch(r"/api/bookings/([A-Za-z0-9_-]{1,64})",path)
            if match:
                with connect() as c: row=c.execute("SELECT * FROM bookings WHERE booking_id=? AND customer_id=?",(match.group(1),customer_id)).fetchone()
                if not row: self.send_json(404,error("NOT_FOUND","Booking not found.")); return
                row=recover_booking(row); result=as_booking(row)
                status,details=service_request(AVAILABILITY_URL,"/internal/reservations/"+urllib.parse.quote(row["booking_id"]))
                if status==200 and details and details.get("room_type"): result["room_type"]=details["room_type"]
                self.send_json(200,result); return
            self.send_json(404,error("NOT_FOUND","Route not found."))
        except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Booking storage is temporarily unavailable."))
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path=="/api/login":
            body,problem=self.read_json()
            if problem: self.body_error(problem); return
            if not isinstance(body,dict) or set(body)!={"phone_number"}:
                self.send_json(400,error("VALIDATION_ERROR","Send exactly one phone_number field.")); return
            phone=normalize_phone(body["phone_number"])
            if not phone: self.send_json(400,error("VALIDATION_ERROR","Enter a phone number with 8 to 15 digits.")); return
            try:
                with connect() as c:
                    c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT customer_id FROM customers WHERE phone_number=?",(phone,)).fetchone()
                    if row: customer_id=row["customer_id"]
                    else:
                        customer_id="C"+uuid.uuid4().hex[:16]; c.execute("INSERT INTO customers VALUES(?,?,?)",(customer_id,phone,stamp()))
                    token=secrets.token_urlsafe(32); expires=stamp(now()+TOKEN_TTL); c.execute("INSERT INTO sessions VALUES(?,?,?)",(token_digest(token),customer_id,expires)); c.commit()
                self.send_json(200,{"token":token,"customer_id":customer_id,"expires_at":expires})
            except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Booking storage is temporarily unavailable."))
            return
        customer_id=self.customer()
        if not customer_id: self.send_json(401,error("UNAUTHORIZED","Missing, invalid or expired customer token.")); return
        if path=="/api/logout":
            token=self.headers.get("X-Customer-Token","")
            try:
                with connect() as c: c.execute("DELETE FROM sessions WHERE token_hash=?",(token_digest(token),))
                self.send_json(200,{"status":"signed_out"})
            except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Could not end the session."))
            return
        if path=="/api/bookings": self.create_booking(customer_id); return
        match=re.fullmatch(r"/api/bookings/([A-Za-z0-9_-]{1,64})/cancel",path)
        if match: self.cancel_booking(customer_id,match.group(1)); return
        self.send_json(404,error("NOT_FOUND","Route not found."))
    def body_error(self,problem):
        if problem=="415": self.send_json(415,error("UNSUPPORTED_MEDIA_TYPE","Content-Type must be application/json."))
        elif problem=="413": self.send_json(413,error("VALIDATION_ERROR","Request body exceeds 16 KiB."))
        else: self.send_json(400,error("VALIDATION_ERROR","Malformed JSON request."))
    def create_booking(self,customer_id):
        body,problem=self.read_json()
        if problem: self.body_error(problem); return
        if not isinstance(body,dict) or set(body)!={"room_id","check_in","check_out","guest_count"}:
            self.send_json(400,error("VALIDATION_ERROR","Invalid booking fields.")); return
        rid,ci,co,guests=(body.get(k) for k in ("room_id","check_in","check_out","guest_count"))
        if not valid_id(rid) or not valid_dates(ci,co) or type(guests) is not int or not 1<=guests<=8:
            self.send_json(400,error("VALIDATION_ERROR","Invalid booking fields.")); return
        key=self.headers.get("Idempotency-Key","")
        if not valid_id(key): self.send_json(400,error("VALIDATION_ERROR","A valid Idempotency-Key header is required.")); return
        encoded=json.dumps(body,sort_keys=True,separators=(",",":"))
        try:
            with connect() as c:
                existing=c.execute("SELECT * FROM bookings WHERE customer_id=? AND request_key=?",(customer_id,key)).fetchone()
                if existing:
                    if existing["request_json"]!=encoded: self.send_json(409,error("IDEMPOTENCY_CONFLICT","Request key was used with different booking details.")); return
                    row=recover_booking(existing)
                    if row["status"]=="rejected": self.send_json(409,json.loads(row["error_json"] or json.dumps(error("ROOM_UNAVAILABLE","Room unavailable for these dates.")))); return
                    self.send_json(200 if row["status"] in ("confirmed","cancelled") else 202,as_booking(row)); return
                booking_id="B"+uuid.uuid4().hex[:16]; current=stamp()
                c.execute("INSERT INTO bookings(booking_id,customer_id,request_key,request_json,room_id,check_in,check_out,guest_count,status,currency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(booking_id,customer_id,key,encoded,rid,ci,co,guests,"pending","MYR",current,current))
            code,res=service_request(AVAILABILITY_URL,"/internal/reservations","POST",dict(body,booking_id=booking_id))
            if code in (200,201) and res:
                update_booking(booking_id,status="confirmed",reservation_id=res.get("reservation_id"),total_price_cents=res.get("total_price_cents"),currency=res.get("currency","MYR"),notification_status="pending")
                row=load_booking(booking_id); deliver_notification(row); row=load_booking(booking_id)
                self.send_json(201,as_booking(row)); return
            if code==409 or code==404:
                payload=res or error("SERVICE_UNAVAILABLE","Could not reserve the room.")
                update_booking(booking_id,status="rejected",error_json=json.dumps(payload))
                self.send_json(code,payload); return
            row=load_booking(booking_id); self.send_json(202,as_booking(row))
        except sqlite3.IntegrityError: self.send_json(409,error("IDEMPOTENCY_CONFLICT","Request key was already used."))
        except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Booking storage is temporarily unavailable."))
    def cancel_booking(self,customer_id,booking_id):
        try:
            with connect() as c: row=c.execute("SELECT * FROM bookings WHERE booking_id=? AND customer_id=?",(booking_id,customer_id)).fetchone()
            if not row: self.send_json(404,error("NOT_FOUND","Booking not found.")); return
            if row["status"] in ("cancelled","rejected"): self.send_json(200,as_booking(row)); return
            if row["status"]=="pending": self.send_json(409,error("BOOKING_STATE_CONFLICT","A pending booking cannot be cancelled yet.")); return
            if row["status"]=="confirmed": update_booking(booking_id,status="cancelling")
            code,data=service_request(AVAILABILITY_URL,"/internal/reservations/"+urllib.parse.quote(booking_id)+"/release","POST")
            if code==200: update_booking(booking_id,status="cancelled")
            row=load_booking(booking_id); self.send_json(200 if row["status"]=="cancelled" else 202,as_booking(row))
        except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Booking storage is temporarily unavailable."))
    def log_message(self,fmt,*args): super().log_message(fmt,*args)

def notification_retry_loop():
    while True:
        time.sleep(5)
        try:
            with connect() as c: rows=c.execute("SELECT * FROM bookings WHERE status='confirmed' AND notification_status='pending' OR status IN ('pending','cancelling')").fetchall()
            for row in rows:
                if row["status"] in ("pending","cancelling"): recover_booking(row)
                else: deliver_notification(row)
        except sqlite3.Error: pass

if __name__=="__main__":
    if not INTERNAL_KEY: raise SystemExit("Set INTERNAL_KEY before starting the Booking service.")
    init_db(); threading.Thread(target=notification_retry_loop,daemon=True).start(); port=int(os.environ.get("PORT","5001")); print(f"Booking service: http://localhost:{port}"); ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
