import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import test_auth as auth_tests


ROOM = {"room_id": "R101", "room_number": "101", "room_type": "standard",
        "capacity": 2, "price_per_night_cents": 12000, "currency": "MYR"}


class AvailabilityStub(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.calls.append((self.path, dict(self.headers)))
        delay, status, body = self.server.delay, self.server.reply_status, self.server.reply_body
        if delay:
            time.sleep(delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            pass

    def log_message(self, *args):
        pass


class RoomSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth_tests.AuthTests.setUpClass.__func__(cls)
        # Another test class may have imported the app with its own temporary path.
        cls.module.init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), AvailabilityStub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        auth_tests.AuthTests.tearDownClass.__func__(cls)

    register = auth_tests.AuthTests.register
    login = auth_tests.AuthTests.login

    def setUp(self):
        auth_tests.AuthTests.setUp(self)
        self.register()
        self.headers = {"X-Customer-Token": self.login().json["token"]}
        self.params = {"check_in": "2026-11-10", "check_out": "2026-11-12", "guest_count": "2"}
        self.server.calls = []
        self.server.delay = 0
        self.server.reply_status = 200
        self.server.reply_body = json.dumps({"rooms": [ROOM]}).encode()
        self.module.app.config.update(
            AVAILABILITY_URL=f"http://127.0.0.1:{self.server.server_port}",
            INTERNAL_KEY="test-internal-key", AVAILABILITY_TIMEOUT=0.5,
        )

    def search(self, **params):
        return self.client.get("/api/rooms", query_string=params or self.params, headers=self.headers)

    def test_search_calls_real_http_stub_and_forwards_only_internal_key(self):
        response = self.search()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"rooms": [ROOM]})
        path, headers = self.server.calls[0]
        self.assertEqual(urlparse(path).path, "/internal/rooms")
        self.assertEqual(parse_qs(urlparse(path).query), {k: [v] for k, v in self.params.items()})
        self.assertEqual(headers["X-Internal-Key"], "test-internal-key")
        self.assertNotIn("X-Customer-Token", headers)

    def test_empty_room_list_is_success(self):
        self.server.reply_body = b'{"rooms": []}'
        self.assertEqual(self.search().json, {"rooms": []})

    def test_requires_login_before_contacting_availability(self):
        for headers in ({}, {"X-Customer-Token": "wrong"}):
            response = self.client.get("/api/rooms", query_string=self.params, headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.server.calls, [])

    def test_validates_query_before_contacting_availability(self):
        cases = [dict(self.params, check_in="2026-02-30"),
                 dict(self.params, check_in="20261110"),
                 dict(self.params, check_out="2026-11-10"),
                 dict(self.params, check_out="2026-12-11"),
                 dict(self.params, guest_count="0"), dict(self.params, guest_count="9"),
                 dict(self.params, guest_count="2.0"), dict(self.params, guest_count="true"),
                 dict(self.params, extra="unexpected"), {"check_in": "2026-11-10"}]
        for params in cases:
            response = self.search(**params)
            self.assertEqual(response.status_code, 400, params)
            self.assertEqual(response.json["error"]["code"], "VALIDATION_ERROR")
        duplicate = list(self.params.items()) + [("guest_count", "3")]
        self.assertEqual(self.client.get("/api/rooms", query_string=duplicate, headers=self.headers).status_code, 400)
        self.assertEqual(self.server.calls, [])

    def test_thirty_nights_are_allowed(self):
        response = self.search(check_in="2026-11-10", check_out="2026-12-10", guest_count="2")
        self.assertEqual(response.status_code, 200)

    def test_missing_key_does_not_send_request(self):
        self.module.app.config["INTERNAL_KEY"] = ""
        response = self.search()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.server.calls, [])

    def test_unavailable_or_slow_service_returns_safe_error(self):
        self.server.delay = .2
        self.module.app.config["AVAILABILITY_TIMEOUT"] = .03
        response = self.search()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"]["code"], "SERVICE_UNAVAILABLE")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.module.app.config["AVAILABILITY_URL"] = f"http://127.0.0.1:{port}"
        self.assertEqual(self.search().status_code, 503)

    def test_bad_upstream_responses_are_not_exposed_as_room_lists(self):
        for status, body in [(500, b'{"secret":"do-not-forward"}'),
                             (401, b'{"error":{"code":"UNAUTHORIZED"}}'),
                             (302, b''), (200, b'not-json'), (200, b'[]'),
                             (200, b'{"rooms": null}'), (200, b'{"rooms": [{}]}'),
                             (200, json.dumps({"rooms": [dict(ROOM, capacity=True)]}).encode()),
                             (200, json.dumps({"rooms": [dict(ROOM, price_per_night_cents=-1)]}).encode())]:
            self.server.reply_status, self.server.reply_body = status, body
            response = self.search()
            self.assertEqual(response.status_code, 503, body)
            self.assertEqual(response.json["error"]["code"], "SERVICE_UNAVAILABLE")
            self.assertNotIn("do-not-forward", response.get_data(as_text=True))
