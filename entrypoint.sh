#!/bin/sh
set -e

# Default: start web UI + background sync
exec python main.py "$@"
