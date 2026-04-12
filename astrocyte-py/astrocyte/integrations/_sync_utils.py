"""Shared async-to-sync bridge for integration adapters.

The naive ``asyncio.run()`` fails when called from an already-running event loop
(common in notebook kernels, ASGI frameworks, etc.). The ``ThreadPoolExecutor +
asyncio.run`` workaround creates a NEW event loop in the worker thread, which
breaks if the coroutine uses resources bound to the original loop (e.g. asyncpg
connection pools, aiohttp sessions).

This module provides a single helper that handles both cases safely:
- No running loop: use ``asyncio.run()`` directly.
- Running loop: schedule on the EXISTING loop via ``run_coroutine_threadsafe``,
  then block the calling (sync) thread waiting for the result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

T = TypeVar("T")


def _run_async_from_sync(coro: Coroutine[object, object, T]) -> T:
    """Run an async coroutine from synchronous code, safely handling nested loops.

    - If no event loop is running: creates one via ``asyncio.run()``.
    - If an event loop IS running (e.g. Jupyter, ASGI): schedules the coroutine
      on that loop via ``run_coroutine_threadsafe`` and blocks until complete.
      This keeps async resources (connection pools, sessions) on their original loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None or not loop.is_running():
        return asyncio.run(coro)

    # Schedule on the existing loop, block the sync caller thread
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
