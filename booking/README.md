# Booking service: current progress

Implemented: POST /api/register, POST /api/login, GET /api/bookings and GET /health.
Room search, create/view/cancel booking, service calls and retry workers are still pending.
Follow ../api-guide.md for the full shared contract.

## Run from the repository root

```powershell
python -m pip install -r booking/requirements.txt
python -m flask --app booking.app run --port 5001
```

Restart the service after editing code. SQLite tables are created automatically without deleting existing bookings. No fixed customer accounts or tokens are created. Register your own test users. Local database files are ignored by Git.

Optional environment variables: BOOKING_DATABASE (alternate database file), WEBSITE_ORIGIN (default http://localhost:3000). The database parent folder must already exist.

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
python -m unittest discover -s tests -p test_auth.py -v
```

Tests use a temporary database, not booking/booking.db.
