#!/opt/homebrew/bin/python3
"""ios-auto-deploy: keeps the iPhone's copy of each Flutter app current with its
latest commit, over Wi-Fi, with no cable and nothing to tap on the phone.

Mechanism (one "tick", run every POLL_SECONDS by launchd com.elityre.ios-auto-deploy):
  1. Find every project under PROJECTS_ROOT that contains an `ios-deploy.conf`.
  2. For each, compare the working tree's HEAD commit with the commit last
     installed on the phone (state.json). Equal -> nothing to do.
  3. If the phone is reachable (paired over Wi-Fi in Xcode, Developer Mode on)
     and the app is not currently running on it, build a release from a clean
     checkout of that commit and install it IN PLACE with `xcrun devicectl`.
     In-place install = an app update: data, settings and icon position stay.
  4. If the app is running (foreground, or suspended in the background), wait
     and retry each tick, up to DEFER_RUNNING_MINUTES, then install anyway.

Config: ~/Library/Application Support/IOSAutoDeploy/config.env  (all keys optional)
State:  ~/Library/Application Support/IOSAutoDeploy/state.json
Clean checkouts and full build logs: ~/Library/Application Support/IOSAutoDeploy/{checkouts,builds}
Log:    ~/Library/Logs/ios-auto-deploy.log

Usage:
  watch.py                    run forever (what launchd does)
  watch.py once               one tick, then exit
  watch.py status             what is committed vs what is on the phone
  watch.py deploy NAME [--force]   build + install NAME's HEAD now; --force ignores "app is running"
"""
import datetime
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

APP_SUPPORT = Path.home() / "Library/Application Support/IOSAutoDeploy"
CONFIG = APP_SUPPORT / "config.env"
STATE = APP_SUPPORT / "state.json"
CHECKOUTS = APP_SUPPORT / "checkouts"
BUILD_LOGS = APP_SUPPORT / "builds"
LOCK = APP_SUPPORT / "lock"
LOG = Path.home() / "Library/Logs/ios-auto-deploy.log"

DEFAULTS = {
    "PROJECTS_ROOT": str(Path.home() / "Documents/agent-coding"),
    "DEVICE_UDID": "",            # empty = the only paired physical iPhone
    "POLL_SECONDS": "120",
    "DEFER_RUNNING_MINUTES": "120",
    "NTFY_SERVER": "https://ntfy.sh",
    "NTFY_TOPIC": "",             # empty = no phone notifications
    "BUILD_TIMEOUT_MINUTES": "40",
}

# launchd gives a bare PATH; flutter, pod and git live in Homebrew.
ENV = dict(os.environ)
ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + ENV.get("PATH", "")
ENV.setdefault("LANG", "en_US.UTF-8")


def now():
    return datetime.datetime.now()


def log(msg):
    line = f"{now():%Y-%m-%d %H:%M:%S} {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    if sys.stdout.isatty():
        print(line, flush=True)


def read_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        for raw in CONFIG.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = os.path.expanduser(v.strip().strip('"').strip("'"))
    return cfg


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE)


def run(cmd, cwd=None, timeout=120, log_file=None):
    """Run a command; return (returncode, combined output). Output also appended to log_file."""
    p = subprocess.run(cmd, cwd=cwd, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=timeout)
    if log_file:
        with open(log_file, "a") as f:
            f.write(f"\n$ {shlex.join(str(c) for c in cmd)}\n{p.stdout}")
    return p.returncode, p.stdout


def git(repo, *args):
    rc, out = run(["git", "-C", str(repo), *args])
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {out.strip()}")
    return out.strip()


# ---------------------------------------------------------------- projects

