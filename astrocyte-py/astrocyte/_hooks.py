"""Hook manager — registration and dispatch for event hooks."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from astrocyte.types import HookEvent

# Hook handler type — FFI-safe: takes a HookEvent, returns an awaitable or None.
HookHandler = Callable[[HookEvent], Awaitable[None] | None]

logger = logging.getLogger("astrocyte")


class HookManager:
    """Thread-safe event hook registration and dispatch."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookHandler]] = {}
        self._lock = threading.Lock()

    def register(self, event_type: str, handler: HookHandler) -> None:
        """Register an event hook handler."""
        with self._lock:
            if event_type not in self._hooks:
                self._hooks[event_type] = []
            self._hooks[event_type].append(handler)

    async def fire(
        self,
        event_type: str,
        bank_id: str | None = None,
        data: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        """Fire all registered hooks for an event type. Non-blocking, failures logged."""
        with self._lock:
            handlers = list(self._hooks.get(event_type, []))
        if not handlers:
            return
        event = HookEvent(
            event_id=uuid.uuid4().hex,
            type=event_type,
            timestamp=datetime.now(timezone.utc),
            bank_id=bank_id,
            data=data,
        )
        for handler in handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await result
            except Exception:
                logger.warning(
                    "Hook handler failed for event %s (bank=%s)",
                    event_type,
                    bank_id,
                    exc_info=True,
                )
