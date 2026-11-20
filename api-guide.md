# AirnStay: Shared API Guide

Updated 20 November 2026 to match the current implementation in `booking/booking.py`, `availability/availability.py`, and `notification/notification.py`.


## Who calls which API?

Use this guide so everyone sends and receives the same data. API means an address one program calls to ask another program to do something. JSON is the data in the request or reply. The shared source is api-guide.md in the repository root. It replaces api-contract.md; use this version when building services.

| Service | Address / connection |
| --- | --- |
| Booking - M2 | http://localhost:5001; the website (M1) calls it. |
| Availability - M3 | http://localhost:5002; Booking calls it. |
| Notification - M4 | http://localhost:5003; Booking calls it. |


### Implemented API routes

| Owner | Method and path | Purpose |
| --- | --- | --- |
| M2 | POST /api/login | Open or resume a phone-number session |
| M2 | POST /api/logout | Revoke the current customer session |
| M2 | POST /api/admin/identify | Check an admin access sequence |
| M2 | POST /api/admin/login | Open an admin session using sequence and password |
| M2 | POST /api/admin/logout | Revoke the current admin session |
| M2 | GET /api/rooms | Find rooms for the customer |
| M2 | POST /api/bookings | Create a booking for the customer |
| M2 | GET /api/bookings | List the customer's bookings |
| M2 | GET /api/bookings/{booking_id} | View one owned booking |
| M2 | POST /api/bookings/{booking_id}/cancel | Cancel an owned booking |
| M2 | GET /api/notifications | List notifications and unread count |
| M2 | POST /api/notifications/read | Mark all customer notifications read |
| M2 | POST /api/notifications/clear | Delete all customer notifications |
| M2 | GET /admin/rooms | Find rooms for an admin-created booking |
| M2 | GET /admin/day | List active bookings overlapping a date |
| M2 | GET /admin/stats | Get daily dashboard counts and arrival revenue |
| M2 | GET /admin/bookings | List booking history with optional filters |
| M2 | POST /admin/bookings | Create a booking for a guest |
| M2 | POST /admin/bookings/{booking_id}/cancel | Cancel a booking as admin |
| M2 | POST /admin/bookings/{booking_id}/checkin | Check in a guest |
| M2 | POST /admin/bookings/{booking_id}/checkout | Check out a guest |
| M2 | POST /admin/bookings/{booking_id}/modify | Change the stay dates |
| M3 | GET /internal/rooms | Find rooms |
| M3 | POST /internal/reservations | Reserve room nights |
| M3 | GET /internal/reservations/{booking_id} | Check reservation |
| M3 | POST /internal/reservations/{booking_id}/release | Release room nights |
| M3 | POST /internal/reservations/{booking_id}/modify | Change reserved dates |
| M4 | POST /internal/notifications | Save a booking-confirmation event |
| M2 / M3 / M4 | GET /health on each service | Check service runs |

### Booking request flow

Keep these as separate processes and APIs. `booking/booking.py` owns the client-facing booking API and coordinates the other services by HTTP; it does not import or combine their implementation code. Availability owns room inventory and reservations. Notification owns confirmation records.

| Step | From -> To | Request | Reply |
| --- | --- | --- | --- |
| 1. Search | Client -> Booking | GET /api/rooms | Room list from Booking |
| 2. Check availability | Booking -> Availability | GET /internal/rooms | Available room list |
| 3. Submit booking | Client -> Booking | POST /api/bookings | Booking result; a guest name is stored with it |
| 4. Reserve room | Booking -> Availability | POST /internal/reservations | Reserved, conflict, or unknown room |
| 5. Record confirmation | Booking -> Notification | POST /internal/notifications | Acknowledgement; failed delivery remains pending and is retried |

Steps 1-2 run when the guest searches. Steps 3-5 run when the guest books. Booking sends the confirmation only after Availability reserves the room. If Notification is unavailable, the booking remains confirmed with notification status pending, and Booking retries it in the background.

