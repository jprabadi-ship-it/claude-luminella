"""Check GitHub for a newer build, and swap this one for it.

Everything that can fail happens before anything is replaced: the release is
fetched, the disk image mounted, the bundle inside it checked against this
app's own signing identity, and a copy staged in a temporary folder. Only then
does the running app quit and the copy move into place.

The signature check is the part that matters. The app downloads code and runs
it, so it accepts a build only if Gatekeeper passes it and it carries the same
Team ID as the copy already installed -- a notarised build from anyone else is
still refused.
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import urllib.request

API = "https://api.github.com/repos/jprabadi-ship-it/claude-luminella/releases/latest"
APP_NAME = "Clauminella.app"
TIMEOUT = 20

# Where the version being replaced is kept, so there is a way back from a bad
# update. Deliberately not beside the installed app: /Applications is for the
# one copy people are meant to run, and a second bundle there is something to
# open by mistake.
KEEP_DIR = os.path.join(os.path.expanduser("~"), ".claude", "luminella", "previous")
KEPT_APP = os.path.join(KEEP_DIR, APP_NAME)


def _run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, check=False)


def app_path():
    """Where this bundle lives, or None when running from source."""
    resources = os.environ.get("RESOURCEPATH")
    if not resources:
        return None
    # .../Clauminella.app/Contents/Resources
    path = os.path.dirname(os.path.dirname(resources))
    return path if path.endswith(".app") else None


def installed_version(path=None):
    path = path or app_path()
    if not path:
        return None
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, ValueError):
        return None


def team_id(path):
    """The Team ID a bundle is signed with, or None."""
    out = _run(["/usr/bin/codesign", "-dv", "--verbose=2", path], timeout=60)
    found = re.search(r"TeamIdentifier=(.+)", (out.stderr or "") + (out.stdout or ""))
    if not found:
        return None
    # Apple's own bundles say "not set", whose first word is a plausible-looking
    # team id and is not one.
    value = found.group(1).strip()
    return None if value.lower() == "not set" else value


def _parts(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def is_newer(remote, local):
    """Whether remote is a later version than local, by number not by string."""
    if not remote or not local:
        return False
    return _parts(remote) > _parts(local)


def latest_release():
    """{'version', 'url', 'notes'} for the newest release, or None."""
    request = urllib.request.Request(
        API, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": "Clauminella"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.load(response)
    except Exception:
        return None
    tag = (data.get("tag_name") or "").lstrip("v")
    asset = next((a for a in data.get("assets") or []
                  if (a.get("name") or "").endswith(".dmg")), None)
    if not tag or not asset:
        return None
    return {"version": tag,
            "url": asset.get("browser_download_url"),
            "name": data.get("name") or tag,
            "notes": data.get("body") or ""}


def stage(url, expect_team, log=print):
    """Download, verify, and lay the new bundle aside. Returns a path or None.

    Nothing installed is touched here; a failure anywhere leaves the running
    app exactly as it was.
    """
    work = tempfile.mkdtemp(prefix="clauminella-update-")
    dmg = os.path.join(work, "update.dmg")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Clauminella"})
        with urllib.request.urlopen(request, timeout=120) as response, \
                open(dmg, "wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:
        log("update: download failed: %r" % exc)
        shutil.rmtree(work, ignore_errors=True)
        return None

    mount = os.path.join(work, "mnt")
    os.makedirs(mount, exist_ok=True)
    attached = _run(["/usr/bin/hdiutil", "attach", dmg, "-nobrowse", "-quiet",
                     "-mountpoint", mount], timeout=180)
    if attached.returncode != 0:
        log("update: could not open the disk image: %s" % attached.stderr.strip()[:200])
        shutil.rmtree(work, ignore_errors=True)
        return None

    staged = None
    try:
        source = os.path.join(mount, APP_NAME)
        if not os.path.isdir(source):
            log("update: the disk image holds no %s" % APP_NAME)
            return None

        # Gatekeeper first: a build Apple has not notarised never gets copied.
        assess = _run(["/usr/sbin/spctl", "--assess", "--type", "execute", source],
                      timeout=120)
        if assess.returncode != 0:
            log("update: refused by Gatekeeper: %s" % assess.stderr.strip()[:200])
            return None

        # Then the identity: notarised says Apple checked it, not that it is ours.
        theirs = team_id(source)
        if not theirs or theirs != expect_team:
            log("update: signed by %s, expected %s" % (theirs, expect_team))
            return None

        staged = os.path.join(work, APP_NAME)
        shutil.copytree(source, staged, symlinks=True)
    finally:
        _run(["/usr/bin/hdiutil", "detach", mount, "-quiet"], timeout=120)

    if staged is None:
        shutil.rmtree(work, ignore_errors=True)
    return staged


SWAP = r"""#!/bin/bash
# Written by Clauminella to replace itself. Waits for the app to go, puts the
# new bundle in place, starts it again, and clears up after itself.
set -u
target=%(target)s
staged=%(staged)s
work=%(work)s
log=%(log)s

