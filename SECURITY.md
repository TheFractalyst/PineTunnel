# Security Policy

## Supported Versions

Only the latest release receives security updates. Older versions should upgrade to the latest release.

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public issue.

Email: contact@pinetunnel.com with details of the vulnerability. You will receive a response within 48 hours.

Include in your report:
- Description of the vulnerability and its impact
- Steps to reproduce (proof of concept)
- Affected versions/configurations
- Any suggested mitigations

We will acknowledge your report within 48 hours and credit you in the fix announcement unless you prefer to remain anonymous.

## Security Architecture

- **HMAC-SHA256** on all EA communication with `hmac.compare_digest()` (constant-time) to prevent timing attacks
- **Webhook secret** verification on all TradingView alert endpoints
- **Session-based auth** with server-side tokens (secrets.token_urlsafe, 1h TTL, Redis-backed in multi-worker)
- **Admin API key** for admin-only routes with rotation support
- **Rate limiting** - Redis-based sliding window rate limiter on webhook endpoints
- **IP validation** - Cloudflare IP allowlist middleware for webhook endpoints
- **Request validation** - Size limits and content-type enforcement on all endpoints
- **Security headers** - X-Content-Type-Options, X-Frame-Options, Referrer-Policy on all responses
- **CORS** - Restricted to known origins (deny-by-default, never `["*"]` with credentials)
- **Replay protection** - Timestamp skew check (5-minute window) on signed requests
- **Failed attempt tracking** - Redis-backed, 10-failure threshold, 1-hour IP block

## Best Practices for Deployment

- Set all environment variables via your hosting platform (never commit `.env`)
- Use a strong `WEBHOOK_SECRET` (32+ characters)
- Use a strong `JWT_SECRET` (32+ characters)
- Restrict `TELEGRAM_ADMIN_IDS` to your own Telegram user ID
- Enable Cloudflare IP allowlist in production
- Run behind a reverse proxy (Cloudflare, nginx, etc.) for TLS termination
- Configure `SECURITY_EMAIL` and `SECURITY_URL` env vars to enable `/.well-known/security.txt` discovery

## Scope and Safe Harbor

The following are in scope for security testing:
- The PineTunnel server codebase in this repository
- Your own VPS deployment of PineTunnel

The following are out of scope:
- Production deployments you do not own
- Social engineering of project maintainers
- Denial-of-service testing against any deployment
- Attempting to access other users' data

If you act in good faith and stay within scope, we will not pursue legal action regarding your security testing.