| Later action | From -> To | Request | Reply |
| --- | --- | --- | --- |
| List bookings | Client -> Booking | GET /api/bookings | This customer's bookings |
| View or check progress | Client -> Booking | GET /api/bookings/{booking_id} | Current order |
| Cancel | Client -> Booking | POST /api/bookings/{booking_id}/cancel | Cancelled or cancelling |
| Release room | Booking -> Availability | POST /internal/reservations/{booking_id}/release | Released acknowledgement |

The admin console uses a separate admin token. It supports room search, creating guest bookings, listing bookings for a selected date, daily statistics, date and room filtered history, cancellation, check-in, check-out, and date changes. Booking writes user-facing notifications for these actions directly to its notification table. The Notification service is a separate internal event recorder for confirmed booking events; its records are not the user's notification-tab data.

The services use these default addresses:

| Component | Address |
| --- | --- |
| Client website | http://localhost:3000 |
| Booking | http://localhost:5001 |
| Availability | http://localhost:5002 |
| Notification | http://localhost:5003 |


### Headers: information sent with the request

Customer website -> Booking: X-Customer-Token carries the token returned by `/api/login`. Protected `/api/*` routes require it. Admin website -> Booking: X-Admin-Token carries the token returned by `/api/admin/login`; protected `/admin/*` routes require it. `/api/admin/identify` and `/api/admin/login` are public authentication routes. Logout deletes the current token hash. Booking -> other services: X-Internal-Key is a server-only secret. `run.sh` creates a random key for each launch unless INTERNAL_KEY is supplied. Creating a customer booking also needs Idempotency-Key: a new UUID per new attempt; reuse it when retrying that attempt. Send Content-Type: application/json with JSON bodies. Health and OPTIONS preflight need no token.

Keep private tokens/keys out of GitHub and URLs. Booking identifies customer_id from the token, not from user-supplied JSON. Customer IDs are generated by Booking; do not assume a particular ID format or let the browser choose an identity.


## Website to Booking: M1 calls M2


### Phone-number sessions: M1 builds the page, M2 builds the API

Flow: enter a phone number -> open or resume its session -> send the returned token with API requests. Booking keeps a stable customer record for each normalized phone number so the same number always sees the same bookings. M3 and M4 continue using X-Internal-Key; they do not implement login.

**POST /api/login**: no customer token. Content-Type: application/json. Send exactly:

```json
{"phone_number": "+60123456789"}
```

Normalize phone number by trimming spaces and punctuation, then storing it in international form with a leading plus and 8-15 digits. On first use, create a customer record; on later use, return the same customer_id. Reject extra fields, nulls, non-string values and malformed JSON with 400 VALIDATION_ERROR; wrong content type -> 415 UNSUPPORTED_MEDIA_TYPE. Return 200:

```json
{
  "token": "random-token-returned-only-at-login",
  "customer_id": "C_generated_id",
  "expires_at": "2026-11-07T12:00:00Z"
}
```

Token expires two hours after login. Its value is opaque and is not a customer ID. Generate unpredictable tokens with Python secrets and save only SHA-256 token hashes in the sessions table, together with customer_id and UTC expiry. A repeat login with the same phone number returns the same customer_id and a new token.

M1: keep the login token in browser memory and attach it as X-Customer-Token on protected calls. A reload requires entering the phone number again. On 401, clear the token and show Login. Sign out clears the in-memory token and returns the browser to guest mode. Missing, unknown or expired tokens on protected routes return 401 UNAUTHORIZED using the shared error shape. Phone-number-only login does not verify number ownership; use an OTP flow before storing real personal data.


### Find rooms: GET /api/rooms

Use X-Customer-Token. No JSON body. Required URL parameters: check_in, check_out and guest_count (1-8). Booking asks Availability and returns 200 with this room-list format:

```json
{"rooms": [{
  "room_id": "R101", "room_number": "101",
  "room_type": "single", "capacity": 1,
  "price_per_night_cents": 8000, "currency": "MYR"
}]}
```

No rooms found: return 200 with {"rooms": []}. A search does not reserve anything.


### Create: POST /api/bookings

Send the customer token, request key and this JSON:

```json
{"room_id": "R101", "check_in": "2026-11-10",
 "check_out": "2026-11-12", "guest_count": 1, "guest_name": "Alex Tan"}
```

