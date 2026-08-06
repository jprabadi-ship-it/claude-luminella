"""Push-to-talk: hold a switch to record, release to transcribe.

Recording is ffmpeg reading the AVFoundation input; transcription is an
external command so the app bundle stays small (mlx-whisper alone is ~180MB,
and its models are over a gigabyte). The backend is auto-detected at runtime
and can be pinned in config.json.

Nothing here is on the critical path: if ffmpeg or the transcriber is missing,
push-to-talk reports the problem and the rest of the app is unaffected.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import traceback
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ignore taps too short to be speech, and stop runaway recordings.
MIN_SECONDS = 0.4
MAX_SECONDS = 180


def clean_env():
    """Environment for child processes, with the app bundle's Python stripped.

    py2app exports PYTHONHOME and PYTHONPATH pointing at the bundle's own
    interpreter. Any external Python we spawn inherits them, fails to find its
    standard library, and dies with "No module named 'encodings'" before it
    runs a line of code.
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("PYTHON") or key in ("RESOURCEPATH", "ARGVZERO", "EXECUTABLEPATH"):
            del env[key]
    # An app bundle inherits no locale, so a child process defaults to ASCII
    # and dies the moment its output contains a progress bar or Japanese text.
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "en_US.UTF-8"
    # CoreFoundation tools read this rather than LANG; without it pbcopy
    # interprets UTF-8 bytes as MacRoman and pastes mojibake.
    env["__CF_USER_TEXT_ENCODING"] = "0x1F5:0x08000100:0x08000100"
    return env


KEYCODE_V = 9  # kVK_ANSI_V


def accessibility_trusted(prompt=False):
    """Whether this process may post keyboard events."""
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: bool(prompt)}))
    except Exception:
        return False


def paste_to_focused():
    """Send Cmd+V to whatever has focus. Returns (ok, message).

    The event is posted by this process rather than by osascript. Routing it
    through System Events made macOS attribute the keystroke to osascript,
    which is not something the user can grant accessibility to -- the attempt
    failed with "osascript is not allowed to send keystrokes". Posting
    directly makes this app the actor, so the permission can be granted to it
    by name.
    """
    if not accessibility_trusted():
        return False, "アクセシビリティの許可がありません"
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSetFlags,
            kCGEventFlagMaskCommand,
            kCGHIDEventTap,
        )
    except ImportError as exc:
        return False, "Quartz を読み込めません: %s" % exc

    try:
        for pressed in (True, False):
            event = CGEventCreateKeyboardEvent(None, KEYCODE_V, pressed)
            CGEventSetFlags(event, kCGEventFlagMaskCommand)
            CGEventPost(kCGHIDEventTap, event)
            time.sleep(0.02)
    except Exception as exc:
        return False, repr(exc)
    return True, ""


def set_clipboard(text):
    """Put text on the clipboard, preferring the in-process pasteboard.

    Going through pbcopy means handing bytes to another process and hoping it
    guesses the encoding; NSPasteboard takes a string and there is nothing to
    misinterpret.
    """
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString

        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        if board.setString_forType_(text, NSPasteboardTypeString):
            return True
    except Exception:
        pass
    subprocess.run(
        ["/usr/bin/pbcopy"], input=text.encode("utf-8"), timeout=10, env=clean_env()
    )
    return True


def find_ffmpeg():
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def find_transcriber(explicit=None):
    """(kind, path) for the best available backend, or (None, None).

    mlx-whisper runs on the GPU and is roughly twenty times faster than the
    reference implementation on Apple Silicon, so it wins when present.
    """
    if explicit and os.path.exists(explicit):
        return ("mlx" if "mlx" in os.path.basename(explicit) else "whisper"), explicit
    local = os.path.join(ROOT, ".venv", "bin", "mlx_whisper")
    if os.path.exists(local):
        return "mlx", local
    found = shutil.which("mlx_whisper")
    if found:
        return "mlx", found
    found = shutil.which("whisper")
    if found:
        return "whisper", found
    return None, None


def list_input_devices(ffmpeg=None):
    """[(index, name)] of AVFoundation audio inputs."""
    ffmpeg = ffmpeg or find_ffmpeg()
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, timeout=20, env=clean_env(),
            encoding="utf-8", errors="replace",
        ).stderr
    except (OSError, subprocess.SubprocessError):
        return []
    devices, in_audio = [], False
    for line in out.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if in_audio:
            # "[AVFoundation indev @ 0x...] [0] Device name"
            marker = "] ["
            if marker not in line:
                break
            try:
                rest = line.split(marker, 1)[1]
                index, name = rest.split("] ", 1)
                devices.append((int(index), name.strip()))
            except ValueError:
                break
    return devices


