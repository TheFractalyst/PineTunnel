"""Persistent per-user EA download endpoint.

URL format: /api/ea/download/{user_id}/{platform}/{signature}
- user_id: Telegram user ID
- platform: mt5 or mt4
- signature: HMAC-SHA256(user_id:platform) signed with JWT_SECRET

Links are permanent (no expiry). Tied to Telegram user_id.
Users can share the link but the signature only validates for that user_id.
"""

import hashlib
import hmac
import io
import logging
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

from apps.server.config.settings import get_config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ea-download"])

# EA file locations relative to project root
_EA_FILES = {
    "mt5": {
        "ea": "apps/ea/mt5/PineTunnel_EA.ex5",
        "dll": "apps/ea/dll/bin/PTWebSocket.dll",
    },
    "mt4": {
        "ea": "apps/ea/mt4/PineTunnel_EA_MT4.ex4",
        "dll": "apps/ea/dll/bin/PTWebSocket32.dll",
    },
}


def _get_secret() -> str:
    """Get the signing secret (reuses JWT secret)."""
    cfg = get_config()
    if not cfg.jwt_secret:
        raise RuntimeError("JWT_SECRET not configured - cannot sign download links")
    return cfg.jwt_secret


def generate_download_url(user_id: int, platform: str = "mt5") -> str:
    """Generate a persistent download URL for a Telegram user.

    URL format: {base_url}/api/ea/download/{user_id}/{platform}/{sig}
    No expiry - link is permanent and personalized.
    """
    payload = f"{user_id}:{platform}"
    secret = _get_secret()
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    cfg = get_config()
    base = cfg.server.base_url.rstrip("/")
    return f"{base}/api/ea/download/{user_id}/{platform}/{sig}"


def verify_download_signature(user_id: int, platform: str, sig: str) -> bool:
    """Verify the download signature for a user_id + platform."""
    if platform not in _EA_FILES:
        return False
    payload = f"{user_id}:{platform}"
    secret = _get_secret()
    expected_sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected_sig)


def _create_zip(platform: str) -> bytes:
    """Create a zip file with EA + DLL + README for the given platform."""
    files = _EA_FILES[platform]
    project_root = Path(__file__).resolve().parents[3]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        ea_path = project_root / files["ea"]
        dll_path = project_root / files["dll"]

        if ea_path.exists():
            zf.write(ea_path, ea_path.name)
        else:
            logger.error("EA file not found: %s", ea_path)

        if dll_path.exists():
            zf.write(dll_path, dll_path.name)
        else:
            logger.error("DLL file not found: %s", dll_path)

        readme = (
            f"PineTunnel EA Installation ({platform.upper()})\n\n"
            f"1. Copy {'PineTunnel_EA.ex5' if platform == 'mt5' else 'PineTunnel_EA_MT4.ex4'} "
            f"to MQL{'5' if platform == 'mt5' else '4'}/Experts/\n"
            f"2. Copy {'PTWebSocket.dll' if platform == 'mt5' else 'PTWebSocket32.dll'} "
            f"to MQL{'5' if platform == 'mt5' else '4'}/Libraries/\n"
            f"3. Restart MetaTrader\n"
            f"4. Attach PineTunnel EA to your chart\n"
            f"5. Enter your License Key in the EA settings\n"
            f"6. In TradingView, add the strategy and enter your License Key + Secret Key\n\n"
            f"The EA auto-updates itself after first install.\n"
        )
        zf.writestr("README.txt", readme)

    return buf.getvalue()


@router.get("/api/ea/download/{user_id}/{platform}/{sig}")
async def download_ea_persistent(user_id: int, platform: str, sig: str):
    """Download EA files using a persistent per-user link.

    URL is permanent (no expiry). Tied to Telegram user_id.
    Signature is HMAC-SHA256(user_id:platform) signed with JWT_SECRET.
    """
    if not verify_download_signature(user_id, platform, sig):
        return Response(
            content="Invalid download link. Use /download in the bot for a valid link.",
            media_type="text/plain",
            status_code=403,
        )

    logger.info("EA download: user_id=%s platform=%s", user_id, platform)

    try:
        zip_data = _create_zip(platform)
        filename = f"pinetunnel_ea_{platform}.zip"
        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error("EA download failed: %s", e)
        return Response(
            content="Download failed. Please contact support.",
            media_type="text/plain",
            status_code=500,
        )


# Legacy endpoint - keep for old links still in circulation
@router.get("/api/ea/get/{token}")
async def download_ea_signed(token: str):
    """Legacy: Download EA files using old token format."""
    parts = token.split(".")
    if len(parts) != 3:
        return Response(
            content="Invalid download link. Use /download in the bot for a new link.",
            media_type="text/plain",
            status_code=403,
        )

    license_key, platform, sig = parts
    payload = f"{license_key}:{platform}"
    secret = _get_secret()
    expected_sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]

    if not hmac.compare_digest(sig, expected_sig):
        return Response(
            content="Invalid download link. Use /download in the bot for a new link.",
            media_type="text/plain",
            status_code=403,
        )

    if platform not in _EA_FILES:
        return Response(
            content="Invalid platform. Use /download in the bot.",
            media_type="text/plain",
            status_code=403,
        )

    logger.info("EA download (legacy): license=%s platform=%s", license_key[:4] + "***", platform)

    try:
        zip_data = _create_zip(platform)
        filename = f"pinetunnel_ea_{platform}.zip"
        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error("EA download failed: %s", e)
        return Response(
            content="Download failed. Please contact support.",
            media_type="text/plain",
            status_code=500,
        )