**201** = new confirmed booking; **202** = pending. Both return the full Booking object:

```json
{
  "booking_id": "B001",
  "room_id": "R101",
  "check_in": "2026-11-15",
  "check_out": "2026-11-17",
  "guest_count": 2,
  "status": "confirmed",
  "notification_status": "pending",
  "created_at": "2026-11-10T10:00:00Z",
  "updated_at": "2026-11-10T10:00:00Z"
}
```

Booking generates an unpredictable booking ID. A customer is identified by the stable customer record for their normalized phone number. Save the idempotency key and booking input with the booking. Compare field values, not JSON spacing/order.

Same key + same input â†’ existing booking: 202 if pending/cancelling, 200 if confirmed/cancelled; rejected attempts repeat their saved error. Same key + changed input â†’ 409 IDEMPOTENCY_CONFLICT.

### List, view and cancel

- `GET /api/bookings` â†’ **200**, `{"bookings": [...]}` for the authenticated customer.
- `GET /api/bookings/{booking_id}` â†’ **200**, full Booking object, including rejected orders.
- `POST /api/bookings/{booking_id}/cancel` â†’ **200**, full object with status cancelled; or **202**, status cancelling while release is unresolved.
- Unknown booking â†’ 404. Repeat cancelled cancellation â†’ 200 unchanged; repeat cancelling â†’ 202; cancel pending/rejected â†’ 409 BOOKING_STATE_CONFLICT.

The booking response includes `booking_id`, `reservation_id`, `customer_id`, `room_id`, `check_in`, `check_out`, `guest_count`, `status`, price and currency fields, `notification_status`, and timestamps. `guest_name` is accepted on create and stored for the admin history and desk views; it is not included in the customer booking response.

### Client feedback

| Result | Display |
| --- | --- |
| confirmed | Confirmation and booking ID; a user notification is saved |
| 409 ROOM_UNAVAILABLE | Room unavailable |
| pending | Booking is being processed |
| cancelling | Cancellation is being processed |
| cancelled | Cancellation confirmed |
| rejected | Booking rejected |
| 503 / network error | Unable to get result; preserve existing ID/request key |

The website polls pending bookings and refreshes the customer's notification list. If the create response is lost, retry POST with the original key and input to retrieve the same order. Do not automatically create a new attempt.

## Customer notifications

`GET /api/notifications` returns this customer's notification records newest first and the unread total. Each record contains `notification_id`, `booking_id`, `message`, `created_at` (UTC timestamp), and `is_read`.

```json
{"notifications":[{"notification_id":"N123","booking_id":"B123","message":"Booking confirmed","created_at":"2026-11-20T13:00:00Z","is_read":0}],"unread_count":1}
```

`POST /api/notifications/read` marks all of the customer's notifications read and returns `{"status":"ok"}`. `POST /api/notifications/clear` deletes all of them and returns `{"status":"cleared"}`. Both require X-Customer-Token and have no request body. Booking creates messages for booking confirmation/cancellation and admin-created, admin-cancelled, check-in, check-out, and date-changed bookings. The user interface displays each message with its timestamp and shows the unread count on the bell.

## Admin authentication and booking management

`POST /api/admin/identify` takes `{"number":"AIRN7K29ADMIN"}` without a token. It returns `{"admin":true}` for the configured sequence or 404 otherwise. `POST /api/admin/login` takes `{"number":"...","password":"..."}` and returns `{"token":"...","expires_at":"..."}`; invalid credentials return 401. Defaults printed by `run.sh` are `AIRN7K29ADMIN` and `admin123`. Set `ADMIN_NUMBER` and `ADMIN_PASSWORD` before starting services to change them. Send the login token as X-Admin-Token to `/admin/*`. `POST /api/admin/logout` revokes that token.