class PushToTalk:
    """Owns one recording at a time. Callbacks fire on a worker thread."""

    def __init__(self, cfg, on_state=None, on_result=None, log=print):
        self.cfg = cfg
        self.on_state = on_state or (lambda state: None)
        self.on_result = on_result or (lambda ok, text, pasted=False: None)
        self.log = log
        self.proc = None
        self.path = None
        self.started_at = None
        self.opened_at = None
        self.lock = threading.Lock()

    def available(self):
        return find_transcriber(self.cfg.get("stt_path"))[0] is not None

    def is_recording(self):
        with self.lock:
            return self.proc is not None

    def start(self):
        with self.lock:
            if self.proc:
                return
            ffmpeg = find_ffmpeg()
            fd, self.path = tempfile.mkstemp(prefix="luminella-ptt-", suffix=".wav")
            os.close(fd)
            # Capture in the device's native format. Forcing a rate or channel
            # count here makes some interfaces (e.g. a 192kHz 5.1 device) drop
            # out and yield silence; the transcriber resamples anyway.
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "info",
                "-f", "avfoundation",
                "-i", ":%d" % int(self.cfg.get("mic_index", 0)),
                "-t", str(MAX_SECONDS),
                "-y", self.path,
            ]
            try:
                self.proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    env=clean_env(), encoding="utf-8", errors="replace", bufsize=1,
                )
            except OSError as exc:
                self.log("ptt: ffmpeg failed to start: %s" % exc)
                self.proc = None
                self.on_result(False, "ffmpeg を起動できません")
                return
            self.started_at = time.time()
            self.log("ptt: opening mic %s" % self.cfg.get("mic_index", 0))
            self.on_state("warmup")
            proc = self.proc

        def wait_until_live():
            """Signal "speak now" only once the device is actually delivering.

            A Continuity microphone can take three or four seconds to come up,
            and nothing before that is captured -- the first words vanish. The
            ring is the cue to start talking, so it must not turn on until
            ffmpeg reports the input stream.
            """
            opened = None
            try:
                for line in proc.stderr:
                    if "Input #0" in line:
                        opened = time.time()
                        break
            except (OSError, ValueError):
                pass
            with self.lock:
                still_ours = self.proc is proc
            if not still_ours:
                return
            if opened:
                self.opened_at = opened
                self.log("ptt: mic live after %.2fs" % (opened - self.started_at))
                self.on_state("rec")
            else:
                self.log("ptt: mic never reported ready")

        threading.Thread(target=wait_until_live, daemon=True).start()

        def watchdog():
            time.sleep(MAX_SECONDS)
            with self.lock:
                stale = self.proc is proc
            if stale:
                self.log("ptt: watchdog stop after %ds" % MAX_SECONDS)
                self.stop()

        threading.Thread(target=watchdog, daemon=True).start()

    def stop(self):
        with self.lock:
            proc, path, started = self.proc, self.path, self.started_at
            self.proc = self.path = self.started_at = None
        if not proc:
            return

        # SIGINT lets ffmpeg finalise the container; killing it truncates.
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

        live_from = self.opened_at or started or time.time()
        held = time.time() - live_from
        self.opened_at = None
        self.log("ptt: captured %.2fs of audio" % held)
        if held < MIN_SECONDS:
            self._cleanup(path)
            self.on_state("idle")
            return

        threading.Thread(target=self._transcribe, args=(path,), daemon=True).start()

    SILENCE_DB = -60.0

    def _peak_db(self, path):
        """Peak level of a capture, or None if it cannot be measured."""
        try:
            out = subprocess.run(
                [find_ffmpeg(), "-hide_banner", "-i", path, "-af", "volumedetect",
                 "-f", "null", "-"],
                capture_output=True, timeout=60, env=clean_env(),
                encoding="utf-8", errors="replace",
            ).stderr
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            if "max_volume:" in line:
                try:
                    return float(line.split("max_volume:")[1].strip().split()[0])
                except (IndexError, ValueError):
                    return None
        return None

    def _transcribe(self, path):
        try:
            self._transcribe_inner(path)
        except Exception:
            self.log("ptt: transcribe crashed\n%s" % traceback.format_exc())
            self._cleanup(path)
            self.on_result(False, "文字起こしが異常終了しました")

    def _trim_silence(self, path):
        """Strip leading and trailing silence, resampled to 16k mono.

        Whisper fills silence with invention -- it will loop the previous
        sentence, or produce a stock phrase like a video sign-off. Handing it
        only the speech avoids the problem regardless of which microphone is
        in use.
        """
        out = path[:-4] + "-trim.wav"
        cmd = [
            find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", path,
            "-af",
            "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-45dB:"
            "detection=peak,areverse,"
            "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-45dB:"
            "detection=peak,areverse",
            "-ar", "16000", "-ac", "1", "-y", out,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120, env=clean_env(),
                           encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("ptt: trim failed: %s" % exc)
            return path
        if not os.path.exists(out) or os.path.getsize(out) < 2000:
            self.log("ptt: trim produced nothing usable, using raw capture")
            return path
        return out

    def _transcribe_inner(self, path):
        self.on_state("stt")

        peak = self._peak_db(path)
        self.log("ptt: peak %s dB (mic %s)" % (peak, self.cfg.get("mic_index", 0)))
        if peak is not None and peak < self.SILENCE_DB:
            self.log("ptt: silent capture")
            self._cleanup(path)
            self.on_result(False, "音が入っていません（マイクを確認してください）")
            return

        if self.cfg.get("trim_silence"):
            original = os.path.getsize(path)
            trimmed = self._trim_silence(path)
            if trimmed != path:
                kept = os.path.getsize(trimmed)
                # A capture is 16-bit mono at the device rate; anything under a
                # second, or a fraction of what went in, means the filter ate
                # the speech rather than the silence.
                if kept < 32000 or kept < original * 0.15:
                    self.log("ptt: trim removed too much (%d -> %d), using raw" % (original, kept))
                    try:
                        os.unlink(trimmed)
                    except OSError:
                        pass
                else:
                    self._cleanup(path)
                    path = trimmed
                    self.log("ptt: trimmed to %.1f KB" % (kept / 1024.0))

        kind, exe = find_transcriber(self.cfg.get("stt_path"))
        if not kind:
            self._cleanup(path)
            self.on_result(False, "文字起こしエンジンが見つかりません")
            return

        outdir = tempfile.mkdtemp(prefix="luminella-stt-")
        name = "out"
        language = self.cfg.get("stt_language", "ja")
        if kind == "mlx":
            cmd = [exe, path,
                   "--model", self.cfg.get("stt_model", "mlx-community/whisper-large-v3-turbo"),
                   "--language", language,
                   "--output-format", "txt",
                   "--output-dir", outdir,
                   "--output-name", name]
        else:
            cmd = [exe, path,
                   "--model", self.cfg.get("stt_model_cli", "base"),
                   "--language", language,
                   "--output_format", "txt",
                   "--output_dir", outdir,
                   "--fp16", "False"]
            name = os.path.splitext(os.path.basename(path))[0]

        try:
            keep = os.path.join(os.path.dirname(self.cfg.get("_log_path", "")) or
                                os.path.expanduser("~/.claude/luminella"), "last.wav")
            shutil.copyfile(path, keep)
        except OSError:
            pass

        self.log("ptt: transcribing with %s (%s)" % (kind, exe))
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=300, env=clean_env(),
                encoding="utf-8", errors="replace"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("ptt: transcriber failed: %s" % exc)
            self._cleanup(path, outdir)
            self.on_result(False, "文字起こしに失敗しました")
            return

        txt = os.path.join(outdir, name + ".txt")
        text = ""
        if os.path.exists(txt):
            with open(txt, encoding="utf-8") as f:
                text = f.read().strip()
        if not text:
            self.log("ptt: empty transcript rc=%s err=%s" % (result.returncode, result.stderr[-300:]))
            self._cleanup(path, outdir)
            self.on_result(False, "聞き取れませんでした")
            return

        try:
            set_clipboard(text)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("ptt: pbcopy failed: %s" % exc)
            self._cleanup(path, outdir)
            self.on_result(False, "クリップボードにコピーできませんでした")
            return

        self._cleanup(path, outdir)

        if self.cfg.get("paste_to_focused"):
            # Give the focused app a moment to be ready for the keystroke.
            time.sleep(0.15)
            ok, err = paste_to_focused()
            if not ok:
                self.log("ptt: paste failed: %s" % err)
                self.on_result(True, text, pasted=False)
                return

        self.on_result(True, text, pasted=bool(self.cfg.get("paste_to_focused")))

    def _cleanup(self, path, outdir=None):
        for p in (path,):
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
        if outdir:
            shutil.rmtree(outdir, ignore_errors=True)
