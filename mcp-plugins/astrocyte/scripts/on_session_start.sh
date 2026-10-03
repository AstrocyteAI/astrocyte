#!/usr/bin/env bash
# Astrocyte plugin — session-start check.
#
# The memory server is registered by `astrocyte setup`, not by this plugin
# (one verified registration per agent, repairable by `astrocyte doctor
# --fix`). This hook only makes a missing install visible at the moment it
# matters, instead of the first memory tool call failing silently.
#
# Exit 0 always — a degraded hook must never block the session.

set -u

if ! command -v astrocyte >/dev/null 2>&1; then
    echo "Astrocyte: not installed. Run: uv tool install 'astrocyte[local]' && astrocyte setup" >&2
    exit 0
fi

config="${ASTROCYTE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/astrocyte/astrocyte.yaml}"
if [[ ! -f "$config" ]]; then
    echo "Astrocyte: installed but not set up. Run: astrocyte setup" >&2
fi

exit 0
