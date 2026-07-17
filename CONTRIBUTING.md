# Contributing to PineTunnel

Thanks for your interest in contributing! This is an open-source project, and all contributions are welcome.

## Getting Started

**Prerequisites:** Python 3.13+, Redis 6+

```bash
git clone https://github.com/TheFractalyst/PineTunnel.git
cd PineTunnel

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Edit secrets (WEBHOOK_SECRET, JWT_SECRET, ADMIN_API_KEY)

# Redis is required for WebSocket state and rate limiting
# Install: brew install redis && redis-server  (macOS)

alembic upgrade head  # Create database tables
```

## Development

```bash
# Run the server
uvicorn apps.server.main:app --reload --host 127.0.0.1 --port 8000

# Format and type check before submitting
black . && isort . && mypy apps/
```

## Code Style

- Python 3.13+ with type hints throughout
- Async/await for all I/O operations
- Pydantic for request/response models
- SQLAlchemy 2.0 with raw SQL via `text()` and Alembic migrations
- Logging via `logging.getLogger(__name__)` (no print statements in production code)
- **Black** for formatting (line-length 100, `pyproject.toml [tool.black]`)
- **isort** for import ordering (profile="black", `pyproject.toml [tool.isort]`)
- **mypy** strict mode for type checking (`pyproject.toml [tool.mypy]`)

## Pull Request Process

1. Fork the repo and create a feature branch from `main`
2. Run `black . && isort . && mypy apps/` to ensure code passes formatting and type checks
3. Keep commits focused with clear messages
4. Open a PR with a description of what changed and why

## Project Structure

```
apps/server/     FastAPI backend
  routes/        Route modules (webhook, auth, admin, signals, ea_download, etc.)
  services/      Business logic (MT4/MT5, risk, rate limiting, Telegram bot)
  middleware/    Security, rate limiting, IP validation, request validation
  ws/            WebSocket connection management (Redis-backed)
  webhook/       Signal parser, queue, executor pipeline
  db/            SQLite + PostgreSQL adapters (shared base class)
  config/        Pydantic settings, lifespan, health checks
  utils/         Security (HMAC, IP validation), logging helpers
  models/        Pydantic request/response schemas
apps/ea/         MetaTrader Expert Advisor (MQL5/MQL4 + C++ DLL)
  mt5/           MT5 EA (.mq5) + MQL includes
  mt4/           MT4 EA (.mq4) + MQL includes
  dll/           C++ WebSocket client (CMake, x64 + Win32)
  pine/          PineScript webhook sender (alert format helper)
alembic/         Database migrations (7 versions)
scripts/         CI verification scripts
.github/         CI/CD workflows (DLL + EA compilation)
```

## Reporting Issues

Please [search existing issues](https://github.com/TheFractalyst/PineTunnel/issues?q=is%3Aissue) before creating a new one to avoid duplicates. Include:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (OS, Python version, MT4/MT5 version)

**Security vulnerabilities:** Do not use GitHub Issues. See [SECURITY.md](SECURITY.md) for the vulnerability reporting process.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
