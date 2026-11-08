# ios-auto-deploy

Keeps the iPhone's copy of every Flutter app in `agent-coding` current with the
latest commit, over Wi-Fi, with no cable and nothing to tap on the phone. Built
2026-11-07 because updating the meditation timer, the EMA sampler and the
exercise tracker meant remembering to run `flutter run` or `tool/phone.sh deploy`
for each one after every change.

## How it works

A launchd agent (`com.elityre.ios-auto-deploy`) runs `watch.py` forever. Every
`POLL_SECONDS` (default 2 min) it does one *tick*:

1. **Find apps.** Every folder under `PROJECTS_ROOT` that contains an
   `ios-deploy.conf` (plus `.git` and `pubspec.yaml`) is an app to keep updated.
   Adding that file to a new Flutter project is the whole onboarding.
2. **Compare commits.** The working tree's HEAD commit is compared with the
   commit recorded in `state.json` as last installed. Only *committed* work is
   deployed, so a half-edited tree never reaches the phone. Equal: nothing to do.
3. **Check the phone.** `xcrun devicectl device info apps` must answer. The
   phone is paired to this Mac over Wi-Fi in Xcode (Developer Mode on), so it is
   reachable whenever it is on the same network and awake-ish. Unreachable: try
   again next tick (logged once per change of reachability).
4. **Don't kill a session.** If the app's process exists on the phone
   (foreground *or* suspended in the background; `devicectl` can't tell them
   apart) the install waits, up to `DEFER_RUNNING_MINUTES` (default 2 h), then
   goes ahead anyway. iOS keeps suspended apps around for a while, so a
   frequently used app typically updates with a delay of minutes to an hour or
   two. `watch.py deploy NAME --force` skips the wait.
5. **Build from a clean checkout.** The project is cloned once into
   `checkouts/<name>` and reset to the exact HEAD commit for each build, so the
   build never sees uncommitted edits and never touches the project's own
   `build/` directory. Gitignored files the build needs (`.env`, `lib/config.dart`)
   are listed in `ios-deploy.conf` as `COPY_UNTRACKED` and copied in. Then
   `flutter build ios --release --build-number=<commit count> $BUILD_ARGS`.
   The build number on the phone therefore equals `git rev-list --count HEAD`,
   which is how you can tell which commit the phone has.
6. **Install in place.** `xcrun devicectl device install app` over Wi-Fi. iOS
   treats this as an app update: data container, settings, home-screen
   position all stay. (Never `flutter install` after an uninstall; that wipes
   data.) The app list is re-read to confirm the new build number, the state is
   saved, and if `NTFY_TOPIC` is set a phone notification says what changed
   (the commit subject). A failed build is logged and notified once, then not
   retried until a new commit lands or you run `watch.py deploy NAME`.

Signing uses the team already set in each project's `Runner.xcodeproj`
(automatic signing, paid team, profile valid to 2027-08). The phone already
trusts it, so new apps install without a trip to Device Management.

## Files

| File | Purpose |
|---|---|
| `watch.py` | The whole tool: loop, `once`, `status`, `deploy NAME [--force]` |
| `install.sh` | Writes the launchd plist and (re)starts the agent; seeds config. Idempotent. |
| `uninstall.sh` | Stops and removes the agent (keeps config, state, checkouts, logs) |
| `config.env.example` | All settings with defaults; copied to Application Support on first install |
| `<project>/ios-deploy.conf` | Lives in each app's repo. Presence opts the app in. Keys: `BUILD_ARGS`, `COPY_UNTRACKED`, `BUNDLE_ID` (default: read from `project.pbxproj`) |

## Runtime locations

- Config: `~/Library/Application Support/IOSAutoDeploy/config.env`
- State (last deployed commit per app, failures, deferrals): `~/Library/Application Support/IOSAutoDeploy/state.json`
- Clean checkouts used for building: `~/Library/Application Support/IOSAutoDeploy/checkouts/<project>/`
- Full `flutter build` + install output, one file per attempt: `~/Library/Application Support/IOSAutoDeploy/builds/`
- Summary log: `~/Library/Logs/ios-auto-deploy.log`
- Agent: `~/Library/LaunchAgents/com.elityre.ios-auto-deploy.plist`

## Common tasks

```bash
cd ~/Documents/agent-coding/ios-auto-deploy
./install.sh                                  # install or restart the agent (waits for an in-flight deploy)
./uninstall.sh                                # stop it
python3 watch.py status                       # committed vs deployed vs what the phone reports, per app
python3 watch.py deploy meditation-app        # push HEAD now (end-to-end test); --force to ignore "app is running"
python3 watch.py once                         # one tick by hand, same logic as the agent
tail -f ~/Library/Logs/ios-auto-deploy.log
ls -t ~/Library/Application\ Support/IOSAutoDeploy/builds | head   # per-build logs
xcrun devicectl list devices                  # is the phone paired and available?
xcrun devicectl device info apps --device <UDID> | grep elityre   # versions on the phone
```

Onboard a new Flutter iPhone app: set the signing team once in Xcode (Runner
target, Signing & Capabilities, automatic), make sure it builds with
`flutter build ios --release`, add an `ios-deploy.conf` (empty is fine; list
gitignored build inputs under `COPY_UNTRACKED`), commit. The next tick installs it.

Stop updating one app: delete its `ios-deploy.conf`. Stop everything:
`./uninstall.sh`.

## Settings that matter

- Xcode: the iPhone is paired with **Connect via network** on (Window, Devices
  and Simulators). `xcrun devicectl list devices` should show it as `available
  (paired)` with transport `localNetwork` when no cable is attached.
- iPhone: Developer Mode on (Settings, Privacy & Security). Same Wi-Fi as the Mac.
- Each app's `Runner.xcodeproj`: automatic signing with the paid team. With a
  free personal team the install would expire after 7 days; the poller would
  not notice, so a weekly commit (or `deploy NAME`) would be needed.
- Mac: the agent runs in the GUI login session, so it only works while Eli is
  logged in (Xcode signing needs the login keychain). Builds also need the Mac
  awake; a closed lid pauses them until it wakes.

## Gotchas learned

- `devicectl device info processes` lists *suspended* apps too, so "running"
  really means "has not been jettisoned yet". That is why there is a 2-hour cap
  rather than waiting for the process to disappear.
- Every Flutter app's binary is `Runner.app/Runner`; processes are matched to
  an app by its install folder URL from `device info apps`, not by name.
- Build from a clean checkout, not the working tree: a build picks up whatever
  is on disk, and other sessions leave projects with dozens of uncommitted
  files. The checkout is reset with `git clean` but keeps `build/`, `.dart_tool`,
  `Pods` and friends so rebuilds stay warm.
- `git fetch origin HEAD` is run as well as `git fetch origin`, so a project
  left on a detached HEAD still deploys its current commit.
- Not `/usr/bin/python3` under launchd: it is denied access to `~/Documents`
  (see `agent-coding/CLAUDE.md`). `/opt/homebrew/bin/python3` is used.
- `install.sh` waits for the deploy lock before `bootout`, because killing
  `devicectl install` mid-transfer is the one way to leave the phone's app in a
  bad state.
- Only one copy runs at a time (`flock` on `IOSAutoDeploy/lock`). A manual
  `deploy` while the agent is mid-build prints "waiting" and then runs.
