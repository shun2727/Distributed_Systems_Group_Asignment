from contextlib import closing
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.folder.name) / "test.db")
        real_connect = sqlite3.connect
        with patch("sqlite3.connect", side_effect=lambda *a, **kw: real_connect(cls.db_path)):
            cls.module = importlib.import_module("booking.app")
        cls.module.app.config.update(TESTING=True, DATABASE=cls.db_path)
        cls.module.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            for name in ("notification_events", "booking_requests", "bookings"):
                db.execute(f"DELETE FROM {name}")
        self.client = self.module.app.test_client()
