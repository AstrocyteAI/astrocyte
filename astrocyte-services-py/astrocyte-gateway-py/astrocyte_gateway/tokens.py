"""Per-user gateway tokens: the hashed registry file and the CLI that edits it.

Each token is bound to one principal, optional ``team:`` groups and optional
bank grants. The registry file holds only a SHA-256 hash of each token; the
plaintext is printed once, by ``create``, and never stored.

    python -m astrocyte_gateway.tokens create --principal user:alice \\
        --banks 'project:*' --permissions read,write --file tokens.yaml
    python -m astrocyte_gateway.tokens list --file tokens.yaml
    python -m astrocyte_gateway.tokens revoke <id> --file tokens.yaml

The gateway reads the file named by ``ASTROCYTE_TOKENS_FILE`` when
``ASTROCYTE_AUTH_MODE=token`` and re-reads it when it changes, so a revoke
takes effect on the next request without a restart.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from astrocyte.types import AccessGrant, AstrocyteContext

__all__ = [
    "TOKENS_FILE_ENV",
    "TOKEN_PREFIX",
    "TokenGrant",
    "TokenRecord",
    "TokenRegistry",
    "TokenRegistryError",
    "create_token",
    "hash_token",
    "load_records",
    "main",
    "registry_for",
    "revoke_token",
]

TOKENS_FILE_ENV = "ASTROCYTE_TOKENS_FILE"
TOKEN_PREFIX = "astk_"
_HASH_PREFIX = "sha256:"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Bank ids are [a-zA-Z0-9._:@-]; grants may add fnmatch characters.
_BANK_PATTERN_RE = re.compile(r"^[A-Za-z0-9._:@*?\[\]!-]{1,255}$")
_PRINCIPAL_RE = re.compile(r"^(user|agent|service):[A-Za-z0-9._:@-]{1,255}$")
_GROUP_RE = re.compile(r"^team:[A-Za-z0-9._:@-]{1,255}$")
_FILE_HEADER = (
    "# Astrocyte gateway tokens. Managed by `python -m astrocyte_gateway.tokens`.\n"
    "# Holds SHA-256 hashes only; a token's plaintext is shown once, at creation.\n"
)


class TokenRegistryError(Exception):
    """The registry file is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class TokenGrant:
    bank_id: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class TokenRecord:
    id: str
    token_hash: str
    principal: str
    groups: tuple[str, ...] = ()
    grants: tuple[TokenGrant, ...] = ()
    label: str | None = None
    created_at: str | None = None
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @property
    def scoped(self) -> bool:
        """Carries bank grants or groups, which mean something only under access control."""
        return bool(self.grants or self.groups)

    def context(self) -> AstrocyteContext:
        """The authenticated context a request carrying this token runs as."""
        return AstrocyteContext(
            principal=self.principal,
            groups=list(self.groups) or None,
            grants=[
                AccessGrant(bank_id=g.bank_id, principal=self.principal, permissions=list(g.permissions))
                for g in self.grants
            ]
            or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "principal": self.principal,
            "hash": self.token_hash,
            "groups": list(self.groups),
            "grants": [{"bank_id": g.bank_id, "permissions": list(g.permissions)} for g in self.grants],
            "label": self.label,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
        }


def hash_token(token: str) -> str:
    """Registry form of a token. Tokens are 256-bit random, so an unsalted hash is enough."""
    return _HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_principal(principal: str) -> str:
    if not _PRINCIPAL_RE.match(principal):
        raise ValueError(
            f"principal {principal!r} must be user:<id>, agent:<id> or service:<id> "
            "(no wildcards; groups go in --groups)"
        )
    return principal


def _validate_group(group: str) -> str:
    if not _GROUP_RE.match(group):
        raise ValueError(f"group {group!r} must be team:<name>")
    return group


def _validate_bank_pattern(bank_id: str) -> str:
    if not _BANK_PATTERN_RE.match(bank_id):
        raise ValueError(f"bank {bank_id!r} is not a bank id or glob pattern")
    return bank_id


