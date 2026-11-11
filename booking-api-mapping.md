# Booking API Mapping

A short guide for connecting the Client UI to Booking.
Base URL: `http://localhost:5001`.

This describes the current local Booking implementation. Real-service integration is still to be tested.
Search uses check-in date, check-out date and room type. API values: `single` (Single), `double` (Double), `king_suite` (King Suite). Availability owns the sample inventory: three rooms of each type. These values must also be supported by the Client and Availability services.

## 1. Search rooms

`GET /api/rooms?check_in=2026-11-15&check_out=2026-11-17&room_type=double`

No request body. Booking forwards these parameters to Availability.

200 response (room values below are examples):

```json
{"rooms": [{"room_id": "R101", "room_number": "101", "room_type": "double", "capacity": 2}]}
```

- No rooms available: `200` with `{"rooms": []}`. Show "No rooms available."
- Availability cannot be reached: `503`. Show "Unable to search rooms. Please try again."
- Searching does not reserve a room; creation can still return a conflict.

## 2. Create a booking

`POST /api/bookings`

Headers: `Content-Type: application/json` and `Idempotency-Key: <UUID>`.
The UI can generate a key with `crypto.randomUUID()`. Keep the same key and body when retrying the same booking; use a new key for a different booking attempt.

Request:

```json
{
  "guest_name": "Alex Tan",
  "room_id": "R101",
  "check_in": "2026-11-15",
  "check_out": "2026-11-17",
  "guest_count": 2
}
```

201 response (example ID):

```json
{
  "booking_id": "B001",
  "guest_name": "Alex Tan",
  "room_id": "R101",
  "check_in": "2026-11-15",
  "check_out": "2026-11-17",
  "guest_count": 2,
  "status": "confirmed",
  "notification_status": "sent",
  "created_at": "2026-11-15T08:00:00Z",
  "updated_at": "2026-11-15T08:00:00Z"
}
```

| Result | UI action |
| --- | --- |
| 201, confirmed | Show confirmation; save booking_id |
| 200 on identical retry of a confirmed/cancelled booking | Show the returned current status; do not create another order |
| 409, ROOM_UNAVAILABLE | Show "Room unavailable. Please choose another room." |
| 202, pending | Save booking_id; show "Booking is being processed." |
| 409, IDEMPOTENCY_CONFLICT | The key was reused with different details; do not silently retry |

If Notification is down, the booking can remain confirmed with `notification_status: "pending"`. Booking retries notification delivery; the UI does not need a notification API call.

## 3. View a booking

`GET /api/bookings/{booking_id}`

No request body. Returns `200` with the same full Booking object shown above, including its current status, or `404` if not found.

The UI can keep booking IDs in its browser session and let the guest enter an ID. Booking does not identify a user by name or receive a session ID. There is no login or ownership check in this demo.

## 4. Cancel a booking

`POST /api/bookings/{booking_id}/cancel`

No request body. Booking asks Availability to release the room.

| Result | UI action |
| --- | --- |
| 200, cancelled | Show "Booking cancelled." The response is the full Booking object |
| 202, cancelling | Show "Cancellation is being processed." The response is the full Booking object |
| 404, NOT_FOUND | Show "Booking not found." |
| 409, BOOKING_STATE_CONFLICT | Pending or rejected bookings cannot be cancelled |

Repeating cancellation of an already cancelled booking returns `200` without changing it.

## Common rules

- Names must be non-empty; dates must be real YYYY-MM-DD with check-out after check-in; guest count must be a positive integer. Availability checks room capacity.
- Invalid input returns `400`; non-JSON creation returns `415`.
- For pending/cancelling, query the View API every two seconds for up to 30 seconds, then offer manual refresh. A timeout is not proof that the booking failed.
- If a create response is lost, retry with the original key and details. Do not generate another key automatically.
- No login token or internal API key is required by the simplified local Booking version. Teammate services must also accept calls without an internal key.

Errors use this format; booking_id is included only when an associated order exists:

```json
{"error": {"code": "ROOM_UNAVAILABLE", "message": "Room unavailable for these dates."}, "booking_id": "B001"}
```

## Calls made by Booking

| Purpose | Service API |
| --- | --- |
| Search | Availability: GET /internal/rooms |
| Reserve | Availability: POST /internal/reservations |
| Check after a lost reservation reply | Availability: GET /internal/reservations/{booking_id} |
| Release | Availability: POST /internal/reservations/{booking_id}/release |
| Record confirmation | Notification: POST /internal/notifications |

The UI calls only Booking. Backend request/response details remain in [api-guide.md](api-guide.md).
