#!/bin/bash
# Create a console user with a username and password, or set a new password for one.
#   ./scripts/add_console_user.sh <username> [admin|viewer]
# It asks for the password without showing it. Run on the machine the service runs on.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -ge 1 ] || { echo "Usage: $0 <username> [admin|viewer]"; exit 1; }
exec .venv/bin/python -m app.adduser "$@"
