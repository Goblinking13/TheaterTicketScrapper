#!/bin/bash
# Install a single user service for flights and theater observations.
set -e
cd "$(dirname "$0")/.."
project_path="$PWD"
.venv/bin/python scripts/prepare-local.py
runtime_path="$HOME/Library/Application Support/TicketCollector"
cd "$runtime_path"
.venv/bin/python -m flightwatch.local_scheduler --write-plist "$project_path/.private/com.flightwatch.local.plist"
mkdir -p "$HOME/Library/LaunchAgents"
agent_path="$HOME/Library/LaunchAgents/com.flightwatch.local.plist"
service_domain="gui/$(id -u)"
if launchctl print "$service_domain/com.flightwatch.local" >/dev/null 2>&1; then
  launchctl bootout "$service_domain/com.flightwatch.local"
fi
cp "$project_path/.private/com.flightwatch.local.plist" "$agent_path"
launchctl bootstrap "$service_domain" "$agent_path"
echo "Installed unified collector. Logs: $runtime_path/state/logs/"
