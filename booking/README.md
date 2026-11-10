# Booking service

Implements the simplified v2 API in ../api-guide.md: room search, create, view, cancel and health. No registration/login, customer tokens, booking-list endpoint or pricing.

Booking calls Availability and Notification over HTTP. Local tests use temporary HTTP services; real teammate services still need integration.

## Run

From the repository root:

```powershell
python -m pip install -r booking/requirements.txt
$env:INTERNAL_KEY = "replace-with-your-teams-shared-secret"
python -m flask --app booking.app run --port 5001
```

Configure the same private key in Availability and Notification; do not commit it or put it in the website. Restart Flask after changing code or configuration.

| Setting | Default |
| --- | --- |
| BOOKING_DATABASE | booking/booking-v2.db (absolute path resolved by the app) |
| WEBSITE_ORIGIN | http://localhost:3000 |
| AVAILABILITY_URL | http://localhost:5002 |
| NOTIFICATION_URL | http://localhost:5003 |
| BOOKING_RETRY_WORKER | 1 (enabled) |

The database parent directory must exist. The new default database keeps the earlier booking.db and account data untouched. Old orders are not copied into the simplified demo. If BOOKING_DATABASE points at an old account-based database, choose a new file; the app refuses to alter that schema. Local databases are ignored by Git.

## Try from another PowerShell terminal

Health and search:

```powershell
Invoke-RestMethod http://localhost:5001/health
Invoke-RestMethod "http://localhost:5001/api/rooms?check_in=2026-11-15&check_out=2026-11-17&guest_count=2"
```

Create and view:

```powershell
$requestKey = [guid]::NewGuid().ToString()
$bookingBody = @{
    guest_name = 'Alex Tan'
    room_id = 'R101'
    check_in = '2026-11-15'
    check_out = '2026-11-17'
    guest_count = 2
} | ConvertTo-Json

$booking = Invoke-RestMethod -Uri 'http://localhost:5001/api/bookings' -Method Post -ContentType 'application/json' -Headers @{ 'Idempotency-Key' = $requestKey } -Body $bookingBody
Invoke-RestMethod "http://localhost:5001/api/bookings/$($booking.booking_id)"
```

Reuse the exact key and input to retry the same attempt. New keys mean new attempts. Guest names are trimmed and bounded to 1–100 characters. Booking generates the booking ID.

Create returns 201 confirmed, 202 pending, or a saved rejection. No key, offline Availability or an unknown reserve outcome leaves a valid new order pending. Search returns 503 when Availability cannot be queried, and 200 with an empty list only when the service reports no matches.

Cancel a confirmed booking, with no body:

```powershell
Invoke-RestMethod -Uri "http://localhost:5001/api/bookings/$($booking.booking_id)/cancel" -Method Post
```

Returns 200 cancelled after release succeeds, or 202 cancelling until recovery. Repeated cancelled requests return the same order. Pending/rejected bookings cannot be cancelled (409). Orders are retained.

## Recovery

- Pending order and request key are saved before asking Availability.
- The worker checks reservation status with the same booking ID after lost replies.
- Confirmation and a pending notification are saved in one transaction.
- Notification delivery is attempted before returning a new confirmation; failure never undoes confirmation. Retry uses the same event ID.
- Cancellation is saved as cancelling before release; only a matching acknowledgement marks cancelled.
- Late reservation replies cannot change cancelling/cancelled back to confirmed.

Automatic recovery starts on the first request and revisits saved work every five seconds. Service restarts retain pending work. HTTP calls use finite connect/read timeouts and do not follow redirects. Fix configuration/schema errors rather than relying on retries to fix them.

Disable the automatic worker with BOOKING_RETRY_WORKER=0, or manually run one pass:

```powershell
python -m flask --app booking.app retry-pending
```

Client should query pending/cancelling orders every two seconds for up to 30 seconds, then offer manual refresh.

## Tests and scope

```powershell
python -m unittest discover -s tests -v
```

Tests use isolated temporary databases and HTTP stubs. They cover simplified field validation, no-login APIs, request deduplication, concurrency, lost replies, cancellation, notification outages and restart recovery. They do not prove the teammate's inventory implementation prevents conflicting bookings; test that with real services during integration.

No authenticated ownership enforcement: anyone knowing an ID can view or cancel that booking. Use demo data and do not expose a public booking list. Input validation, parameterized SQL, internal keys, CORS and bounded request bodies remain enabled.
