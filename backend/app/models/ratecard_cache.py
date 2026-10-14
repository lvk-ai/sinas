"""The last good copy of a fetched model price ratecard (MODEL_PRICES_URL)."""
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class RatecardCache(Base):
    """One row per remote ratecard source (today only "url").

    `entries` holds the parsed ratecard, reduced to the fields pricing uses.
    A failed refresh records `last_error` and leaves `entries` alone, so
    pricing keeps working on the last good copy. `url` is the source the
    entries came from: once MODEL_PRICES_URL points elsewhere they are no
    longer used.
    """

    __tablename__ = "ratecard_cache"

    source: Mapped[str] = mapped_column(String(20), primary_key=True)
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entries: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    model_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
