# Security deployment checklist

This document summarizes controls added for the API security review and what operators should configure in production.

## Secrets and HTTPS

- Set a stable **`AUTH_SECRET_KEY`** so session cookies survive restarts and cannot be forged across deploys.
- Serve the API over **HTTPS** only in production.
- Set **`SESSION_COOKIE_HTTPS_ONLY=true`** when all clients use HTTPS.
- Set **`SESSION_COOKIE_SAME_SITE`** to `lax` (default) or `strict` if you do not need cross-site cookie flows.

## Reverse proxy and `X-Forwarded-*`

- **`PROXY_HEADERS_TRUSTED_HOSTS`**: comma-separated hostnames or IPs of your reverse proxy (nginx, ingress, etc.) that append `X-Forwarded-Proto` / `X-Forwarded-For`. Starlette’s `ProxyHeadersMiddleware` only trusts these hosts. Use `*` only in development.
- **`TRUST_X_FORWARDED_FOR_CLIENT_IP`**: set to **`true`** only when the **first** hop to the API is always your trusted proxy and that proxy **replaces** or correctly manages `X-Forwarded-For` (clients must not be able to inject an arbitrary client IP). If unsure, leave **`false`** so login rate limiting uses the direct TCP peer address.

Recommended nginx pattern: set `X-Forwarded-For` to the real client (e.g. `proxy_set_header X-Forwarded-For $remote_addr;`) or append in a controlled way; do not forward raw client-supplied `X-Forwarded-For` unchanged from the public internet to the app when `TRUST_X_FORWARDED_FOR_CLIENT_IP=true`.

## OpenAPI / docs

- Set **`EXPOSE_OPENAPI_DOCS=false`** in production to disable `/docs`, `/redoc`, and `/openapi.json`.

## Raster COG paths

- **`RASTER_STORAGE_PATH`** must be the only writable/readable tree intended for COG files. Stored `properties.raster.cog_path` values are resolved and must stay under this directory (symlinks resolved; paths outside the root are rejected).

## Titiler internal access

- **`TITILER_INTERNAL_SECRET`**: shared secret between the API and Titiler (or nginx in front of Titiler). The API can send this value in the **`X-GeoFast-Internal-Token`** header for `/internal/...` fetches; query `token=` remains supported for compatibility. Prefer headers in new integrations so access logs are less likely to capture the secret.
- Restrict network access so only the API (and Titiler sidecar) can reach internal COG fetch URLs.

## STAC catalog URLs

- Admin-registered **`stac_api_root_url`** values are validated to block obvious SSRF targets (e.g. localhost, literal private IPs). Optional: set **`STAC_CATALOG_ROOT_URL_REQUIRE_HTTPS=true`** to require `https` for new/updated catalogs.

## Observability “servers JSON”

- Netdata (or compatible) **`base_url`** entries in admin observability settings are validated with the same outbound rules before save and before HTTP fetch.

## Admin HTML forms

- Destructive / state-changing admin observability forms include a **CSRF** token tied to the session. Ensure sessions work (cookie domain/path) if you use multiple subdomains.

## Rate limiting

- Login attempts are rate-limited per IP (Redis when configured, else in-process).
- Change-password attempts with a wrong current password are additionally throttled per IP in-process across workers.

For heavy public endpoints, consider an edge WAF or API gateway rate limits as a second layer.
