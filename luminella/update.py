"""Check for a newer build and swap this one for it.

This follows GigowatUpdater (~/総合/GigowatUpdater/GigowatUpdater.swift), the
shared component the other gigowat apps use. Clauminella is Python under
py2app, so the Swift cannot be dropped in; what matches is the behaviour -- the
same manifest, the same checks, the same three answers, the same log.

Everything that can fail happens before anything is replaced: the manifest is
read, the disk image fetched and checksummed, the bundle inside it held to a
code signing requirement, and a copy staged in a temporary folder. Only then
does the running app quit and the copy move into place.

The checks are the point. The app downloads code and runs it, so a build is
accepted only when its bytes match the manifest and its signature satisfies
  anchor apple generic and identifier "<our bundle id>"
  and certificate leaf[subject.OU] = "<our team>" and notarized
-- Apple having notarised something says they looked at it, not that it is
ours, and the bundle id keeps a different app of ours out of this one's place.
"""

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

SLUG = "clauminella"
MANIFEST = "https://gigowat.com/updates/%s.json" % SLUG
USER_AGENT = "GigowatUpdater"
APP_NAME = "Clauminella.app"
PAGE = "https://claude-luminella.gigowat.com/"
TIMEOUT = 20

# Settings read from the app's own defaults, named as in the component.
MANIFEST_OVERRIDE_KEY = "GigowatUpdaterManifestURL"
TEST_ANSWER_KEY = "GigowatUpdaterTestAnswer"
SKIP_KEY = "GigowatUpdaterSkippedVersion"

# The answers the dialog can give, in the component's button order.
INSTALL, LATER, SKIP = 1, 2, 3

# Where the version being replaced is kept, so there is a way back from a bad
# update. Deliberately not beside the installed app: /Applications is for the
# one copy people are meant to run, and a second bundle there is something to
# open by mistake.
KEEP_DIR = os.path.join(os.path.expanduser("~"), ".claude", "luminella", "previous")
KEPT_APP = os.path.join(KEEP_DIR, APP_NAME)


def _run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout, check=False)


# ---- where we are ---------------------------------------------------------

def app_path():
    """Where this bundle lives, or None when running from source."""
    resources = os.environ.get("RESOURCEPATH")
    if not resources:
        return None
    path = os.path.dirname(os.path.dirname(resources))
    return path if path.endswith(".app") else None


def _plist(path):
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f)
    except (OSError, ValueError):
        return {}


def installed_version(path=None):
    path = path or app_path()
    return _plist(path).get("CFBundleShortVersionString") if path else None


def bundle_id(path=None):
    path = path or app_path()
    return _plist(path).get("CFBundleIdentifier") if path else None


def team_id(path):
    """The Team ID a bundle is signed with, or None."""
    out = _run(["/usr/bin/codesign", "-dv", "--verbose=2", path], timeout=60)
    found = re.search(r"TeamIdentifier=(.+)", (out.stderr or "") + (out.stdout or ""))
    if not found:
        return None
    # Apple's own bundles say "not set", whose first word looks like a team id.
    value = found.group(1).strip()
    return None if value.lower() == "not set" else value


def log(message):
    """Append to the component's log, in its format.

    Kept where the Swift apps keep theirs so one place holds every app's
    update history: ~/Library/Caches/<bundle id>/GigowatUpdater.log
    """
    identifier = bundle_id() or USER_AGENT
    directory = os.path.join(os.path.expanduser("~"), "Library", "Caches", identifier)
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(os.path.join(directory, "GigowatUpdater.log"), "a",
                  encoding="utf-8") as f:
            f.write("%s %s %s\n" % (stamp, installed_version() or "?", message))
    except OSError:
        pass


# ---- the app's own settings ----------------------------------------------

def _default(key):
    identifier = bundle_id()
    if not identifier:
        return None
    out = _run(["/usr/bin/defaults", "read", identifier, key], timeout=30)
    return out.stdout.strip() if out.returncode == 0 else None


def _set_default(key, value):
    identifier = bundle_id()
    if identifier:
        _run(["/usr/bin/defaults", "write", identifier, key, str(value)], timeout=30)


def manifest_url():
    override = _default(MANIFEST_OVERRIDE_KEY)
    return override or MANIFEST


