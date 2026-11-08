#!/bin/bash
# Installs (or reinstalls) the launchd agent com.elityre.ios-auto-deploy, which runs watch.py as a
# long-lived loop. Idempotent: rerun after editing watch.py or the config.
#   config: ~/Library/Application Support/IOSAutoDeploy/config.env (seeded from config.env.example)
#   log:    ~/Library/Logs/ios-auto-deploy.log
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.elityre.ios-auto-deploy
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_SUPPORT="$HOME/Library/Application Support/IOSAutoDeploy"
LOG="$HOME/Library/Logs/ios-auto-deploy.log"
# Not /usr/bin/python3: launchd denies that binary access to ~/Documents (see agent-coding/CLAUDE.md).
PY=/opt/homebrew/bin/python3

mkdir -p "$APP_SUPPORT" "$HOME/Library/Logs" "$HOME/Library/LaunchAgents"
if [ ! -f "$APP_SUPPORT/config.env" ]; then
    cp "$HERE/config.env.example" "$APP_SUPPORT/config.env"
    echo "Created $APP_SUPPORT/config.env from the example (defaults work; edit to add an ntfy topic)."
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <!-- Builds and installs new commits of the Flutter iPhone apps onto the phone over Wi-Fi.
         Reinstall: ./install.sh      Remove: ./uninstall.sh      Log: $LOG -->
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$HERE/watch.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HERE</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST

# A deploy in flight would be killed by bootout; wait for it rather than leave a half-installed app.
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    $PY - "$APP_SUPPORT/lock" <<'PY' || true
import fcntl, sys
f = open(sys.argv[1], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("a deploy is in progress; waiting for it to finish before restarting the agent...")
    fcntl.flock(f, fcntl.LOCK_EX)
PY
fi
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed $LABEL. Log: $LOG"
launchctl print "gui/$(id -u)/$LABEL" | grep -E '^\s+(state|pid)' || true