class Project:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.build_args = []
        self.copy_untracked = []
        self.bundle_id = ""
        for raw in (self.path / "ios-deploy.conf").read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "BUILD_ARGS":
                self.build_args = shlex.split(v)
            elif k == "COPY_UNTRACKED":
                self.copy_untracked = v.split()
            elif k == "BUNDLE_ID":
                self.bundle_id = v
        if not self.bundle_id:
            self.bundle_id = self._bundle_from_pbxproj()

    def _bundle_from_pbxproj(self):
        pbx = self.path / "ios/Runner.xcodeproj/project.pbxproj"
        for line in pbx.read_text().splitlines():
            if "PRODUCT_BUNDLE_IDENTIFIER" in line and "RunnerTests" not in line:
                return line.split("=", 1)[1].strip().rstrip(";").strip('"')
        raise RuntimeError(f"{self.name}: no PRODUCT_BUNDLE_IDENTIFIER in {pbx}; set BUNDLE_ID in ios-deploy.conf")

    def head(self):
        sha = git(self.path, "rev-parse", "HEAD")
        subject = git(self.path, "log", "-1", "--format=%s")
        count = int(git(self.path, "rev-list", "--count", "HEAD"))
        return sha, subject, count


def discover(cfg):
    root = Path(cfg["PROJECTS_ROOT"])
    found = []
    for d in sorted(root.iterdir()):
        if (d / "ios-deploy.conf").is_file() and (d / ".git").exists() and (d / "pubspec.yaml").is_file():
            try:
                found.append(Project(d))
            except Exception as e:  # a broken conf must not stop the other apps
                log(f"{d.name}: skipped: {e}")
    return found


# ---------------------------------------------------------------- phone

def devicectl_json(args, timeout=60):
    """Run `xcrun devicectl ... --json-output FILE` and return the parsed 'result', or None if the
    phone could not be reached."""
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=APP_SUPPORT)
    os.close(fd)
    try:
        rc, out = run(["xcrun", "devicectl", *args, "--json-output", tmp], timeout=timeout + 30)
        if rc != 0:
            return None
        return json.loads(Path(tmp).read_text()).get("result")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def resolve_device(cfg):
    if cfg["DEVICE_UDID"]:
        return cfg["DEVICE_UDID"]
    res = devicectl_json(["list", "devices"])
    if not res:
        raise RuntimeError("devicectl list devices failed")
    phones = [d for d in res["devices"]
              if d.get("hardwareProperties", {}).get("reality") == "physical"
              and d.get("connectionProperties", {}).get("pairingState") == "paired"]
    if len(phones) != 1:
        raise RuntimeError(f"expected exactly one paired physical device, found {len(phones)}; set DEVICE_UDID in {CONFIG}")
    return phones[0]["hardwareProperties"]["udid"]


def phone_apps(udid):
    """{bundle_id: app dict} for apps on the phone, or None if unreachable."""
    res = devicectl_json(["device", "info", "apps", "--device", udid, "--timeout", "30"])
    if res is None:
        return None
    return {a["bundleIdentifier"]: a for a in res.get("apps", [])}


def phone_running_executables(udid):
    res = devicectl_json(["device", "info", "processes", "--device", udid, "--timeout", "30"])
    if res is None:
        return None
    return [p.get("executable", "") for p in res.get("runningProcesses", [])]


def app_is_running(apps, procs, bundle_id):
    app = apps.get(bundle_id)
    if not app or procs is None:
        return False
    # Every Flutter app's binary is .../Runner.app/Runner, so match on the install folder.
    return any(p.startswith(app["url"]) for p in procs)


# ---------------------------------------------------------------- build + install

