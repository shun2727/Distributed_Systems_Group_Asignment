#!/usr/bin/env python3
"""Reset AirnStay's local databases to their initial seeded state."""

import importlib.util
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def load_service(name, path):
    spec = importlib.util.spec_from_file_location(f"airnstay_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    availability = load_service("availability", ROOT / "availability" / "availability.py")
    booking = load_service("booking", ROOT / "booking" / "booking.py")
    notification = load_service("notification", ROOT / "notification" / "notification.py")

    availability_db = ROOT / "availability" / "availability.db"
    booking_db = ROOT / "booking" / "booking.db"
    notification_db = ROOT / "notification" / "notification.db"

    # Initialize missing databases and schemas, then use the project-defined room seed.
    availability.DB = availability_db
    booking.DB = booking_db
    notification.DB = notification_db
    availability.init_db()
    booking.init_db()
    notification.init_db()

    with sqlite3.connect(availability_db, timeout=10) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("ATTACH DATABASE ? AS booking_db", (str(booking_db),))
        db.execute("ATTACH DATABASE ? AS notification_db", (str(notification_db),))
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM booking_db.sessions")
        db.execute("DELETE FROM booking_db.admin_sessions")
        db.execute("DELETE FROM booking_db.notifications")
        db.execute("DELETE FROM booking_db.booking_names")
        db.execute("DELETE FROM booking_db.bookings")
        db.execute("DELETE FROM booking_db.customers")
        db.execute("DELETE FROM notification_db.notifications")
        db.execute("DELETE FROM main.occupied_nights")
        db.execute("DELETE FROM main.reservations")
        db.execute("DELETE FROM main.rooms")
        db.executemany(
            "INSERT INTO main.rooms(room_id, room_number, room_type, capacity, price_per_night_cents) "
            "VALUES(?, ?, ?, ?, ?)",
            availability.ROOMS,
        )
        db.commit()

    print("AirnStay databases reset. Seed rooms are restored; all booking data is cleared.")


if __name__ == "__main__":
    main()
