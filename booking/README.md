# Booking service: current progress

Implemented: POST /api/register, POST /api/login, GET /api/bookings, GET /api/rooms and GET /health.
Room search forwards HTTP requests to Availability. Create/view/cancel booking, reservation/notification calls and retry workers are still pending. Room search has been tested with a temporary HTTP stub; the teammate's real Availability service has not been integrated yet.
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
