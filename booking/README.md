# Booking service: current progress

Implemented: POST /api/register, POST /api/login, GET /api/bookings, GET /api/bookings/{booking_id}, POST /api/bookings, GET /api/rooms, POST /api/bookings/{booking_id}/cancel and GET /health.
Room search, reservation creation/recovery and confirmation notifications call the other services over HTTP. Cancellation releases the reservation and retries safely after outages. Integration has been tested with temporary HTTP stubs; the teammates' real services have not been integrated yet.
Follow ../api-guide.md for the full shared contract.

## Run from the repository root

```powershell
python -m pip install -r booking/requirements.txt
python -m flask --app booking.app run --port 5001
```

Restart the service after editing code. SQLite tables are created automatically without deleting existing bookings. No fixed customer accounts or tokens are created. Register your own test users. Local database files are ignored by Git.

Optional environment variables: BOOKING_DATABASE (alternate database file), WEBSITE_ORIGIN (default http://localhost:3000). The database parent folder must already exist. Room search also uses AVAILABILITY_URL (default http://localhost:5002) and requires INTERNAL_KEY. Only Booking and the other backends should know this key.

## Try the account flow

```powershell
$credentials = @{ username = 'alice'; password = 'ExamplePass123' } | ConvertTo-Json
Invoke-RestMethod http://localhost:5001/api/register -Method Post -ContentType 'application/json' -Body $credentials
$login = Invoke-RestMethod http://localhost:5001/api/login -Method Post -ContentType 'application/json' -Body $credentials
Invoke-RestMethod http://localhost:5001/api/bookings -Headers @{ 'X-Customer-Token' = $login.token }
```

These credentials are examples for local tests. Duplicate registration returns 409; log in instead. Do not publish real passwords or tokens. Passwords and tokens are hashed in storage. Sessions expire after two hours. The website should keep its token in memory; closing/reloading the page requires login again. A client-side sign-out clears the browser token but does not revoke server sessions. Use only local demo data; this version does not include production login rate limiting or password recovery.

## Test

```powershell
python -m unittest discover -s tests -v
```

Tests use a temporary database, not booking/booking.db.

## Connect room search to Availability

Before starting Flask, set these in the same terminal:

```powershell
$env:AVAILABILITY_URL = "http://localhost:5002"
$env:INTERNAL_KEY = "replace-with-your-teams-shared-secret"
python -m flask --app booking.app run --port 5001
```

The key above is a placeholder. Agree on a private key with the Availability teammate and configure the same value in both services. Do not commit it. Restart Flask after changing environment variables. Room search sends X-Internal-Key to /internal/rooms and never forwards customer tokens. Requests use a finite three-second connect/read timeout and do not follow redirects.

After logging in using the earlier example, search from the test terminal:

```powershell
Invoke-RestMethod `
    -Uri "http://localhost:5001/api/rooms?check_in=2026-11-10&check_out=2026-11-12&guest_count=2" `
    -Headers @{ "X-Customer-Token" = $login.token }
```

No configured key, an offline Availability service, timeout or invalid upstream response produces 503 SERVICE_UNAVAILABLE. A successful search with no matching rooms produces 200 with an empty rooms array. Bad dates/guest count produce 400; no valid login produces 401. Searching does not reserve rooms or change the booking database.

The room-search tests start and stop a temporary HTTP stub automatically. They test the Booking integration contract, not the teammate's room-inventory or double-booking logic.

## Create and view a booking

Set NOTIFICATION_URL (default http://localhost:5003) along with the earlier Availability settings before starting Flask. Notification uses the same INTERNAL_KEY agreed with the team.

After logging in, run this in the test terminal:

```powershell
$requestKey = [guid]::NewGuid().ToString()
$bookingBody = @{ room_id = 'R101'; check_in = '2026-11-10'; check_out = '2026-11-12'; guest_count = 2 } | ConvertTo-Json
$booking = Invoke-RestMethod `
    -Uri 'http://localhost:5001/api/bookings' `
    -Method Post -ContentType 'application/json' `
    -Headers @{ 'X-Customer-Token' = $login.token; 'Idempotency-Key' = $requestKey } `
    -Body $bookingBody
Invoke-RestMethod `
    -Uri "http://localhost:5001/api/bookings/$($booking.booking_id)" `
    -Headers @{ 'X-Customer-Token' = $login.token }
```

Reuse the same request key and body if retrying the same attempt. Use a new key only for a new attempt. Booking saves one order per customer/key in a transaction before calling Availability. The same key with changed input returns 409 IDEMPOTENCY_CONFLICT. Definite rejection is saved and replayed. IDs and prices come from the backend, never the browser.

201 means confirmed; 202 means pending. If Availability is offline or its response is lost, the order stays pending. A recovery worker checks the original booking ID, then confirms, rejects a released ID, or retries reservation with the same ID after 404. Confirmation and a pending notification event are saved in one local transaction. Notification failures never undo a confirmed booking. Retries reuse the saved event ID.

The automatic worker starts on the first HTTP request, runs every five seconds and survives outages using saved database records. Restarting the service does not erase pending work. Each HTTP call has finite connect/read timeouts; dependency errors leave work pending. A permanent internal-key/API error requires fixing the configuration; retrying alone cannot fix it. In tests the automatic worker is disabled. Set BOOKING_RETRY_WORKER=0 to disable automatic recovery, or run one recovery pass manually:

```powershell
python -m flask --app booking.app retry-pending
```

The Booking cancellation endpoint is implemented. Do not treat this as the whole distributed system being complete. The temporary services in tests only verify Booking's communication and recovery behavior; the Availability teammate must implement and test real inventory concurrency and room pricing.

## Cancel a booking

After creating a confirmed booking:

```powershell
Invoke-RestMethod `
    -Uri "http://localhost:5001/api/bookings/$($booking.booking_id)/cancel" `
    -Method Post -Headers @{ 'X-Customer-Token' = $login.token }
```

Send no body. Only the owner may cancel; missing or another customer's booking returns 404. A pending booking returns 409 BOOKING_STATE_CONFLICT. A confirmed booking first becomes cancelling, then Booking asks Availability to release it. Only a matching 200 released response changes the order to cancelled. An outage, lost reply or invalid response leaves it cancelling (202); the existing recovery worker retries the same release URL. No order is deleted, and price/reservation details stay intact.

Repeated cancelled/rejected cancellations return 200 with the unchanged order. A cancellation already in progress returns 202. Cancelled booking IDs are never reused for a new reservation; Availability must keep released-ID tombstones as defined in api-guide.md. A previously queued confirmation notification remains a historical event; the order's current state must always be read from Booking.
