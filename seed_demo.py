"""Seed demo data for dashboard exploration."""
import json
import os
import random
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "pinetunnel.db"
LICENSES_FILE = "licenses.json"

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD"]
ACTIONS = ["buy", "sell", "close_long", "close_short", "close_all"]

DEMO_LICENSES = [
    {"license_key": "demo_abc123def456", "secret_key": "secret_xyz789abc012", "email": "alice@example.com", "name": "Alice Cooper", "status": "active", "enabled": True, "expires_at": (datetime.now() + timedelta(days=90)).isoformat(), "features": ["unlimited_symbols"], "allowed_symbols": [], "last_activity": datetime.now().isoformat()},
    {"license_key": "demo_def789ghi012", "secret_key": "secret_yza234bcd567", "email": "bob@example.com", "name": "Bob Marley", "status": "active", "enabled": True, "expires_at": (datetime.now() + timedelta(days=30)).isoformat(), "features": [], "allowed_symbols": ["EURUSD", "GBPUSD"], "last_activity": datetime.now().isoformat()},
    {"license_key": "demo_ghi012jkl345", "secret_key": "secret_zab567cde890", "email": "carol@example.com", "name": "Carol King", "status": "active", "enabled": True, "expires_at": (datetime.now() + timedelta(days=365)).isoformat(), "features": ["unlimited_symbols"], "allowed_symbols": [], "last_activity": datetime.now().isoformat()},
    {"license_key": "demo_jkl345mno678", "secret_key": "secret_abc901efg234", "email": "dave@example.com", "name": "Dave Grohl", "status": "disabled", "enabled": False, "expires_at": (datetime.now() - timedelta(days=5)).isoformat(), "features": [], "allowed_symbols": [], "last_activity": (datetime.now() - timedelta(days=5)).isoformat()},
    {"license_key": "demo_mno678pqr901", "secret_key": "secret_bcd123fgh456", "email": "eve@example.com", "name": " Eve Stone", "status": "active", "enabled": True, "expires_at": (datetime.now() + timedelta(days=180)).isoformat(), "features": [], "allowed_symbols": ["XAUUSD"], "last_activity": datetime.now().isoformat()},
]


def seed_licenses():
    data = {}
    for lic in DEMO_LICENSES:
        data[lic["license_key"]] = dict(lic)
    with open(LICENSES_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Seeded {len(data)} licenses to {LICENSES_FILE}")


def seed_trades():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now()
    trades = []
    for i in range(80):
        lic = random.choice(DEMO_LICENSES[:3])
        sym = random.choice(SYMBOLS)
        action = random.choice(ACTIONS)
        ts = now - timedelta(hours=random.randint(0, 168))
        is_win = random.random() > 0.45
        profit = round(random.uniform(5, 120) if is_win else -random.uniform(5, 80), 2)
        volume = round(random.choice([0.01, 0.05, 0.10, 0.20, 0.50, 1.00]), 2)
        price = round(random.uniform(1.05, 1.15), 5)
        status = "success" if random.random() > 0.08 else "failed"
        trades.append(
            (lic["license_key"], ts.strftime("%Y-%m-%d %H:%M:%S"), sym, action, volume, price, profit, status, f"Trade via {sym}", random.randint(100, 999))
        )
    c.executemany(
        "INSERT INTO trades (license_key, timestamp, symbol, action, volume, price, profit, status, comment, magic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        trades,
    )
    conn.commit()
    print(f"Seeded {len(trades)} trades to {DB_PATH}")
    conn.close()


def seed_audit():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now()
    actions = ["login", "add_license", "update_config", "delete_license", "test_webhook", "enable_license", "disable_license", "rotate_secret"]
    for lic in DEMO_LICENSES[:3]:
        ts = now - timedelta(hours=random.randint(0, 72))
        action = random.choice(actions)
        try:
            c.execute("INSERT OR REPLACE INTO ea_audit (license_key, data, updated_at) VALUES (?, ?, ?)",
                      (lic["license_key"], json.dumps({"action": action, "user": "admin", "ip": "127.0.0.1", "detail": f"Demo {action}"}), ts.strftime("%Y-%m-%d %H:%M:%S")))
        except Exception as e:
            pass
    conn.commit()
    print(f"Seeded 3 audit entries")
    conn.close()


def seed_alerts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now()
    alerts = []
    for i in range(15):
        ts = now - timedelta(hours=random.randint(0, 48))
        lic = random.choice(DEMO_LICENSES[:3])
        action = random.choice(["buy", "sell", "close_long", "close_short"])
        sym = random.choice(SYMBOLS)
        code = random.choice([200, 200, 200, 400, 401, 500])
        msg = "Success" if code == 200 else random.choice(["Invalid license", "Rate limited", "Server error"])
        alerts.append((ts.strftime("%Y-%m-%d %H:%M:%S"), "127.0.0.1", "TradingView/1.0", json.dumps({"symbol": sym, "action": action}), action, sym, round(random.choice([0.01, 0.05, 0.10]), 2), code, msg, round(random.uniform(10, 200), 1), 0))
    try:
        c.executemany("INSERT INTO alert_history (timestamp, ip_address, user_agent, payload, action, symbol, volume, response_code, response_message, execution_time_ms, rate_limited) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", alerts)
        conn.commit()
        print(f"Seeded {len(alerts)} alerts")
    except Exception as e:
        print(f"Alert seed skipped: {e}")
    conn.close()


if __name__ == "__main__":
    seed_licenses()
    seed_trades()
    seed_audit()
    seed_alerts()
    print("Demo data seeded successfully!")
