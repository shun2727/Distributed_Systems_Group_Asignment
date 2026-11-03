# Hotel Booking: Shared API Guide v1.1

03 November 2026. Same API paths and fields as v1.0, explained more simply. This is a design agreement, not implemented/tested service code.


## Who calls which API?

Use this guide so everyone sends and receives the same data. API means an address one program calls to ask another program to do something. JSON is the data in the request or reply. Keep the shared copy in docs/api-contract.md.

| Service | Address / connection |
| --- | --- |
| Booking - M2 | http://localhost:5001; the website (M1) calls it. |
| Availability - M3 | http://localhost:5002; Booking calls it. |
| Notification - M4 | http://localhost:5003; Booking calls it. |


### Complete API list: 13 operations

| Provider | Method and path | Purpose |
| --- | --- | --- |
| M2 | GET /api/rooms | Find rooms |
| M2 | POST /api/bookings | Create booking |
| M2 | GET /api/bookings | List own bookings |
| M2 | GET /api/bookings/{booking_id} | View one booking |
| M2 | POST /api/bookings/{booking_id}/cancel | Cancel booking |
| M3 | GET /internal/rooms | Find rooms |
| M3 | POST /internal/reservations | Reserve room nights |
| M3 | GET /internal/reservations/{booking_id} | Check reservation |
| M3 | POST /internal/reservations/{booking_id}/release | Release room nights |
| M4 | POST /internal/notifications | Save confirmation |
| M2 / M3 / M4 | GET /health on each service (3 operations) | Check service runs |


### Headers: information sent with the request

Website -> Booking: X-Customer-Token identifies a demo customer. Booking -> other services: X-Internal-Key is a server-only secret. Creating a booking also needs Idempotency-Key: a new UUID for each new booking attempt; reuse it when retrying that attempt. Send Content-Type: application/json with JSON bodies. Health needs no token.

Keep private tokens/keys out of GitHub and URLs. Booking identifies customer_id from the token, not from user-supplied JSON. Use two sample customers for the demo.


## Website to Booking: M1 calls M2


### Find rooms: GET /api/rooms

No JSON body. Required URL parameters: check_in=2026-11-10, check_out=2026-11-12 and guest_count=2. Booking asks Availability and returns 200 with this room-list format:

```json
{"rooms": [{
  "room_id": "R101", "room_number": "101",
  "room_type": "standard", "capacity": 2,
  "price_per_night_cents": 12000, "currency": "MYR"
}]}
```

No rooms found: return 200 with {"rooms": []}. A search does not reserve anything.


### Create: POST /api/bookings

Send the customer token, request key and this JSON:

```json
{"room_id": "R101", "check_in": "2026-11-10",
 "check_out": "2026-11-12", "guest_count": 2}
```

201 means confirmed. 202 means still pending. Both return the Booking format below. An identical retry returns the original booking: 200 if finished successfully, 202 if pending; a rejected attempt repeats its saved error.

```json
{
  "booking_id": "B001", "reservation_id": "RS001",
  "customer_id": "C001", "room_id": "R101",
  "check_in": "2026-11-10", "check_out": "2026-11-12",
  "guest_count": 2, "status": "confirmed",
  "total_price_cents": 24000, "currency": "MYR",
  "notification_status": "pending",
  "created_at": "2026-11-03T12:00:00Z",
  "updated_at": "2026-11-03T12:00:00Z"
}
```


### List, view and cancel: no request body

GET /api/bookings: 200 with a bookings array of full Booking objects above; an empty list is {"bookings": []}. Return only this customer's orders, newest first by created_at, then booking_id descending. GET /api/bookings/{booking_id}: 200 with one full Booking object.

POST /api/bookings/{booking_id}/cancel: return the full Booking object with status cancelled (200), or cancelling (202) while waiting for release. Do not delete the order. Another customer cannot view or cancel it: return 404.


## Booking to Availability: M2 calls M3


### Search: GET /internal/rooms