for _ in $(seq 1 100); do
  pgrep -f "$target/Contents/MacOS/" >/dev/null 2>&1 || break
  sleep 0.2
done
pkill -9 -f "$target/Contents/MacOS/" 2>/dev/null

backup="$work/previous.app"
keep=%(keep)s
if [ -d "$target" ]; then
  mv "$target" "$backup" || { echo "update: could not move the old app aside" >>"$log"; exit 1; }
fi
if ! mv "$staged" "$target"; then
  echo "update: could not put the new app in place; restoring" >>"$log"
  [ -d "$backup" ] && mv "$backup" "$target"
  exit 1
fi
# Quarantine would make the replacement look like a fresh download and ask the
# person to confirm an app they already had.
/usr/bin/xattr -dr com.apple.quarantine "$target" 2>/dev/null
# Hold on to what was replaced. Only the one previous version: a shelf of old
# builds is its own mess, and going back more than a step is what the releases
# page is for.
if [ -d "$backup" ]; then
  mkdir -p "$(dirname "$keep")"
  rm -rf "$keep"
  mv "$backup" "$keep" && echo "update: kept the previous version at $keep" >>"$log"
fi
echo "update: replaced, starting it again" >>"$log"
/usr/bin/open "$target"
sleep 5
rm -rf "$work"
"""


def kept_version():
    """The version held in reserve, or None when there is nothing to go back to."""
    if not os.path.isdir(KEPT_APP):
        return None
    return installed_version(KEPT_APP)


def stage_kept(log=print):
    """Copy the kept version somewhere the swap can move it from.

    Copied rather than moved, so a swap that fails partway leaves the reserve
    where it was.
    """
    if not os.path.isdir(KEPT_APP):
        return None
    work = tempfile.mkdtemp(prefix="clauminella-rollback-")
    staged = os.path.join(work, APP_NAME)
    try:
        shutil.copytree(KEPT_APP, staged, symlinks=True)
    except OSError as exc:
        log("rollback: could not copy the kept version: %r" % exc)
        shutil.rmtree(work, ignore_errors=True)
        return None
    return staged


def swap_and_relaunch(staged, target=None, log_path=None, log=print):
    """Hand the replacement to a script that outlives this process."""
    target = target or app_path()
    if not target or not staged or not os.path.isdir(staged):
        return False
    work = os.path.dirname(staged)
    script = os.path.join(work, "swap.sh")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(SWAP % {
                "target": json.dumps(target),
                "staged": json.dumps(staged),
                "work": json.dumps(work),
                "keep": json.dumps(KEPT_APP),
                "log": json.dumps(log_path or "/dev/null"),
            })
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/bash", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        log("update: could not start the swap: %r" % exc)
        return False
    return True
