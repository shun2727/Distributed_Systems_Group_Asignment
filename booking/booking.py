"""Booking API (http://localhost:5001), coordinating inventory and notices."""
import hashlib
import hmac
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
ADMIN_NUMBER=os.environ.get("ADMIN_NUMBER","AIRN7K29ADMIN")
ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD","admin123")
BODY_LIMIT=16384

def connect():
    c=sqlite3.connect(DB,timeout=10,isolation_level=None); c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=10000"); return c
def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS customers(customer_id TEXT PRIMARY KEY, phone_number TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, customer_id TEXT NOT NULL, expires_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES customers(customer_id));
        CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications(notification_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, booking_id TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(customer_id) REFERENCES customers(customer_id));
        CREATE TABLE IF NOT EXISTS bookings(booking_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, room_id TEXT NOT NULL, check_in TEXT NOT NULL, check_out TEXT NOT NULL, guest_count INTEGER NOT NULL, status TEXT NOT NULL, reservation_id TEXT, total_price_cents INTEGER, currency TEXT NOT NULL DEFAULT 'MYR', notification_status TEXT, error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(customer_id,request_key), FOREIGN KEY(customer_id) REFERENCES customers(customer_id));
        CREATE INDEX IF NOT EXISTS bookings_customer_created ON bookings(customer_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS booking_names(booking_id TEXT PRIMARY KEY, guest_name TEXT NOT NULL);
        """)
        columns={row[1] for row in c.execute("PRAGMA table_info(notifications)").fetchall()}
        if "is_read" not in columns: c.execute("ALTER TABLE notifications ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")

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

def notify(customer_id, booking_id, message):
    with connect() as c: c.execute("INSERT INTO notifications(notification_id,customer_id,booking_id,message,created_at) VALUES(?,?,?,?,?)",("N"+uuid.uuid4().hex[:16],customer_id,booking_id,message,stamp()))

def admin_session(handler):
    token=handler.headers.get("X-Admin-Token","")
    if not token: return False
    with connect() as c: row=c.execute("SELECT expires_at FROM admin_sessions WHERE token_hash=?",(token_digest(token),)).fetchone()
    return bool(row and row["expires_at"]>stamp())

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
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS"); self.send_header("Access-Control-Allow-Headers","Content-Type, X-Customer-Token, X-Admin-Token, Idempotency-Key")
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
        if path.startswith("/admin/"):
            if not admin_session(self): self.send_json(401,error("UNAUTHORIZED","Admin login required.")); return
            self.admin_get(path,parsed); return
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
            if path=="/api/notifications":
                with connect() as c:
                    rows=c.execute("SELECT notification_id,booking_id,message,created_at,is_read FROM notifications WHERE customer_id=? ORDER BY created_at DESC",(customer_id,)).fetchall()
                    unread=c.execute("SELECT COUNT(*) FROM notifications WHERE customer_id=? AND is_read=0",(customer_id,)).fetchone()[0]
                self.send_json(200,{"notifications":[dict(r) for r in rows],"unread_count":unread}); return
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
        if path=="/api/admin/login":
            body,problem=self.read_json()
            if problem: self.body_error(problem); return
            if not isinstance(body,dict) or body.get("number")!=ADMIN_NUMBER or not hmac.compare_digest(str(body.get("password","")),ADMIN_PASSWORD): self.send_json(401,error("UNAUTHORIZED","Invalid admin number or password.")); return
            token=secrets.token_urlsafe(32); expires=stamp(now()+TOKEN_TTL)
            with connect() as c: c.execute("INSERT INTO admin_sessions VALUES(?,?)",(token_digest(token),expires))
            self.send_json(200,{"token":token,"expires_at":expires}); return
        if path=="/api/admin/identify":
            body,problem=self.read_json()
            if problem: self.body_error(problem); return
            if not isinstance(body,dict) or body.get("number")!=ADMIN_NUMBER: self.send_json(404,error("NOT_FOUND","Account not found.")); return
            self.send_json(200,{"admin":True}); return
        customer_id=self.customer()
        if path=="/api/admin/logout":
            token=self.headers.get("X-Admin-Token","")
            with connect() as c: c.execute("DELETE FROM admin_sessions WHERE token_hash=?",(token_digest(token),))
            self.send_json(200,{"status":"signed_out"}); return
        if path.startswith("/admin/"):
            if not admin_session(self): self.send_json(401,error("UNAUTHORIZED","Admin login required.")); return
            self.admin_post(path); return
        if not customer_id: self.send_json(401,error("UNAUTHORIZED","Missing, invalid or expired customer token.")); return
        if path=="/api/logout":
            token=self.headers.get("X-Customer-Token","")
            try:
                with connect() as c: c.execute("DELETE FROM sessions WHERE token_hash=?",(token_digest(token),))
                self.send_json(200,{"status":"signed_out"})
            except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Could not end the session."))
            return
        if path=="/api/bookings": self.create_booking(customer_id); return
        if path=="/api/notifications/read":
            with connect() as c: c.execute("UPDATE notifications SET is_read=1 WHERE customer_id=?",(customer_id,))
            self.send_json(200,{"status":"ok"}); return
        if path=="/api/notifications/clear":
            with connect() as c: c.execute("DELETE FROM notifications WHERE customer_id=?",(customer_id,))
            self.send_json(200,{"status":"cleared"}); return
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
        if not isinstance(body,dict) or set(body)!={"room_id","check_in","check_out","guest_count","guest_name"}:
            self.send_json(400,error("VALIDATION_ERROR","Invalid booking fields.")); return
        rid,ci,co,guests=(body.get(k) for k in ("room_id","check_in","check_out","guest_count"))
        guest_name=body.get("guest_name")
        if not valid_id(rid) or not valid_dates(ci,co) or type(guests) is not int or not 1<=guests<=8 or not isinstance(guest_name,str) or not guest_name.strip() or len(guest_name)>100:
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
                c.execute("INSERT INTO booking_names VALUES(?,?)",(booking_id,guest_name.strip()))
            code,res=service_request(AVAILABILITY_URL,"/internal/reservations","POST",{"booking_id":booking_id,"room_id":rid,"check_in":ci,"check_out":co,"guest_count":guests})
            if code in (200,201) and res:
                update_booking(booking_id,status="confirmed",reservation_id=res.get("reservation_id"),total_price_cents=res.get("total_price_cents"),currency=res.get("currency","MYR"),notification_status="pending")
                row=load_booking(booking_id); deliver_notification(row); row=load_booking(booking_id)
                notify(customer_id,booking_id,"Booking confirmed")
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
            if code==200: notify(customer_id,booking_id,"Booking cancelled")
            row=load_booking(booking_id); self.send_json(200 if row["status"]=="cancelled" else 202,as_booking(row))
        except sqlite3.Error: self.send_json(503,error("SERVICE_UNAVAILABLE","Booking storage is temporarily unavailable."))

    def admin_get(self,path,parsed):
        if path=="/admin/stats":
            day=urllib.parse.parse_qs(parsed.query).get("date",[date.today().isoformat()])[0]
            try:
                if date.fromisoformat(day).isoformat()!=day: raise ValueError()
            except (ValueError,TypeError): self.send_json(400,error("VALIDATION_ERROR","Invalid dashboard date.")); return
            with connect() as c:
                arrivals=c.execute("SELECT COUNT(*) FROM bookings WHERE check_in=? AND status IN ('confirmed','checked_in')",(day,)).fetchone()[0]
                departures=c.execute("SELECT COUNT(*) FROM bookings WHERE check_out=? AND status IN ('confirmed','checked_in')",(day,)).fetchone()[0]
                in_house=c.execute("SELECT COUNT(*) FROM bookings WHERE check_in<=? AND check_out>? AND status IN ('confirmed','checked_in')",(day,day)).fetchone()[0]
                cancelled=c.execute("SELECT COUNT(*) FROM bookings WHERE status='cancelled' AND substr(updated_at,1,10)=?",(day,)).fetchone()[0]
                revenue=c.execute("SELECT COALESCE(SUM(total_price_cents),0) FROM bookings WHERE check_in=? AND status IN ('confirmed','checked_in','checked_out')",(day,)).fetchone()[0]
            self.send_json(200,{"date":day,"arrivals":arrivals,"departures":departures,"in_house":in_house,"cancelled":cancelled,"arrival_revenue_cents":revenue}); return
        if path=="/admin/rooms":
            q=urllib.parse.parse_qs(parsed.query); ci=q.get("check_in",[None])[0]; co=q.get("check_out",[None])[0]; guests=q.get("guest_count",["1"])[0]
            if not valid_dates(ci,co) or not guests.isdigit() or not 1<=int(guests)<=8:
                self.send_json(400,error("VALIDATION_ERROR","Choose valid stay dates and guest count.")); return
            query=urllib.parse.urlencode({"check_in":ci,"check_out":co,"guest_count":guests})
            code,data=service_request(AVAILABILITY_URL,"/internal/rooms?"+query)
            if code==200: self.send_json(200,data)
            elif code is None: self.send_json(503,error("SERVICE_UNAVAILABLE","Availability service is unavailable."))
            else: self.send_json(code,data or error("SERVICE_UNAVAILABLE","Could not search rooms."))
            return
        with connect() as c:
            if path=="/admin/bookings":
                q=urllib.parse.parse_qs(parsed.query); start=q.get("from_date",[None])[0]; end=q.get("to_date",[None])[0]; room_id=q.get("room_id",[None])[0]
                for day in (start,end):
                    if day:
                        try:
                            if date.fromisoformat(day).isoformat()!=day: raise ValueError()
                        except ValueError: self.send_json(400,error("VALIDATION_ERROR","Invalid history date filter.")); return
                if start and end and start>end: self.send_json(400,error("VALIDATION_ERROR","From date must be on or before to date.")); return
                if room_id and room_id not in {"R101","R102","R201","R202","R301","R302"}: self.send_json(400,error("VALIDATION_ERROR","Unknown room filter.")); return
                sql="SELECT b.*,c.phone_number,COALESCE(n.guest_name,'Guest') guest_name,r.room_number FROM bookings b JOIN customers c USING(customer_id) LEFT JOIN booking_names n USING(booking_id) LEFT JOIN (SELECT 'R101' room_id,'101' room_number UNION SELECT 'R102','102' UNION SELECT 'R201','201' UNION SELECT 'R202','202' UNION SELECT 'R301','301' UNION SELECT 'R302','302') r USING(room_id) WHERE 1=1"
                params=[]
                if start: sql+=" AND b.check_out>?"; params.append(start)
                if end: sql+=" AND b.check_in<=?"; params.append(end)
                if room_id: sql+=" AND b.room_id=?"; params.append(room_id)
                sql+=" ORDER BY b.check_in DESC,b.created_at DESC"
                rows=c.execute(sql,params).fetchall()
                result=[dict(as_booking(x),phone_number=x['phone_number'],guest_name=x['guest_name'],room_number=x['room_number']) for x in rows]
                self.send_json(200,{"bookings":result}); return
            if path=="/admin/day":
                day=urllib.parse.parse_qs(parsed.query).get("date",[date.today().isoformat()])[0]
                try:
                    if date.fromisoformat(day).isoformat()!=day: raise ValueError()
                except (ValueError,TypeError): self.send_json(400,error("VALIDATION_ERROR","Invalid date.")); return
                rows=c.execute("SELECT b.*,c.phone_number,COALESCE(n.guest_name,'Guest') guest_name FROM bookings b JOIN customers c USING(customer_id) LEFT JOIN booking_names n USING(booking_id) WHERE status IN ('confirmed','checked_in') AND check_in<=? AND check_out>? ORDER BY room_id",(day,day)).fetchall()
                self.send_json(200,{"bookings":[dict(as_booking(x),phone_number=x['phone_number'],guest_name=x['guest_name']) for x in rows]}); return
            self.send_json(404,error("NOT_FOUND","Route not found."))

    def admin_post(self,path):
        if path=="/admin/bookings":
            body,problem=self.read_json()
            if problem: self.body_error(problem); return
            phone=normalize_phone(body.get("phone_number")); rid=body.get("room_id"); ci=body.get("check_in"); co=body.get("check_out"); guests=body.get("guest_count"); name=body.get("guest_name")
            if not phone or not valid_id(rid) or not valid_dates(ci,co) or type(guests) is not int or not 1<=guests<=8 or not isinstance(name,str) or not name.strip(): self.send_json(400,error("VALIDATION_ERROR","Provide phone, room, guest name, guest count and valid dates.")); return
            with connect() as c:
                c.execute("INSERT OR IGNORE INTO customers VALUES(?,?,?)",("C"+uuid.uuid4().hex[:16],phone,stamp()))
                customer=c.execute("SELECT customer_id FROM customers WHERE phone_number=?",(phone,)).fetchone()[0]
                bid="B"+uuid.uuid4().hex[:16]; current=stamp()
                c.execute("INSERT INTO bookings(booking_id,customer_id,request_key,request_json,room_id,check_in,check_out,guest_count,status,currency,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(bid,customer,"ADMIN"+uuid.uuid4().hex,"{}",rid,ci,co,guests,"pending","MYR",current,current)); c.execute("INSERT INTO booking_names VALUES(?,?)",(bid,name.strip()))
            code,data=service_request(AVAILABILITY_URL,"/internal/reservations","POST",{"booking_id":bid,"room_id":rid,"check_in":ci,"check_out":co,"guest_count":guests})
            if code not in (200,201): update_booking(bid,status="rejected"); self.send_json(409,data or error("ROOM_UNAVAILABLE","Room unavailable.")); return
            update_booking(bid,status="confirmed",reservation_id=data.get("reservation_id"),total_price_cents=data.get("total_price_cents"),currency=data.get("currency","MYR")); notify(customer,bid,"Booking created by admin"); self.send_json(201,as_booking(load_booking(bid))); return
        match=re.fullmatch(r"/admin/bookings/([A-Za-z0-9_-]{1,64})/(cancel|checkin|checkout|modify)",path)
        if not match: self.send_json(404,error("NOT_FOUND","Route not found.")); return
        bid,action=match.groups()
        with connect() as c: row=c.execute("SELECT * FROM bookings WHERE booking_id=?",(bid,)).fetchone()
        if not row: self.send_json(404,error("NOT_FOUND","Booking not found.")); return
        if action=="cancel":
            code,_=service_request(AVAILABILITY_URL,"/internal/reservations/"+urllib.parse.quote(bid)+"/release","POST")
            if code!=200: self.send_json(503,error("SERVICE_UNAVAILABLE","Could not release room inventory.")); return
            update_booking(bid,status="cancelled"); notify(row['customer_id'],bid,"Booking cancelled by admin")
        elif action in ("checkin","checkout"):
            status="checked_in" if action=="checkin" else "checked_out"
            update_booking(bid,status=status); notify(row['customer_id'],bid,"Check in successful" if action=="checkin" else "Check out successful")
        else:
            body,problem=self.read_json()
            if problem: self.body_error(problem); return
            ci,co=body.get("check_in"),body.get("check_out")
            if not valid_dates(ci,co): self.send_json(400,error("VALIDATION_ERROR","Invalid booking dates.")); return
            code,data=service_request(AVAILABILITY_URL,"/internal/reservations/"+urllib.parse.quote(bid)+"/modify","POST",{"check_in":ci,"check_out":co})
            if code!=200:
                self.send_json(409,data or error("ROOM_UNAVAILABLE","Room unavailable for those dates.")); return
            update_booking(bid,check_in=ci,check_out=co,total_price_cents=data.get("total_price_cents")); notify(row['customer_id'],bid,"Booking dates changed by admin")
        with connect() as c: updated=c.execute("SELECT * FROM bookings WHERE booking_id=?",(bid,)).fetchone()
        self.send_json(200,as_booking(updated))
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