def _record_from_dict(row: Any, idx: int) -> TokenRecord:
    where = f"tokens[{idx}]"
    if not isinstance(row, dict):
        raise TokenRegistryError(f"{where} must be a mapping")
    for key in ("id", "principal", "hash"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise TokenRegistryError(f"{where} is missing {key!r}")
    if not _HASH_RE.match(row["hash"]):
        raise TokenRegistryError(f"{where}.hash must be sha256:<64 hex chars>")
    try:
        principal = _validate_principal(row["principal"])
        groups = tuple(_validate_group(str(g)) for g in row.get("groups") or ())
        grants = []
        for g in row.get("grants") or ():
            if not isinstance(g, dict) or not isinstance(g.get("permissions"), list):
                raise ValueError("each grant needs bank_id and a permissions list")
            bank_id = _validate_bank_pattern(str(g.get("bank_id", "")))
            perms = tuple(str(p) for p in g["permissions"])
            AccessGrant(bank_id=bank_id, principal=principal, permissions=list(perms))
            grants.append(TokenGrant(bank_id=bank_id, permissions=perms))
    except ValueError as e:
        raise TokenRegistryError(f"{where}: {e}") from e
    revoked_at = row.get("revoked_at")
    created_at = row.get("created_at")
    return TokenRecord(
        id=row["id"],
        token_hash=row["hash"],
        principal=principal,
        groups=groups,
        grants=tuple(grants),
        label=row.get("label"),
        created_at=str(created_at) if created_at is not None else None,
        revoked_at=str(revoked_at) if revoked_at is not None else None,
    )


def load_records(path: str | Path) -> list[TokenRecord]:
    """Parse a registry file (YAML, or JSON, which YAML reads too)."""
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise TokenRegistryError(f"token registry {p} does not exist") from e
    except (OSError, yaml.YAMLError) as e:
        raise TokenRegistryError(f"token registry {p} could not be read: {e}") from e
    if data is None:
        data = {}
    rows = data.get("tokens") if isinstance(data, dict) else None
    if rows is None and isinstance(data, dict):
        rows = []
    if not isinstance(rows, list):
        raise TokenRegistryError(f"token registry {p} must be a mapping with a 'tokens' list")
    records = [_record_from_dict(row, i) for i, row in enumerate(rows)]
    ids = [r.id for r in records]
    if len(ids) != len(set(ids)):
        raise TokenRegistryError(f"token registry {p} has duplicate token ids")
    return records


def _write_records(path: Path, records: list[TokenRecord]) -> None:
    payload = {"tokens": [r.to_dict() for r in records]}
    if path.suffix.lower() == ".json":
        text = json.dumps(payload, indent=2) + "\n"
    else:
        text = _FILE_HEADER + yaml.safe_dump(payload, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace, so the gateway never reads a half-written registry.
    fd, tmp = tempfile.mkstemp(prefix=".tokens-", dir=path.parent)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_token(
    path: str | Path,
    *,
    principal: str,
    banks: list[str] | None = None,
    permissions: list[str] | None = None,
    groups: list[str] | None = None,
    label: str | None = None,
) -> tuple[str, TokenRecord]:
    """Mint a token, append its hash to the registry, and return ``(plaintext, record)``."""
    p = Path(path)
    records = load_records(p) if p.exists() else []
    _validate_principal(principal)
    group_t = tuple(_validate_group(g) for g in groups or ())
    perms = tuple(permissions or ("read", "write"))
    grants = []
    for bank in banks or ():
        AccessGrant(bank_id=_validate_bank_pattern(bank), principal=principal, permissions=list(perms))
        grants.append(TokenGrant(bank_id=bank, permissions=perms))
    taken = {r.id for r in records}
    token_id = secrets.token_hex(4)
    while token_id in taken:  # pragma: no cover - 32-bit collision
        token_id = secrets.token_hex(4)
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    record = TokenRecord(
        id=token_id,
        token_hash=hash_token(plaintext),
        principal=principal,
        groups=group_t,
        grants=tuple(grants),
        label=label,
        created_at=_now(),
    )
    _write_records(p, [*records, record])
    return plaintext, record


def revoke_token(path: str | Path, token_id: str) -> TokenRecord:
    """Mark a token revoked. The row stays, so ``list`` keeps an audit trail."""
    p = Path(path)
    records = load_records(p)
    for i, r in enumerate(records):
        if r.id != token_id:
            continue
        if not r.active:
            raise ValueError(f"token {token_id} is already revoked")
        revoked = replace(r, revoked_at=_now())
        records[i] = revoked
        _write_records(p, records)
        return revoked
    raise ValueError(f"no token with id {token_id}")


@dataclass
class TokenRegistry:
    """Token lookup over a registry file, reloaded whenever the file changes."""

    path: Path
    _stamp: tuple[int, int, int] | None = field(default=None, init=False)
    _records: list[TokenRecord] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _refresh(self) -> list[TokenRecord]:
        try:
            st = self.path.stat()
        except OSError as e:
            raise TokenRegistryError(f"token registry {self.path} is not readable: {e}") from e
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self._lock:
            if stamp != self._stamp:
                self._records = [r for r in load_records(self.path) if r.active]
                self._stamp = stamp
            return self._records

    def load(self) -> int:
        """Load now (fail fast at startup); returns the number of active tokens."""
        return len(self._refresh())

    def active_records(self) -> list[TokenRecord]:
        return list(self._refresh())

    def lookup(self, token: str) -> TokenRecord | None:
        """The active record for ``token``, or ``None``. Compares every hash in constant time."""
        presented = hash_token(token)
        match: TokenRecord | None = None
        for r in self._refresh():
            if hmac.compare_digest(r.token_hash, presented):
                match = r
        return match


_registries: dict[Path, TokenRegistry] = {}
_registries_lock = threading.Lock()


def registry_for(path: str | Path) -> TokenRegistry:
    """Process-wide registry for ``path`` (one per file, so reload state is shared)."""
    p = Path(path).expanduser().resolve()
    with _registries_lock:
        reg = _registries.get(p)
        if reg is None:
            reg = _registries[p] = TokenRegistry(p)
        return reg


def _registry_path(arg: str | None) -> Path:
    raw = arg or os.environ.get(TOKENS_FILE_ENV, "").strip()
    if not raw:
        raise SystemExit(f"error: pass --file or set {TOKENS_FILE_ENV}")
    return Path(raw).expanduser()


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _describe(r: TokenRecord) -> str:
    grants = "; ".join(f"{g.bank_id}={','.join(g.permissions)}" for g in r.grants) or "-"
    status = f"revoked {r.revoked_at}" if r.revoked_at else "active"
    parts = [r.id, r.principal, f"groups={','.join(r.groups) or '-'}", f"grants={grants}", status]
    if r.label:
        parts.append(f"label={r.label}")
    return "  ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m astrocyte_gateway.tokens",
        description="Manage per-user gateway tokens (ASTROCYTE_AUTH_MODE=token).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _file_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--file", help=f"registry file (default: ${TOKENS_FILE_ENV})")

    create = sub.add_parser("create", help="mint a token; prints it once")
    create.add_argument("--principal", required=True, help="user:<id>, agent:<id> or service:<id>")
    create.add_argument("--banks", help="comma-separated bank ids or globs, e.g. 'project:*'")
    create.add_argument(
        "--permissions", default="read,write", help="comma-separated: read,write,forget,admin (default read,write)"
    )
    create.add_argument("--groups", help="comma-separated team:<name> groups")
    create.add_argument("--label", help="free-text note, e.g. whose laptop")
    _file_arg(create)

    lst = sub.add_parser("list", help="list tokens (never shows plaintext)")
    lst.add_argument("--all", action="store_true", help="include revoked tokens")
    _file_arg(lst)

    revoke = sub.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("token_id")
    _file_arg(revoke)

    args = parser.parse_args(argv)
    path = _registry_path(args.file)
    try:
        if args.command == "create":
            plaintext, record = create_token(
                path,
                principal=args.principal,
                banks=_csv(args.banks),
                permissions=_csv(args.permissions),
                groups=_csv(args.groups),
                label=args.label,
            )
            print(f"created {_describe(record)}", file=sys.stderr)
            print("store this token now; it is not saved and cannot be shown again:", file=sys.stderr)
            print(plaintext)
        elif args.command == "list":
            records = load_records(path) if path.exists() else []
            for r in records:
                if r.active or args.all:
                    print(_describe(r))
        else:
            record = revoke_token(path, args.token_id)
            print(f"revoked {_describe(record)}", file=sys.stderr)
    except (TokenRegistryError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
