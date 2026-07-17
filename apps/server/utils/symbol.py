from functools import lru_cache


@lru_cache(maxsize=512)
def normalize_symbol(symbol: str) -> str:
    """Normalize trading symbol: uppercase base, lowercase extension.

    Examples:
        xaug26.sim -> XAUG26.sim
        XAUG26.SIM -> XAUG26.sim
        EURUSD     -> EURUSD
        eurusd     -> EURUSD
    """
    if "." in symbol:
        parts = symbol.rsplit(".", 1)
        return parts[0].upper() + "." + parts[1].lower()
    return symbol.upper()
