# Hotel Booking: Shared API Guide v2.0

10 November 2026 â€” simplified team contract.

**Implementation status:** The Booking APIs below match the current Booking code and have been tested with temporary HTTP services. Availability and Notification endpoints describe the expected integration contract; their real implementations and Client integration still need to be checked with teammates.

## 1. Connections and scope

Client (website, port 3000) â†’ Booking (5001) â†’ Availability (5002) / Notification (5003).

- No registration, login, customer tokens, payments or prices.
- Enter `guest_name` when booking; use `booking_id` to view or cancel.
- There is no authenticated ownership check: anyone knowing an ID can view or cancel that booking. Use demo guest data and do not publish a list of all bookings.
- Each backend owns its storage. Other services access it only through APIs.

### Complete API list: 12 operations

| Owner | Method and path | Purpose |
| --- | --- | --- |
| Booking | GET /api/rooms | Search rooms |
| Booking | POST /api/bookings | Create booking |
| Booking | GET /api/bookings/{booking_id} | View booking |
| Booking | POST /api/bookings/{booking_id}/cancel | Cancel booking |
| Availability | GET /internal/rooms | Search rooms |
| Availability | POST /internal/reservations | Reserve nights |
| Availability | GET /internal/reservations/{booking_id} | Recover after lost replies |
| Availability | POST /internal/reservations/{booking_id}/release | Release nights |
| Notification | POST /internal/notifications | Record confirmation |
| Each backend | GET /health (3 operations) | Check service and own storage |

### Headers

- JSON body: `Content-Type: application/json`.
- Create booking: also send `Idempotency-Key: <UUID>`. One UUID per new attempt; reuse it for retries of the same attempt.
- Backend calls need no internal API key in this local demo.
- No customer token or internal API key is needed.
- GET, cancel and release requests have no body.

## 2. Client â†’ Booking

### Search

`GET /api/rooms?check_in=2026-11-15&check_out=2026-11-17&room_type=double`

Search requires `check_in`, `check_out` and `room_type` (`single`, `double` or `king_suite`); it does not take guest_count. Booking forwards the same parameters to `GET /internal/rooms`. Both return **200** with this format:

```json
{"rooms": [{"room_id": "R101", "room_number": "101", "room_type": "double", "capacity": 2}]}
```

No match: `200 {"rooms": []}`. Search does not reserve rooms. Availability unavailable â†’ 503, not an empty list.

### Create

`POST /api/bookings`, with Idempotency-Key and exactly:

```json
{"guest_name": "Alex Tan", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17", "guest_count": 2}
```

**201** = new confirmed booking; **202** = pending. Both return the full Booking object:

