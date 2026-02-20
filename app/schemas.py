from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


# --- Bitrix imbot events (very loose / defensive) ---


class BitrixEventEnvelope(BaseModel):
    """Generic envelope for any Bitrix event callback."""

    event: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    # accept everything
    model_config = {"extra": "allow"}