| Admin route | Request and result |
| --- | --- |
| `GET /admin/rooms?check_in=YYYY-MM-DD&check_out=YYYY-MM-DD&guest_count=1` | Available rooms for the requested stay |
| `POST /admin/bookings` | JSON: `phone_number`, `guest_name`, `room_id`, `check_in`, `check_out`, `guest_count`; returns 201 and the booking |
| `GET /admin/day?date=YYYY-MM-DD` | Confirmed or checked-in bookings whose stay overlaps that date, including guest name and phone |
| `GET /admin/stats?date=YYYY-MM-DD` | Daily `arrivals`, `departures`, `in_house`, `cancelled`, and `arrival_revenue_cents` |
| `GET /admin/bookings?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD&room_id=R101` | Booking history; all filters are optional, date range overlaps stays |
| `POST /admin/bookings/{booking_id}/cancel` | Cancels and releases the room, then notifies the guest |
| `POST /admin/bookings/{booking_id}/checkin` | Sets status to `checked_in` and notifies the guest |
| `POST /admin/bookings/{booking_id}/checkout` | Sets status to `checked_out` and notifies the guest |
| `POST /admin/bookings/{booking_id}/modify` | JSON: `{"check_in":"YYYY-MM-DD","check_out":"YYYY-MM-DD"}`; changes dates if available and notifies the guest |

Admin actions return the updated Booking object. A conflicting date change returns 409. The day desk includes a stay when `check_in <= date < check_out`; cancelled stays are excluded. History may include cancelled and checked-out records.

## 3. Booking â†’ Availability

All calls require X-Internal-Key. Availability owns inventory.

### Reserve

`POST /internal/reservations`

```json
{"booking_id": "B001", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17", "guest_count": 2}
```

**201** for new reservation; **200** for identical retry:

```json
{"booking_id": "B001", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17", "guest_count": 2, "status": "reserved"}
```

Overlap â†’ 409 ROOM_UNAVAILABLE. Changed input with same ID â†’ 409 IDEMPOTENCY_CONFLICT. Released ID â†’ 409 RESERVATION_RELEASED. Unknown room â†’ 404. Guests above room capacity â†’ 400.

Insert reservation and all occupied nights in one transaction, using UNIQUE(room_id, stay_date). Any conflict rolls back the entire stay. Release nights and mark released together too.

### Check after a lost reply

`GET /internal/reservations/{booking_id}` â†’ **200**, full Reservation object above, status reserved or released. Unknown ID â†’ 404.

Booking saves pending before calling reserve. Timeout means the outcome is unknown: return 202 pending. The background worker checks with the same booking ID: reserved â†’ confirm; 404 â†’ retry reserve using original input; released â†’ reject. Definite reservation rejections are saved as rejected; temporary failures remain pending.

### Modify an existing reservation

`POST /internal/reservations/{booking_id}/modify` requires X-Internal-Key and accepts `{"check_in":"YYYY-MM-DD","check_out":"YYYY-MM-DD"}`. It returns the updated reservation and recalculated total on 200. Unknown or inactive reservations return 404; unavailable dates return 409 ROOM_UNAVAILABLE. The change to occupied nights and reservation dates is transactional.

### Release

`POST /internal/reservations/{booking_id}/release` â†’ **200**, including repeated requests:

```json
{"booking_id": "B001", "status": "released"}
```

Retain released IDs so delayed reserve calls cannot reuse them. If release arrives before reserve, save the ID as released anyway. GET then returns the full Reservation object with room_id, check_in, check_out and guest_count null. For previously reserved IDs, preserve their original details.

Booking saves cancelling before release, then cancelled only after a matching release acknowledgement. Failure/timeout â†’ 202 cancelling and background retry using the same ID. Keep the order record.

## 4. Booking â†’ Notification

`POST /internal/notifications`, with X-Internal-Key:

```json
{"event_id": "E001", "event_type": "booking.confirmed", "booking_id": "B001", "customer_id": "C001", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17"}
```

Save before replying: **201** first time; **200** for an identical retry:

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


## Local configuration and scaling notes

`./run.sh` starts all services with default ports 3000, 5001, 5002 and 5003. The ports can be changed with WEBSITE_PORT, BOOKING_PORT, AVAILABILITY_PORT and NOTIFICATION_PORT. Booking's upstream URLs can be changed with AVAILABILITY_URL and NOTIFICATION_URL. If starting services in separate terminals, set the same strong INTERNAL_KEY in each service's environment. Set CLIENT_ORIGINS to the exact website origin when using a non-default web port.

