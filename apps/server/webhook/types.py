"""PineTunnel webhook request model and shared response builders."""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.server.webhook.parser import PineTunnelSignal


class PineTunnelRequest(BaseModel):
    """PineTunnel webhook request - accepts PineTunnel syntax."""

    message: str = Field(..., description="PineTunnel message: LicenseID,Command,Symbol,Parameters")
    key: str | None = Field(None, description="Optional API key override")


def _build_trade_data(
    signal: PineTunnelSignal, client_ip: str, execution_time_ms: float, result: dict
) -> dict:
    """Build the trade-data dict common to both webhook endpoints."""
    return {
        "symbol": signal.symbol,
        "action": signal.command.value,
        "volume": result.get("volume", 0.0),
        "price": result.get("price"),
        "ticket": result.get("ticket"),
        "license_key": signal.license_id,
        "risk": signal.risk,
        "sl": signal.sl,
        "tp": signal.tp,
        "status": "success" if result.get("success") else "failed",
        "closed_count": result.get("closed_count", 0),
        "comment": signal.comment if signal.comment else "PineTunnel",
        "ip_address": client_ip,
        "execution_time_ms": execution_time_ms,
        "result": result,
    }


def _build_response(
    signal: PineTunnelSignal, execution_time_ms: float, result: dict
) -> JSONResponse:
    """Build the JSON response common to both webhook endpoints."""
    return JSONResponse(
        status_code=200 if result.get("success") else 400,
        content={
            "status": "success" if result.get("success") else "failed",
            "command": signal.command.value,
            "symbol": signal.symbol,
            "result": result,
            "execution_time_ms": round(execution_time_ms, 2),
        },
    )
