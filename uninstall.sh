#!/bin/bash
# Stops and removes the launchd agent. Config, state, checkouts and logs are left in place.
LABEL=com.elityre.ios-auto-deploy
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL"