The database paths can be configured with BOOKING_DB, AVAILABILITY_DB and NOTIFICATION_DB. The current SQLite files are appropriate for this local demonstration. For multiple service instances on one machine, instances of a service must share that service's database file; for instances on different machines, replace SQLite with a shared server database such as PostgreSQL. Keep Availability's unique room-night constraint when migrating so concurrent requests still cannot reserve the same room night. A load balancer and shared database are future deployment steps, not capabilities demonstrated by the local launcher.

Phone-number login identifies a demo customer but does not verify ownership of that number. Do not use real personal data. Production deployment would add phone verification and HTTPS, and would provide INTERNAL_KEY through a secret manager.

## Shared fields, validation and errors


### Use these rules in every service

| Item | Rule |
| --- | --- |
| JSON | snake_case; example request fields required; reject unknown fields, nulls and malformed JSON |
| guest_name | Customer booking: trimmed, non-empty, at most 100 characters; admin booking: non-empty; allow non-ASCII names |
| IDs | Strings, 1â€“64 letters/digits/underscores/hyphens; do not depend on example formats |
| Dates | Real YYYY-MM-DD; check-in before check-out; at most 30 nights; past demo dates allowed |
| Nights | Check-in included, check-out excluded: 15â€“17 November occupies nights 15 and 16 |
| guest_count | Integer 1â€“8, not boolean/string; must fit room capacity |
| Timestamps | Server-generated UTC ending Z |
| Booking states | pending, confirmed, rejected, cancelling, cancelled, checked_in, checked_out |
| Notification states | pending, sent; no event is sent before confirmation |
| Recovery | Finite HTTP timeouts (e.g. 3 seconds), retry saved work (e.g. every 5 seconds) |
| Storage | Parameterized SQL; no DB locks held during HTTP calls |
| CORS | Booking allows configured Client origin (default http://localhost:3000), GET/POST/OPTIONS, Content-Type and Idempotency-Key; OPTIONS â†’ 204 |

### Error JSON

```json
{"error": {"code": "ROOM_UNAVAILABLE", "message": "Room unavailable for these dates."}, "booking_id": "B001"}
```

Add booking_id when an associated order exists; omit otherwise. Client reads code, not message wording. Never return keys or stack traces. Avoid real guest data in logs/screenshots. Knowing an ID is not authentication.

| HTTP | Code |
| --- | --- |
| 400 / 401 | VALIDATION_ERROR (bad/missing fields or malformed JSON) / UNAUTHORIZED (wrong login, missing/invalid/expired token, or bad internal key) |
| 404 / 405 | NOT_FOUND (missing or unowned item/route) / METHOD_NOT_ALLOWED |
| 409 | ROOM_UNAVAILABLE; IDEMPOTENCY_CONFLICT; RESERVATION_RELEASED; BOOKING_STATE_CONFLICT |
| 413 | VALIDATION_ERROR (Booking request body exceeds 16 KiB) |
| 415 | UNSUPPORTED_MEDIA_TYPE (JSON body has wrong content type) |
| 500 / 503 | INTERNAL_ERROR / SERVICE_UNAVAILABLE (temporary storage/dependency failure) |

Booking allows the configured website origin (default localhost:3000), required methods and headers in CORS. OPTIONS preflight can return 204 without a token. JSON responses use application/json; GET/cancel/release have no body. Login replies use Cache-Control: no-store. Browser reload or token expiry requires login again.

### Run the local system

For the usual local setup, run the launcher from the repository root. It generates one random internal key shared by the three APIs and prints the website address:

```sh
./run.sh
```

The services create their SQLite tables on startup. For separate terminals, set the same strong `INTERNAL_KEY` value in all three service environments before launching them. The website uses the Booking port printed by `run.sh`; `HOTEL_BOOKING_API` can be set in the page for a different API address.


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

An identical successful create replay returns 200 with the current Booking; a pending attempt remains 202; a rejected attempt repeats its saved error. This guide documents the current routes and data shapes; it does not assert that a complete automated API test suite has passed.
