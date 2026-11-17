"""Idempotent booking confirmation recorder (http://localhost:5003)."""
import json
import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get("NOTIFICATION_DB",ROOT/"notification.db"))
INTERNAL_KEY=os.environ.get("INTERNAL_KEY")
FIELDS={"event_id","event_type","booking_id","customer_id","room_id","check_in","check_out"}
def connect():
    c=sqlite3.connect(DB,timeout=10,isolation_level=None); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=10000"); return c
def init_db():
    with connect() as c: c.execute("CREATE TABLE IF NOT EXISTS notifications(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)")
def err(code,msg): return {"error":{"code":code,"message":msg}}
def valid_id(s): return isinstance(s,str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}",s) is not None
def utc_now(): return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
class Handler(BaseHTTPRequestHandler):
    def send_json(self,status,data):
        body=json.dumps(data,separators=(",",":")).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.cors(); self.end_headers(); self.wfile.write(body)
    def cors(self):
        origin=self.headers.get("Origin")
        if origin in ("http://localhost:3000","http://127.0.0.1:3000"): self.send_header("Access-Control-Allow-Origin",origin); self.send_header("Vary","Origin")
        self.send_header("Access-Control-Allow-Methods","POST, OPTIONS"); self.send_header("Access-Control-Allow-Headers","Content-Type, X-Internal-Key")
    def do_OPTIONS(self): self.send_response(204); self.cors(); self.end_headers()
    def do_GET(self):
        if urlparse(self.path).path!="/health": self.send_json(404,err("NOT_FOUND","Route not found.")); return
        try:
            with connect() as c: c.execute("SELECT 1")
            self.send_json(200,{"service":"notification","status":"ok"})
        except sqlite3.Error: self.send_json(503,{"service":"notification","status":"error"})
    def do_POST(self):
        if urlparse(self.path).path!="/internal/notifications": self.send_json(404,err("NOT_FOUND","Route not found.")); return
        supplied_key=self.headers.get("X-Internal-Key","")
        if not INTERNAL_KEY or not hmac.compare_digest(supplied_key.encode(),INTERNAL_KEY.encode()): self.send_json(401,err("UNAUTHORIZED","Invalid internal key.")); return
        try:
            if "application/json" not in self.headers.get("Content-Type","").lower(): self.send_json(415,err("UNSUPPORTED_MEDIA_TYPE","Content-Type must be application/json.")); return
            n=int(self.headers.get("Content-Length","0"))
            if n>16384: self.send_json(413,err("VALIDATION_ERROR","Request body is too large.")); return
            body=json.loads(self.rfile.read(n))
            if not isinstance(body,dict) or set(body)!=FIELDS or any(not valid_id(body.get(k)) for k in ("event_id","booking_id","customer_id","room_id")) or body.get("event_type")!="booking.confirmed" or not valid_date(body.get("check_in")) or not valid_date(body.get("check_out")) or body["check_in"]>=body["check_out"]:
                self.send_json(400,err("VALIDATION_ERROR","Invalid notification fields.")); return
            encoded=json.dumps(body,sort_keys=True,separators=(",",":"))
            with connect() as c:
                c.execute("BEGIN IMMEDIATE"); old=c.execute("SELECT payload FROM notifications WHERE event_id=?",(body["event_id"],)).fetchone()
                if old:
                    c.commit()
                    if old["payload"]!=encoded: self.send_json(409,err("IDEMPOTENCY_CONFLICT","Event ID was used with different contents."))
                    else: self.send_json(200,{"event_id":body["event_id"],"status":"recorded"})
                    return
                c.execute("INSERT INTO notifications VALUES(?,?,?)",(body["event_id"],encoded,utc_now())); c.commit()
            self.send_json(201,{"event_id":body["event_id"],"status":"recorded"})
        except (json.JSONDecodeError,ValueError): self.send_json(400,err("VALIDATION_ERROR","Malformed JSON request."))
        except sqlite3.Error: self.send_json(503,err("SERVICE_UNAVAILABLE","Notification storage is temporarily unavailable."))
    def log_message(self,fmt,*args): super().log_message(fmt,*args)
def valid_date(s):
    if not isinstance(s,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",s): return False
    try: return datetime.strptime(s,"%Y-%m-%d").date().isoformat()==s
    except ValueError: return False
if __name__=="__main__":
    if not INTERNAL_KEY: raise SystemExit("Set INTERNAL_KEY before starting the Notification service.")
    init_db(); port=int(os.environ.get("PORT","5003")); print(f"Notification service: http://localhost:{port}"); ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