Use X-Internal-Key. Send the same three URL parameters as the website search. Return the same 200 rooms JSON on page 2, including an empty rooms array when nothing matches.


### Reserve: POST /internal/reservations

Use X-Internal-Key and JSON content type. Required request:

```json
{
  "booking_id": "B001", "room_id": "R101",
  "check_in": "2026-11-10", "check_out": "2026-11-12",
  "guest_count": 2
}
```

201 for a new reservation; 200 for the same request retried. Reply using this Reservation format:

```json
{
  "reservation_id": "RS001", "booking_id": "B001",
  "room_id": "R101", "check_in": "2026-11-10",
  "check_out": "2026-11-12", "guest_count": 2,
  "status": "reserved", "total_price_cents": 24000,
  "currency": "MYR"
}
```

Overlapping dates -> 409 ROOM_UNAVAILABLE. Same booking_id with changed fields -> 409 IDEMPOTENCY_CONFLICT. An ID that was released -> 409 RESERVATION_RELEASED. Unknown room -> 404; too many guests for the room -> 400.


### Check: GET /internal/reservations/{booking_id}

Use X-Internal-Key; no body. 200 returns the full Reservation format above with status reserved or released. Unknown booking_id -> 404. M2 uses this after a lost or late response.


### Release: POST /internal/reservations/{booking_id}/release

Use X-Internal-Key; no body. Return 200, even when repeated:

```json
{"booking_id": "B001", "status": "released"}
```

Keep the released record. If release arrives before reserve, save that ID as released anyway. A later reserve cannot reuse it. For this never-reserved ID, lookup returns the full Reservation shape with reservation_id, room_id, check_in, check_out, guest_count and total_price_cents null; booking_id, status and currency remain present. Otherwise keep the original details.


## Notifications and service checks


### Send confirmation: M2 calls M4

POST http://localhost:5003/internal/notifications. Use X-Internal-Key and JSON content type. All fields below are required; event_type must be booking.confirmed.

```json
{
  "event_id": "E001", "event_type": "booking.confirmed",
  "booking_id": "B001", "customer_id": "C001",
  "room_id": "R101", "check_in": "2026-11-10",
  "check_out": "2026-11-12"
}
```

M4 saves the notification before replying. 201 first time; 200 for the same event retried:

```json
{"event_id": "E001", "status": "recorded"}
```

Same event_id with changed contents -> 409 IDEMPOTENCY_CONFLICT. The same event must never create a second notification. Saving a record is enough for the demo; no real email integration is needed.


### If Notification is down

M2 keeps the booking confirmed and stores the notification as pending. Its background retry loop resends the SAME event_id. Only a 200/201 reply marks notification_status sent. Timeout/5xx stays pending; a 4xx needs the code/configuration fixed. M4 handles recording and duplicate detection; M2 handles sending and retrying.


### Check each service: M5 calls M2, M3 and M4

GET /health on ports 5001, 5002 and 5003. No token, request body or query parameters. Return 200 when this service and its own storage are ready:

```json
{"service": "booking", "status": "ok"}
```

Use service="availability" or service="notification" for the other services. If its own storage cannot be used, return 503:

```json
{"service": "booking", "status": "error"}
```

Booking can still report ok while Notification is stopped. Health checks describe each local service. The notification is a record of a confirmation event; the Booking status remains the source for the current order state.


## Shared fields, validation and errors


### Use these rules in every service

| Item | Rule |
| --- | --- |
| Fields | snake_case. All fields in request examples are required. Reject unknown body fields and null request values. |
| IDs | Strings, 1-64 letters/digits/hyphens/underscores. Do not depend on an ID format. |
| Dates | Real YYYY-MM-DD dates; check_in before check_out; maximum 30 nights. Checkout day is not occupied. Past demo dates are allowed for repeatable tests. |
| Guests | guest_count is an integer 1-8, not a string/boolean, and must fit room capacity. |
| Prices | Integer MYR cents; server calculates total from rate x nights. No price in create requests. |
| Timestamps | Server-generated UTC, ending Z. Booking dates are hotel-local calendar dates. |
| Booking reply | All shown fields present. reservation_id/total_price_cents may be null while pending/rejected. Keep price/reservation after cancellation. |
| States | Booking: pending, confirmed, rejected, cancelling, cancelled. Inventory: reserved/released. Notification: pending/sent. |


