"""GitHub REST API poll :class:`~astrocyte.ingest.source.IngestSource` — repository issues."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from astrocyte.config import SourceConfig
from astrocyte.errors import IngestError
from astrocyte.ingest.bank_resolve import resolve_ingest_bank_id
from astrocyte.ingest.logutil import log_ingest_event
from astrocyte.ingest.webhook import RetainCallable
from astrocyte.types import AstrocyteContext, HealthStatus

logger = logging.getLogger("astrocyte_ingestion_github")


def _token(cfg: SourceConfig) -> str:
    auth = cfg.auth or {}
    raw = auth.get("token")
    if raw is None or not str(raw).strip():
        raise IngestError("GitHub poll requires auth.token")
    return str(raw).strip()


def _api_base(cfg: SourceConfig) -> str:
    u = (cfg.url or "").strip()
    if u:
        return u.rstrip("/")
    return "https://api.github.com"


def _owner_repo(cfg: SourceConfig) -> tuple[str, str]:
    p = (cfg.path or "").strip()
    if not p or p.count("/") != 1:
        raise IngestError("GitHub poll requires path: owner/repo")
    owner, repo = p.split("/", 1)
    if not owner.strip() or not repo.strip():
        raise IngestError("GitHub poll path owner/repo must be non-empty")
    return owner.strip(), repo.strip()


def _int_header(raw: object) -> int | None:
    """Parse GitHub numeric response headers; invalid values become ``None``."""
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


class GithubPollIngestSource:
    """Poll ``GET /repos/{owner}/{repo}/issues`` and retain new/updated issues (not pull requests)."""

    def __init__(
        self,
        source_id: str,
        config: SourceConfig,
        *,
        retain: RetainCallable,
    ) -> None:
        self._source_id = source_id
        self._config = config
        self._retain = retain
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._last_error: str | None = None
        # GitHub issue id -> last ingested updated_at (ISO) to avoid duplicate retains
        self._seen_updated: dict[int, str] = {}
        self._since: str | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "poll"

    @property
    def config(self) -> SourceConfig:
        return self._config

    def _interval_s(self) -> float:
        n = self._config.interval_seconds
        if n is None or int(n) < 60:
            raise IngestError("poll requires interval_seconds >= 60")
        return float(int(n))

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._last_error = None
        self._running = True
        base = _api_base(self._config)
        host = urlsplit(base).hostname or ""
        self._client = httpx.AsyncClient(
            base_url=base,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {_token(self._config)}",
                "User-Agent": "astrocyte-ingestion-github",
            },
            timeout=60.0,
        )
        # Enterprise often uses self-signed or custom CA; keep verify=True by default
        if "github.com" not in host and host:
            logger.info("github poll using API base %s", base)
        self._task = asyncio.create_task(self._run_loop(), name=f"astrocyte-github-poll-{self._source_id}")

    async def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> HealthStatus:
        if not self._running or self._task is None:
            return HealthStatus(healthy=False, message="github poll source stopped")
        if self._last_error:
            return HealthStatus(healthy=False, message=self._last_error)
        return HealthStatus(healthy=True, message="github poll loop running")

    async def _run_loop(self) -> None:
        if self._client is None:
            raise RuntimeError("HTTP client not initialized")
        interval = self._interval_s()
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = str(e)
                log_ingest_event(
                    logger,
                    "github_poll_cycle_failed",
                    source_id=self._source_id,
                    error=str(e),
                )
                logger.exception("github poll failed for %s", self._source_id)
            # interruptible sleep
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> None:
        if self._client is None:
            raise RuntimeError("HTTP client not initialized")
        owner, repo = _owner_repo(self._config)
        params: dict[str, Any] = {
            "state": "all",
            "per_page": 50,
            "sort": "updated",
            "direction": "desc",
        }
        if self._since:
            params["since"] = self._since

        r = await self._client.get(f"/repos/{owner}/{repo}/issues", params=params)
        rem_raw = r.headers.get("x-ratelimit-remaining") or r.headers.get("X-RateLimit-Remaining")
        if rem_raw is not None:
            rem = _int_header(rem_raw)
            if rem is not None and rem < 20:
                log_ingest_event(
                    logger,
                    "github_poll_rate_limit_low",
                    source_id=self._source_id,
                    remaining=rem,
                    reset=r.headers.get("x-ratelimit-reset") or r.headers.get("X-RateLimit-Reset"),
                )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log_ingest_event(
                logger,
                "github_poll_http_error",
                source_id=self._source_id,
                status_code=e.response.status_code,
                url=str(e.request.url),
            )
            raise
        issues = r.json()
        if not isinstance(issues, list):
            raise IngestError("GitHub issues response must be a JSON array")

        cursor_max: str | None = None
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            uat = issue.get("updated_at")
            if isinstance(uat, str):
                if cursor_max is None or uat > cursor_max:
                    cursor_max = uat

        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("pull_request"):
                continue
            iid = issue.get("id")
            upd = issue.get("updated_at")
            if not isinstance(iid, int) or not isinstance(upd, str):
                continue
            if self._seen_updated.get(iid) == upd:
                continue
            title = issue.get("title")
            body = issue.get("body")
            num = issue.get("number")
            html_url = issue.get("html_url")
            t = title if isinstance(title, str) else ""
            b = body if isinstance(body, str) else ""
            n = num if isinstance(num, int) else 0
            u = html_url if isinstance(html_url, str) else ""
            text = f"[GitHub #{n}] {t}\n\n{b}".strip()
            if not text:
                text = f"[GitHub #{n}] (empty)"

            user = issue.get("user")
            login = None
            if isinstance(user, dict):
                lg = user.get("login")
                if isinstance(lg, str):
                    login = lg

            eff_principal = self._config.principal
            if isinstance(eff_principal, str) and eff_principal.strip():
                p = eff_principal.strip()
            elif login:
                p = f"github:{login}"
            else:
                p = None

            try:
                bank_id = resolve_ingest_bank_id(self._config, principal=p)
            except IngestError as e:
                logger.warning("github poll %s skip issue %s: %s", self._source_id, iid, e)
                continue

            profile = self._config.extraction_profile
            ctx = AstrocyteContext(principal=p) if p else None
            metadata = {
                "github": {
                    "issue_id": iid,
                    "number": n,
                    "html_url": u,
                    "updated_at": upd,
                    "author": login,
                }
            }

            await self._retain(
                text,
                bank_id,
                metadata=metadata,
                content_type="text",
                extraction_profile=profile,
                source=self._source_id,
                context=ctx,
            )
            self._seen_updated[iid] = upd

        if cursor_max:
            self._since = cursor_max
        self._last_error = None
