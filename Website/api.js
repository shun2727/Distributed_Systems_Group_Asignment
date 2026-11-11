// api.js: the ONLY file that talks to the Booking service.
//
// Read this file first if you want to learn how API calls work.
// Every button on the page ends up calling one of the five small functions at the
// bottom, and they all go through the single request() function.
//
// An API call is just an HTTP request, the same kind a browser sends when it loads
// a page, except that the reply is JSON data instead of HTML:
//
//   method   what to do        GET = read, POST = create or act
//   url      where to send it  http://localhost:5001 + a path such as /api/rooms
//   headers  extra info        Content-Type, Idempotency-Key
//   body     the data (JSON)   only for POST
//
// The reply has a status code (200 ok, 201 created, 400 bad input, 404 not found,
// 409 conflict, 503 service unavailable) and a JSON body.

const Api = (() => {
  const listeners = [];   // functions to tell about every call (used by the request log)

  async function request(method, path, { query, body, headers = {}, silent = false } = {}) {
    // 1. Build the URL. Query values go after "?", e.g. /api/rooms?check_in=2026-11-12&...
    let target = path;
    if (query) target += "?" + new URLSearchParams(query).toString();
    const url = CONFIG.API_BASE + target;

    // 2. Build the headers. If we send JSON, we must say so.
    const reqHeaders = { ...headers };
    if (body !== undefined) reqHeaders["Content-Type"] = "application/json";

    // 3. Send it and wait for the reply.
    const started = performance.now();
    let status = 0;
    let data = null;
    try {
      if (CONFIG.USE_MOCK) {
        ({ status, data } = await Mock.handle(method, path, query, body, reqHeaders));
      } else {
        const response = await fetch(url, {
          method,
          headers: reqHeaders,
          body: body !== undefined ? JSON.stringify(body) : undefined,  // objects must become text
        });
        status = response.status;
        data = await response.json().catch(() => null);                 // turn the JSON text back into an object
      }
    } catch (err) {
      // fetch() only throws when NOTHING answered: the service is stopped, the port
      // is wrong, or the browser blocked it (CORS). A 404 or 409 does not throw.
      data = { error: { code: "NETWORK_ERROR", message: String(err.message || err) } };
    }

    // 4. Hand back one simple result, and tell the request log what happened.
    const ms = Math.round(performance.now() - started);
    if (!silent) {
      listeners.forEach((fn) => fn({ method, url, target, requestHeaders: reqHeaders, requestBody: body, status, data, ms }));
    }
    return { ok: status >= 200 && status < 300, status, data };
  }

  // The five calls the page needs. Each one matches a row in the API list (report 4.2).
  return {
    onLog: (fn) => listeners.push(fn),

    // GET /health: is the service running? (not shown in the log)
    health: () => request("GET", "/health", { silent: true }),

    // GET /api/rooms?check_in=...&check_out=...&guest_count=...  (FR1)
    searchRooms: (query) => request("GET", "/api/rooms", { query }),

    // POST /api/bookings  (FR2, FR3)
    // The Idempotency-Key header is a random ID for this booking attempt. If the same
    // request is sent twice with the same key, the server returns the original booking
    // instead of creating a second one.
    createBooking: (body, key) =>
      request("POST", "/api/bookings", { body, headers: { "Idempotency-Key": key } }),

    // GET /api/bookings/{booking_id}  (FR5)
    getBooking: (id) => request("GET", `/api/bookings/${encodeURIComponent(id)}`),

    // POST /api/bookings/{booking_id}/cancel  (FR6)
    cancelBooking: (id) => request("POST", `/api/bookings/${encodeURIComponent(id)}/cancel`),
  };
})();