### Use the same error JSON everywhere except /health

```json
{"error": {"code": "ROOM_UNAVAILABLE",
           "message": "Room unavailable for these dates."}}
```

If an authenticated customer's order already exists, also add booking_id at the top level. Clients read code, not the wording of message. For a rejected create, save the error/status and return them on retry. Never return private keys or stack traces.

| HTTP | Error code / meaning |
| --- | --- |
| 400 / 401 | VALIDATION_ERROR (bad/missing fields or malformed JSON) / UNAUTHORIZED (bad token/key) |
| 404 / 405 | NOT_FOUND (missing or unowned item/route) / METHOD_NOT_ALLOWED |
| 409 | ROOM_UNAVAILABLE; IDEMPOTENCY_CONFLICT; RESERVATION_RELEASED; BOOKING_STATE_CONFLICT |
| 415 | UNSUPPORTED_MEDIA_TYPE (JSON body has wrong content type) |
| 500 / 503 | INTERNAL_ERROR / SERVICE_UNAVAILABLE (temporary storage/dependency failure) |

Booking allows the configured website origin (default localhost:3000), required methods and headers in CORS. OPTIONS preflight can return 204 without a token. JSON responses use application/json; GET/cancel/release have no body. Demo tokens are not production authentication.


## Six rules for safe bookings


### 1. Reserve the whole stay together - M3

Store one occupied row for each room/night. Add UNIQUE(room_id, stay_date). Insert the reservation and all nights in ONE database transaction: either everything succeeds or everything is undone. This prevents two users from booking the same nights. Release nights and mark released in one transaction too.


### 2. A retry must not create another order - M1/M2/M3/M4

M1 creates one request key per new attempt and keeps it on retry. M2 authenticates first and saves one order per customer/key, together with the original input. Compare field values, not JSON spacing/order. M3 uses booking_id the same way; M4 uses event_id. Check/save each ID together in a database transaction. Changed input with the same ID/key -> 409.


### 3. A timeout does not mean failure - M2

Save pending before calling reserve. If the reply is lost, return 202 pending. Check the reservation with the same booking_id: reserved -> confirm; 404 -> retry reserve with original fields; released -> reject. Temporary failures stay pending; definite rejection is saved as rejected. Never invent another booking ID after a timeout.


### 4. Cancel only after the room is released - M2/M3

For confirmed orders, save cancelling before calling release; mark cancelled only after 200. Otherwise stay cancelling (202) and retry. Repeat cancelled/rejected cancellation -> 200 unchanged; cancelling -> 202; pending -> 409 BOOKING_STATE_CONFLICT. Released IDs never reserve again, even if a delayed reserve arrives later.


### 5. Save confirmation and pending notification together - M2

Use one Booking database transaction to mark confirmed and save a pending event for the retry loop. Before confirmation, notification_status is pending but no event is sent. Never let an old recovery reply change cancelling/cancelled back to confirmed. Use database updates that only change the expected current state. Retry workers use a finite HTTP timeout, e.g. 3 seconds, and revisit pending work, e.g. every 5 seconds.


### 6. Show the real result - M1/M5

M1 refreshes pending/cancelling orders every 2 seconds for up to 30 seconds, then offers manual refresh. Same successful create replay returns 200 with its current Booking; pending stays 202; rejected repeats its saved error. M5 tests all 13 operations, concurrency, retries, ownership, lost replies, cancellation and notification outage/recovery. Save actual outputs. This guide is the target, not a claim that those tests passed.
