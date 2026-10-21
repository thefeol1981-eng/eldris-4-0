#!/usr/bin/env sh
# Publish ELDRIS 3.0 to a free public HTTPS address (Cloudflare quick tunnel).
cd "$(dirname "$0")" || exit 1
exec python3 tools/publish_free.py "$@"
