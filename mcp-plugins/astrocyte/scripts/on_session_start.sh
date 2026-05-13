#!/usr/bin/env bash
# Astrocyte plugin — session-start hook.
#
# v0.1.0 does the minimum: log to stderr that the plugin loaded, and
# emit a hint to stdout if ASTROCYTE_CONFIG is unset (in which case
# the MCP server would fail to start on first tool call).
#
# Future versions will additionally:
#   - call ``astrocyte-mcp --probe`` to verify the bank is reachable
#   - inject a one-line "context loaded" status with the current bank_id
#   - pre-fetch recent memories for the configured default bank
#
# Exit 0 always — a degraded hook must never block the session.

set -u

if [[ -z "${ASTROCYTE_CONFIG:-}" ]]; then
    echo "  Astrocyte: ASTROCYTE_CONFIG env var unset — set it to your astrocyte.yaml path." >&2
    echo "  Astrocyte: example: export ASTROCYTE_CONFIG=\$HOME/.config/astrocyte/astrocyte.yaml" >&2
fi

# Probe ``uvx --from astrocyte-stack astrocyte-mcp --version`` would be ideal
# but costs a cold cache hit (~3-5s). Defer to first tool use.

exit 0
