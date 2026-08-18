# Admin Web BFF deployment

The React admin application is hosted by FUE, while this backend remains the
private BFF. FUE gateway authentication must protect every production request
to `/admin-api`; CORS is a browser compatibility control, not authentication.

Enable the BFF only in a controlled deployment environment with
`TRIPGUARD_ADMIN_API_ENABLED`. Configure the allowed FUE browser origins with
`TRIPGUARD_ADMIN_ALLOWED_ORIGINS`.

The following POI integration settings are private server-side deployment
values. Never place them in FUE static-app variables, browser bundles, client
requests, logs, or API responses:

- `TRIPGUARD_CRAWLAB_RESULTS_API_URL`
- `TRIPGUARD_CRAWLAB_API_TOKEN`
- `TRIPGUARD_TENCENT_LOCATION_API_KEY`
- `TRIPGUARD_TENCENT_LOCATION_BASE_URL`

The FUE static application may configure its backend base URL through
`VITE_ADMIN_API_BASE_URL`, but it must never receive any of the private POI
settings above.