```json
{
  "booking_id": "B001",
  "guest_name": "Alex Tan",
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

Booking generates an unpredictable booking ID. Keys are globally unique because there are no accounts. Save the key, original normalized input and booking together in one DB transaction. Compare field values, not JSON spacing/order.

Same key + same input â†’ existing booking: 202 if pending/cancelling, 200 if confirmed/cancelled; rejected attempts repeat their saved error. Same key + changed input â†’ 409 IDEMPOTENCY_CONFLICT.

### View and cancel

- `GET /api/bookings/{booking_id}` â†’ **200**, full Booking object, including rejected orders.
- `POST /api/bookings/{booking_id}/cancel` â†’ **200**, full object with status cancelled; or **202**, status cancelling while release is unresolved.
- Unknown booking â†’ 404. Repeat cancelled cancellation â†’ 200 unchanged; repeat cancelling â†’ 202; cancel pending/rejected â†’ 409 BOOKING_STATE_CONFLICT.
- No booking-list endpoint. Client keeps the returned ID and also lets a guest enter an ID to retrieve a booking.

### Client feedback

| Result | Display |
| --- | --- |
| confirmed | Confirmation and booking ID |
| 409 ROOM_UNAVAILABLE | Room unavailable |
| pending | Booking is being processed |
| cancelling | Cancellation is being processed |
| cancelled | Cancellation confirmed |
| rejected | Booking rejected |
| 503 / network error | Unable to get result; preserve existing ID/request key |

Poll pending/cancelling bookings with GET every 2 seconds, for up to 30 seconds. Stop when resolved; otherwise offer manual refresh. If the create response is lost, retry POST with the original key and input to retrieve the same order. Do not automatically create a new attempt.

## 3. Booking â†’ Availability

No internal API key is required. Availability owns inventory.

### Reserve

`POST /internal/reservations`

```json
{"booking_id": "B001", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17", "guest_count": 2}
```

**201** for new reservation; **200** for identical retry:

```json
{"booking_id": "B001", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17", "guest_count": 2, "status": "reserved"}
```

Overlap â†’ 409 ROOM_UNAVAILABLE. Changed input with same ID â†’ 409 IDEMPOTENCY_CONFLICT. Released ID â†’ 409 RESERVATION_RELEASED. Unknown room: 404 NOT_FOUND. Guests above room capacity: 400 VALIDATION_ERROR.

Error replies must include the matching error code, for example `{"error":{"code":"VALIDATION_ERROR","message":"Guest count exceeds room capacity."}}`. Booking checks both the HTTP status and error code before rejecting an order; an unexpected reply leaves it pending for recovery.

Insert reservation and all occupied nights in one transaction, using UNIQUE(room_id, stay_date). Any conflict rolls back the entire stay. Release nights and mark released together too.

### Check after a lost reply

`GET /internal/reservations/{booking_id}` â†’ **200**, full Reservation object above, status reserved or released. Unknown ID â†’ 404.

Booking saves pending before calling reserve. Timeout means the outcome is unknown: return 202 pending. The background worker checks with the same booking ID: reserved â†’ confirm; 404 â†’ retry reserve using original input; released â†’ reject. Definite reservation rejections are saved as rejected; temporary failures remain pending.

### Release

`POST /internal/reservations/{booking_id}/release` â†’ **200**, including repeated requests:

```json
{"booking_id": "B001", "status": "released"}
```

Retain released IDs so delayed reserve calls cannot reuse them. If release arrives before reserve, save the ID as released anyway. GET then returns the full Reservation object with room_id, check_in, check_out and guest_count null. For previously reserved IDs, preserve their original details.

Booking saves cancelling before release, then cancelled only after a matching release acknowledgement. Failure/timeout â†’ 202 cancelling and background retry using the same ID. Keep the order record.

## 4. Booking â†’ Notification

`POST /internal/notifications`:

```json
{"event_id": "E001", "event_type": "booking.confirmed", "booking_id": "B001", "guest_name": "Alex Tan", "room_id": "R101", "check_in": "2026-11-15", "check_out": "2026-11-17"}
```

Save before replying: **201** first time; **200** for an identical retry:

```json
{"event_id": "E001", "status": "recorded"}
```

Same event ID + changed data â†’ 409 IDEMPOTENCY_CONFLICT. Deduplicate persistently by event_id, including after restart. A database or durable log is acceptable if it supports these rules. No email/SMS.

Booking saves confirmation and a pending event in one local transaction. It may try sending before replying to Client; notification failure must not undo confirmation. Retry the saved event with the same ID. Only a matching 200/201 acknowledgement marks notification_status sent. Timeout/5xx stays pending; fix request/schema problems causing 4xx.

A notification records confirmation history; Booking is the source of current order status.

## 5. Shared rules and errors

| Item | Rule |
| --- | --- |
| JSON | snake_case; example request fields required; reject unknown fields, nulls and malformed JSON |
| guest_name | Non-empty string, trimmed; allow non-ASCII names |
| IDs | Strings, 1â€“64 letters/digits/underscores/hyphens; do not depend on example formats |
| Dates | Real YYYY-MM-DD; check-in before check-out; past demo dates allowed |
| Nights | Check-in included, check-out excluded: 15â€“17 November occupies nights 15 and 16 |
| room_type | Search: single, double or king_suite; Availability owns three sample rooms of each type |
| guest_count | Positive integer, not boolean/string; Availability checks room capacity |
| Timestamps | Server-generated UTC ending Z |
| Booking states | pending, confirmed, rejected, cancelling, cancelled |
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
| 400 | VALIDATION_ERROR |
| 404 / 405 | NOT_FOUND / METHOD_NOT_ALLOWED |
| 409 | ROOM_UNAVAILABLE, IDEMPOTENCY_CONFLICT, RESERVATION_RELEASED, BOOKING_STATE_CONFLICT |
| 413 / 415 | VALIDATION_ERROR (body over 16 KiB) / UNSUPPORTED_MEDIA_TYPE |
| 500 / 503 | INTERNAL_ERROR / SERVICE_UNAVAILABLE |

### Health

`GET /health` on each backend, no key/body. **200** when service and its own storage work:

```json
{"service": "booking", "status": "ok"}
```

Use availability / notification for other services. Own storage unavailable â†’ 503 with status error. Booking can be healthy while Notification is stopped.

## 6. Integration checks

- Successful create/view/cancel; released nights can be booked again.
- Concurrent overlapping requests: only one reservation succeeds.
- Repeat create/reserve/release/notification: no unintended duplicates.
- Lost Availability reply: pending order recovers without another reservation.
- Notification outage/restart: booking remains confirmed; event recorded once.
- Release outage: cancelling recovers to cancelled; old reserve replies cannot restore it.
- Invalid inputs are rejected with clear error responses.
- Test with real Client and teammate services; label mock-service evidence separately.

### Shared report changes needed

Remove accounts/login from 2.2; include pending/cancelling and 202 recovery in 2.5/4.2/4.3/4.5; add reservation-check API. Do not describe an Availability timeout as definite rejection. Align Notification storage with its implementation. Table names are internal choices, not API requirements.
