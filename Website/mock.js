// mock.js: a pretend Booking service that lives inside the browser.
// It only runs when CONFIG.USE_MOCK is true. You can ignore this file once the
// real services work. It follows the same API rules as the real Booking service,
// so you can see what good replies look like (status codes, error shapes, duplicates).

const Mock = (() => {
  const ROOMS = [
    { room_id: "R101", room_number: "101", room_type: "standard", capacity: 2 },
    { room_id: "R102", room_number: "102", room_type: "standard", capacity: 2 },
    { room_id: "R201", room_number: "201", room_type: "deluxe", capacity: 4 },
    { room_id: "R301", room_number: "301", room_type: "suite", capacity: 6 },
  ];
  const bookings = {};   // booking_id -> booking
  const byKey = {};      // Idempotency-Key -> the reply we gave the first time
  let counter = 0;

  const err = (status, code, message) => ({ status, data: { error: { code, message } } });
  const copy = (o) => JSON.parse(JSON.stringify(o));
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const isDate = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "");

  // Two stays overlap if each one starts before the other ends.
  // (check_out is exclusive, so back-to-back stays do NOT overlap.)
  const overlaps = (a, b) => a.check_in < b.check_out && b.check_in < a.check_out;
  const isFree = (roomId, ci, co) =>
    !Object.values(bookings).some(
      (b) => b.room_id === roomId && b.status === "confirmed" && overlaps(b, { check_in: ci, check_out: co })
    );

  async function route(method, path, query, body, headers) {
    await wait(250); // pretend the network takes a moment

    if (path === "/health") return { status: 200, data: { service: "booking", status: "ok" } };

    if (method === "GET" && path === "/api/rooms") {
      const guests = Number(query.guest_count);
      if (!isDate(query.check_in) || !isDate(query.check_out) || query.check_in >= query.check_out || !(guests >= 1 && guests <= 8)) {
        return err(400, "VALIDATION_ERROR", "Check the dates and guest count.");
      }
      const rooms = ROOMS.filter((r) => r.capacity >= guests && isFree(r.room_id, query.check_in, query.check_out));
      return { status: 200, data: { rooms } };
    }

    if (method === "POST" && path === "/api/bookings") {
      const key = headers["Idempotency-Key"];
      if (!key) return err(400, "VALIDATION_ERROR", "Idempotency-Key header is required.");

      // Duplicate handling: same key again means "return what you gave me before".
      const saved = byKey[key];
      if (saved) return saved.status === 201 ? { status: 200, data: saved.data } : saved;

      const room = ROOMS.find((r) => r.room_id === (body || {}).room_id);
      if (!room) return err(404, "NOT_FOUND", "Room not found.");
      if (!isDate(body.check_in) || !isDate(body.check_out) || body.check_in >= body.check_out || !body.guest_name || !(body.guest_count >= 1 && body.guest_count <= room.capacity)) {
        return err(400, "VALIDATION_ERROR", "Invalid booking details.");
      }
      if (!isFree(room.room_id, body.check_in, body.check_out)) {
        const rejected = err(409, "ROOM_UNAVAILABLE", "Room unavailable for these dates.");
        byKey[key] = rejected;
        return rejected;
      }
      const id = "B" + String(++counter).padStart(3, "0");
      const now = new Date().toISOString();
      bookings[id] = { booking_id: id, room_id: room.room_id, guest_name: body.guest_name, check_in: body.check_in, check_out: body.check_out, guest_count: body.guest_count, status: "confirmed", notification_status: "pending", created_at: now };
      byKey[key] = { status: 201, data: bookings[id] };
      return byKey[key];
    }

    const match = path.match(/^\/api\/bookings\/([^/]+)(\/cancel)?$/);
    if (match) {
      const b = bookings[decodeURIComponent(match[1])];
      if (!b) return err(404, "NOT_FOUND", "Booking not found.");
      if (method === "GET" && !match[2]) return { status: 200, data: b };
      if (method === "POST" && match[2]) {
        if (b.status === "confirmed") b.status = "cancelled";   // frees the nights again
        return { status: 200, data: b };
      }
    }
    return err(404, "NOT_FOUND", "Unknown route.");
  }

  return { handle: async (...args) => copy(await route(...args)) };
})();