def build(project, sha, count, build_log):
    """Build a release .app from a clean checkout of `sha`. Returns the .app path."""
    co = CHECKOUTS / project.name
    CHECKOUTS.mkdir(parents=True, exist_ok=True)
    if not (co / ".git").exists():
        shutil.rmtree(co, ignore_errors=True)
        rc, out = run(["git", "clone", "-q", str(project.path), str(co)], timeout=600, log_file=build_log)
        if rc != 0:
            raise RuntimeError(f"git clone failed: {out.strip()[-500:]}")
    # Plain `fetch` brings branches; `fetch HEAD` also covers a detached HEAD in the source repo.
    git(co, "fetch", "-q", "origin")
    git(co, "fetch", "-q", "origin", "HEAD")
    git(co, "checkout", "-q", "--detach", sha)
    git(co, "clean", "-qfd", "--exclude=build", "--exclude=.dart_tool", "--exclude=ios/Pods",
        "--exclude=ios/.symlinks", "--exclude=ios/Flutter/ephemeral")
    for rel in project.copy_untracked:
        src = project.path / rel
        if not src.is_file():
            raise RuntimeError(f"COPY_UNTRACKED file missing in working tree: {src}")
        (co / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, co / rel)
    cmd = ["flutter", "build", "ios", "--release", f"--build-number={count}", *project.build_args]
    rc, out = run(cmd, cwd=co, timeout=int(read_config()["BUILD_TIMEOUT_MINUTES"]) * 60, log_file=build_log)
    if rc != 0:
        tail = "\n".join(out.strip().splitlines()[-15:])
        raise RuntimeError(f"flutter build failed (full log: {build_log}):\n{tail}")
    app = co / "build/ios/iphoneos/Runner.app"
    if not app.is_dir():
        raise RuntimeError(f"build produced no {app}")
    return app


def install(udid, app_path, build_log):
    rc, out = run(["xcrun", "devicectl", "device", "install", "app", "--device", udid,
                   "--timeout", "600", str(app_path)], timeout=660, log_file=build_log)
    if rc != 0:
        raise RuntimeError(f"devicectl install failed: {out.strip()[-500:]}")


