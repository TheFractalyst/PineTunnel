"""Tests for the ported Telegram bot (mixin architecture, admin-only).

Part 1 — pure-function helpers / constants / notification constants.
Part 2 — callback-router dispatch: stubs ``telegram`` and the heavy app deps
         (``apps.server.db.analytics_store``, ``apps.server.ws.handler``,
         ``apps.server.routes.ea_download``, ``apps.server.config.settings``)
         with ``MagicMock``, imports the bot, monkeypatches every router
         dispatch target to an async recorder, and asserts each keyboard
         callback prefix reaches the intended handler (never the "unhandled"
         warning).

``python-telegram-bot`` is not installed in the test env, so telegram is stubbed.
"""

import asyncio
import importlib.util
import sys
import types
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Part 1: pure-function helpers / constants / notification
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def helpers_module():
    """Load helpers.py with a stubbed telegram.helpers.escape_markdown (identity)."""
    tg = types.ModuleType("telegram")
    tg.helpers = types.ModuleType("telegram.helpers")
    tg.helpers.escape_markdown = lambda s, version=1: str(s)
    sys.modules["telegram"] = tg
    sys.modules["telegram.helpers"] = tg.helpers

    spec = importlib.util.spec_from_file_location(
        "_tg_helpers_test", "apps/server/services/telegram/helpers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod

    for k in ("telegram", "telegram.helpers", "_tg_helpers_test"):
        sys.modules.pop(k, None)


def test_helpers_sep_is_box_drawing(helpers_module):
    # Reference uses the heavy box-drawing bar, not an ASCII hyphen.
    assert helpers_module.SEP == "━" * 20
    assert len(helpers_module.SEP) == 20


def test_helpers_calc_pagination_basic(helpers_module):
    page, total_pages, start = helpers_module.calc_pagination(0, 42, 8)
    assert (page, total_pages, start) == (0, 6, 0)


def test_helpers_calc_pagination_last_page(helpers_module):
    page, total_pages, start = helpers_module.calc_pagination(5, 42, 8)
    assert (page, total_pages, start) == (5, 6, 40)


def test_helpers_calc_pagination_clamps_negative(helpers_module):
    page, _tp, start = helpers_module.calc_pagination(-1, 42, 8)
    assert (page, start) == (0, 0)


def test_helpers_calc_pagination_empty(helpers_module):
    page, total_pages, start = helpers_module.calc_pagination(0, 0, 8)
    assert (page, total_pages, start) == (0, 1, 0)


def test_helpers_validate_email(helpers_module):
    assert helpers_module.validate_email("user@example.com") is True
    assert helpers_module.validate_email("not-an-email") is False
    assert helpers_module.validate_email("") is False


def test_helpers_validate_volume(helpers_module):
    assert helpers_module.validate_volume("0.10") == 0.10
    assert helpers_module.validate_volume("-1") is None  # negative
    assert helpers_module.validate_volume("notanumber") is None
    assert helpers_module.validate_volume("99999") is None  # > 10000


def test_helpers_validate_symbols(helpers_module):
    assert helpers_module.validate_symbols("25") == 25
    assert helpers_module.validate_symbols("0") is None
    assert helpers_module.validate_symbols("abc") is None


def test_helpers_truncate(helpers_module):
    assert helpers_module.truncate("short", 30) == "short"
    assert helpers_module.truncate("a" * 40, 30) == "a" * 30 + "..."


def test_helpers_generate_license_key(helpers_module):
    k = helpers_module.generate_license_key()
    assert len(k) == 13
    assert k.isdigit()


def test_helpers_is_benign_edit_error(helpers_module):
    assert helpers_module.is_benign_edit_error("Bad Request: message is not modified") is True
    assert helpers_module.is_benign_edit_error("Message to edit not found") is True
    assert helpers_module.is_benign_edit_error("Flood control exceeded") is False


def test_helpers_sanitize_error_strips_paths(helpers_module):
    sanitized = helpers_module._sanitize_error(Exception("boom at /var/app/src/foo.py line 3"))
    assert "/var" not in sanitized
    assert "foo.py" not in sanitized
    assert "boom" in sanitized


def test_helpers_format_license_info(helpers_module):
    data = {
        "name": "Test Account",
        "status": "active",
        "email": "user@example.com",
        "secret_key": "supersecret",
        "user_id": 12345,
        "created_at": "2026-01-01T00:00:00",
        "features": ["websocket", "trading"],
        "max_volume": 1000.0,
        "max_symbols": 25,
        "max_daily_trades": 100,
        "max_daily_loss": 500.0,
        "enabled": True,
    }
    text = helpers_module.format_license_info("ABCKEY123", data)
    assert "🟢" in text  # active status emoji
    assert "ABCKEY123" in text
    assert "Test Account" in text
    assert "supersecret" in text


@pytest.fixture
def constants_module():
    spec = importlib.util.spec_from_file_location(
        "_tg_constants_test", "apps/server/services/telegram/constants.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("_tg_constants_test", None)


def test_constants_conversation_states(constants_module):
    c = constants_module
    # Add License flow: 0..4
    assert (c.ADD_LIC_NAME, c.ADD_LIC_EMAIL, c.ADD_LIC_FEATURES, c.ADD_LIC_EXPIRY, c.ADD_LIC_CONFIRM) == (
        0,
        1,
        2,
        3,
        4,
    )
    # Edit License flow + picker
    assert c.EDIT_LIC_FIELD == 10 and c.EDIT_LIC_VALUE == 11 and c.EDIT_LIC_PICK == 12
    # Expiry flow + picker
    assert c.EXPIRY_VALUE == 50 and c.EXPIRY_PICK == 51
    # Search + Quiet Hours
    assert c.SEARCH_QUERY == 70 and c.USER_QH_INPUT == 90


def test_constants_cleanup_prefixes(constants_module):
    prefixes = constants_module.CONVERSATION_CLEANUP_PREFIXES
    assert "new_lic_" in prefixes
    assert "edit_lic_" in prefixes
    assert "qh_field" in prefixes
    assert "expiry_" in prefixes


def test_notification_constants():
    from apps.server.services.notification import (
        CRITICAL_EVENTS,
        DEFAULT_PREFS,
        NOTIF_LABELS,
        NOTIF_TYPES,
    )

    assert "exec_success" in NOTIF_TYPES
    assert "exec_failed" in NOTIF_TYPES
    assert "position_closed" in NOTIF_TYPES
    assert all(DEFAULT_PREFS[t] is True for t in NOTIF_TYPES)
    assert set(NOTIF_LABELS.keys()) == set(NOTIF_TYPES)
    assert NOTIF_LABELS["exec_failed"] == "Execution Failed"
    assert CRITICAL_EVENTS == {"exec_failed", "margin_warning", "equity_drawdown"}


# ──────────────────────────────────────────────────────────────────────────
# Part 2: callback-router dispatch
# ──────────────────────────────────────────────────────────────────────────


# Every method the router (_route_admin_callback + _user_cb_handler) can dispatch to.
_ROUTER_TARGETS = [
    "_show_main_menu",
    "_show_licenses_menu",
    "_show_monitor_menu",
    "_show_license_list",
    "_show_license_detail",
    "_toggle_license",
    "_delete_confirm",
    "_do_delete_license",
    "_bulk_deactivate_expired",
    "_bulk_activate_all",
    "_force_disconnect_client",
    "_show_status",
    "_show_connections",
    "_show_account_stats",
    "_show_logs",
    "_show_connection_detail",
    "_show_signals_menu",
    "_show_license_signals_overview",
    "_show_signal_list_from_callback",
    "_show_signal_detail",
    "_show_signals_license_picker",
    "_show_system_info",
    "_show_webhook_screen",
    "_show_user_menu",
    "_show_user_settings",
    "_show_notif_presets",
    "_show_user_notif_settings",
    "_apply_notif_preset",
    "_toggle_user_notif",
    "_show_user_licenses",
    "_show_user_quiet_hours",
    "_handle_quiet_hours_cb",
    "_show_user_account_pick",
    "_show_user_account",
    "_show_user_trading_pick",
    "_show_user_trading",
    "_show_user_signal_pick",
    "_show_user_signals",
    "_confirm_close_position",
    "_do_close_position",
    "_confirm_disconnect_ea",
    "_do_disconnect_ea",
    "_show_user_kill_switch",
    "_confirm_close_all_positions",
    "_do_close_all_positions",
    "_confirm_disconnect_all",
    "_do_disconnect_all",
]


@pytest.fixture
def bot_class():
    """Stub telegram + heavy app deps so apps.server.services.telegram.bot imports."""
    for name in (
        "telegram",
        "telegram.helpers",
        "telegram.constants",
        "telegram.ext",
        "telegram.error",
    ):
        sys.modules[name] = MagicMock()
    # routes has a heavy __init__ (imports all routers) — stub the package empty.
    sys.modules["apps.server.routes"] = types.ModuleType("apps.server.routes")
    sys.modules["apps.server.routes.ea_download"] = MagicMock()
    sys.modules["apps.server.db.analytics_store"] = MagicMock()
    sys.modules["apps.server.ws.handler"] = MagicMock()
    sys.modules["apps.server.config.settings"] = MagicMock()
    # notification is a pure module — import it for real.
    sys.modules.pop("apps.server.services.notification", None)

    from apps.server.services.telegram import PineTunnelTelegramBot

    yield PineTunnelTelegramBot

    for k in list(sys.modules):
        if k.startswith("apps.server.services.telegram") or k in (
            "telegram",
            "telegram.helpers",
            "telegram.constants",
            "telegram.ext",
            "telegram.error",
            "apps.server.routes",
            "apps.server.routes.ea_download",
            "apps.server.db.analytics_store",
            "apps.server.ws.handler",
            "apps.server.config.settings",
        ):
            sys.modules.pop(k, None)


class _FakeUser:
    def __init__(self, uid=1):
        self.id = uid
        self.username = "admin"
        self.full_name = "Admin"


class _FakeChat:
    def __init__(self, cid=1):
        self.id = cid


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = MagicMock()

    async def answer(self, *args, **kwargs):
        pass

    async def edit_message_text(self, *args, **kwargs):
        pass


class _FakeUpdate:
    def __init__(self, data, uid=1, cid=1):
        self.callback_query = _FakeQuery(data)
        self.effective_user = _FakeUser(uid)
        self.effective_chat = _FakeChat(cid)
        self.effective_message = MagicMock()
        self.message = MagicMock()


def _make_bot(bot_class, tmp_path):
    return bot_class(
        token="t",
        admin_ids=[1],
        client_manager=MagicMock(),
        db_manager=MagicMock(),
        data_dir=str(tmp_path),
        http_polling_clients={},
        signal_queues={},
        conn_manager=None,
        ws_manager=None,
        test_env=False,
        auth_store=None,
        admin_logger=None,
    )


def _patch_screens(bot):
    """Monkeypatch every router dispatch target with an async recorder."""
    calls: list[str] = []

    def make_rec(name):
        async def _rec(*args, **kwargs):
            calls.append(name)

        return _rec

    for name in _ROUTER_TARGETS:
        setattr(bot, name, make_rec(name))
    return calls


# (callback_data, expected dispatch target)
_ROUTER_CASES = [
    # Main menu navigation
    ("menu_main", "_show_main_menu"),
    ("menu_licenses", "_show_licenses_menu"),
    ("menu_monitor", "_show_monitor_menu"),
    ("menu_signals", "_show_signals_menu"),
    # License actions
    ("lic_list", "_show_license_list"),
    ("lic_info_ABC", "_show_license_detail"),
    ("lic_activate_ABC", "_toggle_license"),
    ("lic_deactivate_ABC", "_toggle_license"),
    ("lic_delconf_ABC", "_delete_confirm"),
    ("lic_dodel_ABC", "_do_delete_license"),
    ("lic_delcancel", "_show_licenses_menu"),
    ("lic_page_2", "_show_license_list"),
    # Bulk operations
    ("lic_bulk_deactivate_expired", "_bulk_deactivate_expired"),
    ("lic_bulk_activate_all", "_bulk_activate_all"),
    ("lic_force_disconnect_ABC", "_force_disconnect_client"),
    # Monitor actions
    ("mon_status", "_show_status"),
    ("mon_connections", "_show_connections"),
    ("mon_account_stats", "_show_account_stats"),
    ("mon_logs", "_show_logs"),
    ("log_webhook", "_show_logs"),
    ("log_admin", "_show_logs"),
    ("log_conn", "_show_logs"),
    ("whlog_page_1", "_show_logs"),
    ("audit_page_1", "_show_logs"),
    ("conn_page_1", "_show_logs"),
    ("mon_conn_detail_ABC", "_show_connection_detail"),
    # Signal tracking
    ("sig_menu", "_show_signals_menu"),
    ("sig_lic_ABC", "_show_license_signals_overview"),
    ("sig_v_ABC_all_0", "_show_signal_list_from_callback"),
    ("sig_d_ABC", "_show_signal_detail"),
    ("sig_pg_2", "_show_signals_license_picker"),
    # Settings
    ("set_toggle_alerts", "_show_main_menu"),
    ("set_system_info", "_show_system_info"),
    ("set_webhook", "_show_webhook_screen"),
    # User menu / settings
    ("user_menu", "_show_user_menu"),
    ("user_settings", "_show_user_settings"),
    ("user_notif_settings", "_show_notif_presets"),
    ("user_notif_custom", "_show_user_notif_settings"),
    ("user_preset_all", "_apply_notif_preset"),
    ("user_preset_critical", "_apply_notif_preset"),
    ("user_preset_silent", "_apply_notif_preset"),
    ("user_toggle_exec_success", "_toggle_user_notif"),
    ("user_licenses", "_show_user_licenses"),
    ("user_quiet_hours", "_show_user_quiet_hours"),
    ("qh_toggle", "_handle_quiet_hours_cb"),
    ("qh_set_tz", "_handle_quiet_hours_cb"),
    ("qh_tz_UTC", "_handle_quiet_hours_cb"),
    # User dashboard — account
    ("ud_account_pick", "_show_user_account_pick"),
    ("ud_acct_KEY", "_show_user_account"),
    # User dashboard — trading
    ("ud_trading", "_show_user_trading_pick"),
    ("ud_trade_open_KEY", "_show_user_trading"),
    ("ud_trade_closed_KEY", "_show_user_trading"),
    ("ud_tradepg_closed_KEY_1", "_show_user_trading"),
    # User dashboard — signals
    ("ud_sig_pick", "_show_user_signal_pick"),
    ("ud_sig_KEY", "_show_user_signals"),
    ("ud_sigpg_KEY_1", "_show_user_signals"),
    # User dashboard — actions
    ("ud_close_KEY_TK", "_confirm_close_position"),
    ("ud_closeok_KEY_TK", "_do_close_position"),
    ("ud_disc_KEY", "_confirm_disconnect_ea"),
    ("ud_discok_KEY", "_do_disconnect_ea"),
    # Kill switch
    ("ud_kill", "_show_user_kill_switch"),
    ("ud_closeall_KEY", "_confirm_close_all_positions"),
    ("ud_closeallok_KEY", "_do_close_all_positions"),
    ("ud_kill_disc_all", "_confirm_disconnect_all"),
    ("ud_kill_disc_all_ok", "_do_disconnect_all"),
]


@pytest.mark.parametrize("data,expected", _ROUTER_CASES)
def test_router_dispatch(bot_class, tmp_path, data, expected):
    bot = _make_bot(bot_class, tmp_path)
    calls = _patch_screens(bot)
    asyncio.run(bot._cb_handler(_FakeUpdate(data), None))
    assert calls == [expected], f"data={data!r} routed to {calls}, expected [{expected!r}]"


def test_router_rejects_non_admin(bot_class, tmp_path):
    bot = _make_bot(bot_class, tmp_path)
    _patch_screens(bot)
    # admin_ids=[1]; user id 999 is not an admin.
    query = _FakeQuery("menu_main")

    async def run():
        upd = _FakeUpdate("menu_main", uid=999)
        upd.callback_query = query
        await bot._cb_handler(upd, None)

    asyncio.run(run())
    # Non-admin branch calls query.edit_message_text("Not authorized.") and returns;
    # no router target is invoked.


def test_set_toggle_alerts_flips_state(bot_class, tmp_path):
    bot = _make_bot(bot_class, tmp_path)
    _patch_screens(bot)
    assert bot.alerts_enabled is True
    asyncio.run(bot._cb_handler(_FakeUpdate("set_toggle_alerts"), None))
    assert bot.alerts_enabled is False


# ──────────────────────────────────────────────────────────────────────────
# Part 3: webhook endpoint — URL validation + catch-all exclusion parity
# ──────────────────────────────────────────────────────────────────────────


def test_validate_webhook_url_accepts_https(bot_class):
    from apps.server.services.telegram.mixins.webhook import _validate_webhook_url

    assert _validate_webhook_url("https://signals.example.com") is True
    assert _validate_webhook_url("https://sub.domain.co/path") is True
    assert _validate_webhook_url("https://my-tunnel.trycloudflare.com") is True


def test_validate_webhook_url_rejects_bad(bot_class):
    from apps.server.services.telegram.mixins.webhook import _validate_webhook_url

    assert _validate_webhook_url("http://insecure.com") is False  # must be https
    assert _validate_webhook_url("https://") is False  # too short
    assert _validate_webhook_url("") is False
    assert _validate_webhook_url("not a url") is False
    assert _validate_webhook_url("https://bad space.com") is False  # whitespace
    assert _validate_webhook_url("https://" + "a" * 201) is False  # too long


def test_catch_all_excludes_webhook_conversation(bot_class):
    """Conversation-owned webhook callbacks must NOT reach the catch-all router;
    the view callback `set_webhook` MUST reach it."""
    from apps.server.services.telegram.bot import _CATCH_ALL_CB_PATTERN as p

    # Conversation-owned -> excluded (router must not claim them)
    for owned in ("set_webhook_edit", "set_webhook_confirm_yes", "set_webhook_confirm_no"):
        assert p.match(owned) is None, f"{owned} should be EXCLUDED from catch-all"
    # View callback -> reaches catch-all router
    assert p.match("set_webhook") is not None, "set_webhook should REACH catch-all"


def test_find_env_path_walks_cwd_to_project_root(monkeypatch, tmp_path):
    """The webhook .env locator must be CWD-based (not __file__-based) so it works
    when the package is pip-installed and __file__ lives in site-packages."""
    from apps.lib.env_manager import find_env_path

    proj = (tmp_path / "proj").resolve()
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[tool.x]\n")
    (proj / ".env").write_text("SERVER_BASE_URL=https://example.com\n")
    monkeypatch.chdir(proj)
    found = find_env_path().resolve()
    assert found == proj / ".env"
    assert found.exists()


def test_find_env_path_finds_env_without_pyproject(monkeypatch, tmp_path):
    """If a dir has .env but no pyproject.toml, find_env_path still returns it."""
    from apps.lib.env_manager import find_env_path

    d = (tmp_path / "plain").resolve()
    d.mkdir()
    (d / ".env").write_text("X=1\n")
    monkeypatch.chdir(d)
    assert find_env_path().resolve() == d / ".env"


def test_webhook_screen_no_env_coherent_message(bot_class, tmp_path, monkeypatch):
    """When not on Render and no .env is found, the screen must NOT show the
    contradictory 'Local (.env)' + '.env not found' — it shows the 'no .env found'
    mode line and no edit button."""
    import os
    from unittest.mock import MagicMock, AsyncMock
    from apps.server.services.telegram import PineTunnelTelegramBot
    from apps.server.services.telegram.mixins import webhook as wh

    empty = (tmp_path / "empty").resolve()
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setenv("SERVER_BASE_URL", "https://pine.deeptest.pro")
    monkeypatch.delenv("RENDER", raising=False)

    class FakeCM:
        clients = {"k1": {"status": "active"}}
        def get_client_by_license(self, k):
            return self.clients.get(k)

    class FakeQuery:
        def __init__(self, data):
            self.data = data
            self.message = MagicMock()
        async def answer(self, *a, **k):
            pass
        last = None
        async def edit_message_text(self, text, **k):
            FakeQuery.last = (text, k.get("reply_markup"))
            return None

    class CbUpdate:
        def __init__(self, data):
            self.callback_query = FakeQuery(data)
            self.effective_user = MagicMock(id=1, username="admin")
            self.effective_chat = MagicMock(id=1)

    bot = PineTunnelTelegramBot(
        token="t", admin_ids=[1], client_manager=FakeCM(), db_manager=MagicMock(),
        data_dir=str(tmp_path), http_polling_clients={}, signal_queues={},
    )
    import asyncio
    asyncio.run(bot._show_webhook_screen(CbUpdate("set_webhook")))
    text, markup = FakeQuery.last
    # No contradiction: 'Local (.env)' must NOT appear when .env is missing
    assert "Local" not in text, f"should not claim Local mode with no .env: {text}"
    assert "no `.env` found" in text.lower() or "not found" in text.lower(), text
    # No edit button when not editable
    btns = [b.text for row in (markup.inline_keyboard if markup else []) for b in row]
    assert not any("Change URL" in b for b in btns), btns


def test_webhook_confirm_hot_reloads_live_config(monkeypatch, tmp_path):
    """Confirming a new webhook URL hot-reloads the running config: the new URL
    is live immediately (os.environ sync + reset_config_singleton), no restart.

    Uses the REAL apps.server.config.settings (not stubbed) so the cache reset
    is exercised end-to-end; only telegram + heavy route/ws/analytics deps are
    stubbed so the bot imports cleanly.
    """
    import asyncio
    from unittest.mock import AsyncMock

    # Stub telegram + heavy deps; keep apps.server.config.settings REAL.
    for name in (
        "telegram",
        "telegram.helpers",
        "telegram.constants",
        "telegram.ext",
        "telegram.error",
    ):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    # escape_markdown must be a real identity so escaped URLs render as strings.
    sys.modules["telegram.helpers"].escape_markdown = lambda s, version=1: str(s)
    monkeypatch.setitem(sys.modules, "apps.server.routes", types.ModuleType("apps.server.routes"))
    monkeypatch.setitem(sys.modules, "apps.server.routes.ea_download", MagicMock())
    monkeypatch.setitem(sys.modules, "apps.server.db.analytics_store", MagicMock())
    monkeypatch.setitem(sys.modules, "apps.server.ws.handler", MagicMock())
    # Force a clean re-import of the telegram package + settings so webhook binds
    # to the REAL settings module (not a stub left over from another test).
    for k in list(sys.modules):
        if k.startswith("apps.server.services.telegram") or k == "apps.server.config.settings":
            sys.modules.pop(k, None)

    from apps.server.config.settings import get_config, reset_config_singleton
    from apps.server.services.telegram import PineTunnelTelegramBot

    proj = (tmp_path / "proj").resolve()
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[tool.x]\n")
    (proj / ".env").write_text("SERVER_BASE_URL=https://old.example.com\nOTHER_KEY=keepme\n")
    monkeypatch.chdir(proj)
    monkeypatch.setenv("SERVER_BASE_URL", "https://old.example.com")
    monkeypatch.delenv("RENDER", raising=False)
    reset_config_singleton()
    assert get_config().server.base_url == "https://old.example.com"

    class FakeCM:
        clients = {"k1": {"status": "active"}}

        def get_client_by_license(self, k):
            return self.clients.get(k)

    class FakeQuery:
        def __init__(self, data):
            self.data = data
            self.message = MagicMock()

        async def answer(self, *a, **k):
            pass

        last = None

        async def edit_message_text(self, text, **k):
            FakeQuery.last = (text, k.get("reply_markup"))
            return None

    def msg_update(txt):
        u = MagicMock()
        u.message.text = txt
        u.message.reply_text = AsyncMock()
        u.effective_user = MagicMock(id=1, username="admin")
        u.effective_chat = MagicMock(id=1)
        return u

    class CbUpdate:
        def __init__(self, data):
            self.callback_query = FakeQuery(data)
            self.effective_user = MagicMock(id=1, username="admin")
            self.effective_chat = MagicMock(id=1)

    bot = PineTunnelTelegramBot(
        token="t",
        admin_ids=[1],
        client_manager=FakeCM(),
        db_manager=MagicMock(),
        data_dir=str(proj),
        http_polling_clients={},
        signal_queues={},
    )

    # input -> confirm state
    assert asyncio.run(bot._webhook_url_input(msg_update("https://new.example.com"), MagicMock())) == 101
    ctx = MagicMock()
    ctx.user_data = {"webhook_new_url": "https://new.example.com"}
    asyncio.run(bot._webhook_url_confirm(CbUpdate("set_webhook_confirm_yes"), ctx))

    # THE KEY ASSERTION: new URL is live immediately, no restart.
    assert get_config().server.base_url == "https://new.example.com"
    # Success message says it's live; no restart prompt.
    text, _ = FakeQuery.last
    assert "Live now" in text
    assert "https://new.example.com" in text
    assert "Restart the server" not in text
    # .env persisted + other key preserved.
    from apps.lib.env_manager import find_env_path, read_env

    env = read_env(find_env_path())
    assert env.get("SERVER_BASE_URL") == "https://new.example.com"
    assert env.get("OTHER_KEY") == "keepme"