def is_overridden():
    return bool(_default(MANIFEST_OVERRIDE_KEY))


def test_answer():
    """The answer to give without asking, while the manifest is overridden.

    Only honoured alongside an override, exactly as the component does it, so
    a stray setting cannot make the real app install without being asked.
    """
    if not is_overridden():
        return None
    try:
        value = int(_default(TEST_ANSWER_KEY) or 0)
    except ValueError:
        return None
    return value if value in (INSTALL, LATER, SKIP) else None


def skipped_version():
    return _default(SKIP_KEY)


def skip_version(version):
    _set_default(SKIP_KEY, version)


# ---- the manifest ---------------------------------------------------------

def _parts(version):
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def is_newer(remote, local):
    """Whether remote is a later version than local, by number not by string."""
    if not remote or not local:
        return False
    return _parts(remote) > _parts(local)


def latest():
    """The manifest as a dict, or None when it cannot be read."""
    url = manifest_url()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.load(response)
    except Exception as exc:
        log("manifest unreadable: %r" % exc)
        return None
    if not data.get("version") or not data.get("url"):
        log("manifest incomplete")
        return None
    return data


# ---- whether this copy can be replaced at all ------------------------------

def location_problem():
    """Why this copy cannot be swapped, or None when it can."""
    path = app_path()
    if not path:
        return "ソースから起動しているため、自動更新できません。"
    if "/AppTranslocation/" in path:
        return ("いまのアプリは macOS が一時的な場所に移して動かしているため、"
                "自動で入れ替えられません。Clauminella を「アプリケーション」"
                "フォルダへ移してから起動し直すか、配布ページから新しい版を"
                "入れてください。")
    if path.startswith("/Volumes/"):
        return ("ディスクイメージ（dmg）の中から起動しているため、入れ替え"
                "られません。Clauminella を「アプリケーション」フォルダへ"
                "移してからお使いください。")
    parent = os.path.dirname(path)
    if not (os.access(parent, os.W_OK) and os.access(path, os.W_OK)
            and os.access(os.path.join(path, "Contents"), os.W_OK)):
        return ("いまのアプリの置き場所（%s）に書き込めないため、自動で"
                "入れ替えられません。配布ページから新しい版を入れて"
                "ください。" % parent)
    return None


# ---- fetching and checking -------------------------------------------------