def notify(cfg, title, message):
    if not cfg["NTFY_TOPIC"]:
        return
    try:
        req = urllib.request.Request(f"{cfg['NTFY_SERVER'].rstrip('/')}/{cfg['NTFY_TOPIC']}",
                                     data=message.encode(), method="POST",
                                     headers={"Title": title, "Tags": "iphone"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"ntfy failed: {e}")


# ---------------------------------------------------------------- the tick

def deploy_one(cfg, state, udid, apps, project, sha, subject, count):
    st = state.setdefault(project.name, {})
    BUILD_LOGS.mkdir(parents=True, exist_ok=True)
    build_log = BUILD_LOGS / f"{project.name}-{now():%Y%m%d-%H%M%S}-{sha[:7]}.log"
    display = apps.get(project.bundle_id, {}).get("name", project.name)
    log(f"{project.name}: deploying {sha[:7]} build {count} \"{subject}\" (log: {build_log.name})")
    t0 = time.time()
    try:
        app = build(project, sha, count, build_log)
        install(udid, app, build_log)
        after = phone_apps(udid) or {}
        got = after.get(project.bundle_id, {})
        if got.get("bundleVersion") != str(count):
            raise RuntimeError(f"installed but phone reports version {got.get('version')} build {got.get('bundleVersion')}, expected build {count}")
    except Exception as e:
        st.update(failed_commit=sha, last_error=str(e)[:2000], failed_at=now().isoformat(timespec="seconds"))
        save_state(state)
        log(f"{project.name}: FAILED after {time.time()-t0:.0f}s: {e}")
        notify(cfg, f"{display}: update failed", f"{sha[:7]} {subject}\n{str(e)[:300]}")
        return False
    st.pop("failed_commit", None); st.pop("last_error", None); st.pop("failed_at", None)
    st.pop("pending_since", None); st.pop("deferred_logged", None)
    st.update(deployed_commit=sha, deployed_subject=subject, build_number=count,
              deployed_at=now().isoformat(timespec="seconds"), version=got.get("version"))
    save_state(state)
    log(f"{project.name}: installed {got.get('version')} ({count}) in {time.time()-t0:.0f}s")
    notify(cfg, f"{display} updated on your phone", f"{got.get('version')} build {count}: {subject}")
    return True


def tick(cfg, state, only=None, force=False):
    projects = discover(cfg)
    if only:
        projects = [p for p in projects if p.name == only]
        if not projects:
            raise SystemExit(f"no project named {only!r} with an ios-deploy.conf under {cfg['PROJECTS_ROOT']}")
    todo = []
    for p in projects:
        try:
            sha, subject, count = p.head()
        except Exception as e:
            log(f"{p.name}: cannot read HEAD: {e}")
            continue
        st = state.get(p.name, {})
        if not only and st.get("deployed_commit") == sha:
            continue
        if not only and st.get("failed_commit") == sha:
            continue  # already tried and logged; a new commit or `deploy NAME` retries
        todo.append((p, sha, subject, count))
    if not todo:
        return
    udid = resolve_device(cfg)
    apps = phone_apps(udid)
    reachable = apps is not None
    if state.get("_phone_reachable") != reachable:
        state["_phone_reachable"] = reachable
        save_state(state)
        log("phone reachable" if reachable else "phone not reachable (off Wi-Fi, asleep too long, or unpaired); will keep trying")
    if not reachable:
        return
    procs = None
    for p, sha, subject, count in todo:
        st = state.setdefault(p.name, {})
        if not force and p.bundle_id in apps:
            if procs is None:
                procs = phone_running_executables(udid)
            if app_is_running(apps, procs, p.bundle_id):
                since = st.get("pending_since")
                if not since:
                    since = now().isoformat(timespec="seconds")
                    st["pending_since"] = since
                    save_state(state)
                waited = (now() - datetime.datetime.fromisoformat(since)).total_seconds() / 60
                if waited < float(cfg["DEFER_RUNNING_MINUTES"]):
                    if st.get("deferred_logged") != sha:
                        st["deferred_logged"] = sha
                        save_state(state)
                        log(f"{p.name}: {sha[:7]} ready but the app is running on the phone; waiting (up to {cfg['DEFER_RUNNING_MINUTES']} min)")
                    continue
                log(f"{p.name}: app still running after {waited:.0f} min; installing anyway")
        deploy_one(cfg, state, udid, apps, p, sha, subject, count)


def status(cfg, state):
    projects = discover(cfg)
    apps = None
    procs = None
    try:
        udid = resolve_device(cfg)
        apps = phone_apps(udid)
        procs = phone_running_executables(udid) if apps is not None else None
    except Exception as e:
        print(f"phone: {e}")
    print("phone:", "reachable" if apps is not None else "NOT reachable")
    for p in projects:
        sha, subject, count = p.head()
        st = state.get(p.name, {})
        dep = st.get("deployed_commit", "")
        onphone = (apps or {}).get(p.bundle_id)
        line = f"{p.name}: HEAD {sha[:7]} (#{count}) \"{subject[:50]}\" | deployed "
        line += f"{dep[:7]} at {st.get('deployed_at')}" if dep else "never"
        if onphone:
            line += f" | phone has {onphone['version']} ({onphone['bundleVersion']})"
            line += ", running" if app_is_running(apps, procs, p.bundle_id) else ""
        elif apps is not None:
            line += " | not installed on phone"
        if dep and dep != sha:
            line += " | UPDATE PENDING"
        if st.get("failed_commit") == sha:
            line += f" | LAST BUILD FAILED: {st.get('last_error','')[:120]}"
        print(line)


def main(argv):
    cfg = read_config()
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    cmd = argv[1] if len(argv) > 1 else "loop"
    if cmd in ("-h", "--help"):
        print(__doc__)
        return
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if cmd == "status":
            lock = None
        else:
            print("another ios-auto-deploy is mid-deploy (probably the launchd agent); waiting for it...", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
    state = load_state()
    if cmd == "status":
        status(cfg, state)
    elif cmd == "once":
        tick(cfg, state)
    elif cmd == "deploy":
        if len(argv) < 3:
            raise SystemExit("usage: watch.py deploy PROJECT_NAME [--force]")
        tick(cfg, state, only=argv[2], force="--force" in argv)
    elif cmd == "loop":
        log(f"started (poll every {cfg['POLL_SECONDS']}s, projects under {cfg['PROJECTS_ROOT']})")
        while True:
            try:
                tick(cfg, load_state())
            except Exception as e:
                log(f"tick failed: {e}")
            time.sleep(int(cfg["POLL_SECONDS"]))
            cfg = read_config()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