def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stage(manifest):
    """Download, check, and lay the new bundle aside.

    Returns (path, None) on success or (None, reason) on refusal. Nothing
    installed is touched either way.
    """
    want_team = team_id(app_path())
    want_id = bundle_id()
    if not want_team or not want_id:
        return None, ("いまのアプリが Developer ID で署名されていないため、"
                      "自動更新できません（開発用のビルドです）。")

    work = tempfile.mkdtemp(prefix="clauminella-update-")
    dmg = os.path.join(work, "update.dmg")
    try:
        request = urllib.request.Request(manifest["url"],
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=300) as response, \
                open(dmg, "wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        log("download failed: %r" % exc)
        return None, "ダウンロードできませんでした。"

    size = manifest.get("size")
    if size and os.path.getsize(dmg) != int(size):
        shutil.rmtree(work, ignore_errors=True)
        log("size mismatch")
        return None, "ダウンロードしたファイルの大きさが台帳と一致しません。"

    want_sha = (manifest.get("sha256") or "").lower()
    if want_sha and _sha256(dmg) != want_sha:
        shutil.rmtree(work, ignore_errors=True)
        log("sha256 mismatch")
        return None, ("ダウンロードしたファイルが台帳と一致しません"
                      "（sha256 が違います）。")

    mount = os.path.join(work, "mnt")
    os.makedirs(mount, exist_ok=True)
    attached = _run(["/usr/bin/hdiutil", "attach", dmg, "-nobrowse", "-quiet",
                     "-mountpoint", mount], timeout=300)
    if attached.returncode != 0:
        shutil.rmtree(work, ignore_errors=True)
        log("attach failed: %s" % attached.stderr.strip()[:200])
        return None, "ディスクイメージを開けませんでした。"

    staged, problem = None, None
    try:
        source = os.path.join(mount, APP_NAME)
        if not os.path.isdir(source) or bundle_id(source) != want_id:
            problem = ("ディスクイメージの中に、このアプリ（%s）が見つかり"
                       "ません。" % want_id)
        else:
            requirement = ('anchor apple generic and identifier "%s" '
                           'and certificate leaf[subject.OU] = "%s" '
                           'and notarized' % (want_id, want_team))
            checked = _run(["/usr/bin/codesign", "--verify", "--deep",
                            "--strict", "-R=" + requirement, source],
                           timeout=300)
            if checked.returncode != 0:
                why = (checked.stderr or "").strip().splitlines()
                problem = ("新しい版の署名または公証を確かめられませんでした。"
                           "上書きはしていません。\n（%s）"
                           % (why[-1][:160] if why else "requirement not met"))
                log("requirement failed: %s" % (why[-1][:200] if why else "?"))
            else:
                found = installed_version(source)
                if found != manifest["version"]:
                    problem = ("ディスクイメージの中の版（%s）が、台帳の版"
                               "（%s）と合いません。" % (found, manifest["version"]))
                else:
                    staged = os.path.join(work, APP_NAME)
                    shutil.copytree(source, staged, symlinks=True)
    finally:
        _run(["/usr/bin/hdiutil", "detach", mount, "-quiet"], timeout=120)

    if staged is None:
        shutil.rmtree(work, ignore_errors=True)
        return None, problem or "新しい版を確かめられませんでした。"
    return staged, None


# ---- going back ------------------------------------------------------------

def kept_version():
    """The version held in reserve, or None when there is nothing to go back to."""
    return installed_version(KEPT_APP) if os.path.isdir(KEPT_APP) else None


def stage_kept():
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
        log("rollback copy failed: %r" % exc)
        shutil.rmtree(work, ignore_errors=True)
        return None
    return staged


# ---- the swap --------------------------------------------------------------

SWAP = r"""#!/bin/bash
# Written by Clauminella to replace itself. Waits for the app to go, puts the
# new bundle in place, starts it again, and clears up after itself.
set -u
target=%(target)s
staged=%(staged)s
work=%(work)s
keep=%(keep)s
log=%(log)s

say() { echo "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%S%%z) swap $*" >>"$log" 2>/dev/null; }

for _ in $(seq 1 100); do
  pgrep -f "$target/Contents/MacOS/" >/dev/null 2>&1 || break
  sleep 0.2
done
pkill -9 -f "$target/Contents/MacOS/" 2>/dev/null

backup="$work/previous.app"
if [ -d "$target" ]; then
  mv "$target" "$backup" || { say "could not move the old app aside"; exit 1; }
fi
if ! mv "$staged" "$target"; then
  say "could not put the new app in place; restoring"
  [ -d "$backup" ] && mv "$backup" "$target"
  exit 1
fi
# Quarantine would make the replacement look like a fresh download and ask the
# person to confirm an app they already had.
/usr/bin/xattr -dr com.apple.quarantine "$target" 2>/dev/null
# Hold on to what was replaced, one generation only: a shelf of old builds is
# its own mess, and going back further is what the releases page is for.
if [ -d "$backup" ]; then
  mkdir -p "$(dirname "$keep")"
  rm -rf "$keep"
  mv "$backup" "$keep" && say "kept the previous version at $keep"
fi
say "replaced, starting it again"
/usr/bin/open "$target"
sleep 5
rm -rf "$work"
"""


def swap_and_relaunch(staged, target=None):
    """Hand the replacement to a script that outlives this process."""
    target = target or app_path()
    if not target or not staged or not os.path.isdir(staged):
        return False
    work = os.path.dirname(staged)
    script = os.path.join(work, "swap.sh")
    identifier = bundle_id() or USER_AGENT
    log_file = os.path.join(os.path.expanduser("~"), "Library", "Caches",
                            identifier, "GigowatUpdater.log")
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(SWAP % {
                "target": json.dumps(target),
                "staged": json.dumps(staged),
                "work": json.dumps(work),
                "keep": json.dumps(KEPT_APP),
                "log": json.dumps(log_file),
            })
        os.chmod(script, 0o755)
        subprocess.Popen(["/bin/bash", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        log("could not start the swap: %r" % exc)
        return False
    return True
