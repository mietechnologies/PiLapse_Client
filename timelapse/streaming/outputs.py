"""
Outputs that receive encoded frames from picamera2's hardware encoders.

  FrameBuffer      — holds the latest MJPEG frame for the built-in web server
  FfmpegPipeOutput — feeds H.264 into a supervised ffmpeg process
                     (MediaMTX publish, RTMP push)

Both are called on picamera2's encoder thread, which is shared by every
output of that encoder.  They therefore never block: a stalled network
connection must not freeze the live view or the other outputs.
"""

from __future__ import annotations

import logging
import queue
import struct
import subprocess
import threading
import time
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

try:
    from picamera2.outputs import Output as _Output
except ImportError:  # development machine / tests: same interface, no camera stack

    class _Output:  # type: ignore[no-redef]
        def __init__(self, pts=None) -> None:
            self.recording = False
            self.needs_pacing = False
            self.needs_add_stream = False

        def start(self) -> None:
            self.recording = True

        def stop(self) -> None:
            self.recording = False

        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            pass

        def _add_stream(self, encoder_stream, *args, **kwargs) -> None:
            pass


# ─── latest-frame buffer (MJPEG) ─────────────────────────────────────────────


class FrameBuffer(_Output):
    """Keeps the most recent frame and wakes anyone waiting for a new one."""

    def __init__(self) -> None:
        super().__init__()
        self._cond = threading.Condition()
        self._frame: Optional[bytes] = None
        self._seq = 0

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        data = bytes(frame)  # the encoder reuses its buffer after we return
        with self._cond:
            self._frame = data
            self._seq += 1
            self._cond.notify_all()

    def stop(self) -> None:
        super().stop()
        with self._cond:
            self._frame = None  # never serve a stale frame after the encoder stops

    def wait_for_frame(self, after_seq: int, timeout: float) -> tuple[int, Optional[bytes]]:
        """Block until a frame newer than `after_seq` exists (or timeout).

        Returns (seq, frame); frame is None on timeout.
        """
        with self._cond:
            fresh = self._cond.wait_for(
                lambda: self._seq > after_seq and self._frame is not None, timeout
            )
            return (self._seq, self._frame) if fresh else (after_seq, None)


# ─── supervised ffmpeg (H.264) ───────────────────────────────────────────────

# Frames reach ffmpeg wrapped in IVF, a minimal container that carries a
# timestamp per frame.  This passes the camera's own sensor timestamps through
# instead of having ffmpeg stamp raw H.264 on arrival (what picamera2's
# FfmpegOutput does): that breaks whenever two frames land in one read —
# e.g. after a stall — producing unset and non-monotonic timestamps that
# RTMP ingests dislike.
_PIPE_INPUT = ["-thread_queue_size", "64", "-f", "ivf", "-i", "-"]
_IVF_TIMEBASE_HZ = 1_000_000  # picamera2 timestamps are in microseconds


def ivf_header(width: int, height: int) -> bytes:
    return b"DKIF" + struct.pack(
        "<HH4sHHIIII", 0, 32, b"H264", width, height, _IVF_TIMEBASE_HZ, 1, 0, 0
    )


def ivf_frame(data: bytes, pts_us: int) -> bytes:
    return struct.pack("<IQ", len(data), pts_us) + data


def mediamtx_args(publish_url: str) -> list[str]:
    """ffmpeg output args to publish the copied H.264 stream to MediaMTX."""
    return ["-c:v", "copy", "-f", "rtsp", "-rtsp_transport", "tcp", publish_url]


def rtmp_args(url: str, silent_audio: bool = True) -> list[str]:
    """ffmpeg output args to push the copied H.264 stream to an RTMP ingest."""
    if not silent_audio:
        return ["-c:v", "copy", "-f", "flv", url]
    return [
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "flv", url,
    ]


class FfmpegPipeOutput(_Output):
    """
    Pipes H.264 into an ffmpeg process and keeps it running.

    - Frames pass through a bounded queue to a writer thread, so the encoder
      thread never waits on ffmpeg.  If the queue fills (ffmpeg or the network
      stalled), frames are dropped and writing resumes at the next keyframe so
      the receiver never gets a broken group of pictures.
    - If ffmpeg exits (network drop, MediaMTX restart, ingest hiccup) it is
      restarted with exponential back-off, starting on a keyframe.

    `secrets` are redacted from everything this class logs.
    """

    RESTART_MIN_S = 2.0
    RESTART_MAX_S = 60.0
    # A process that stays up this long resets the back-off.
    HEALTHY_AFTER_S = 30.0

    def __init__(
        self,
        name: str,
        output_args: Sequence[str],
        size: tuple[int, int] = (1280, 720),
        secrets: Sequence[str] = (),
        queue_frames: int = 60,
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        super().__init__()
        self.name = name
        self._size = size
        self._cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "warning",
                     *_PIPE_INPUT, *output_args]
        self._secrets = [s for s in secrets if s]
        self._queue: "queue.Queue[Optional[tuple[bytes, bool, int]]]" = queue.Queue(queue_frames)
        self._resync = True  # encoder side: only enqueue from a keyframe
        self._dropped = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self.restarts = 0

    # ── picamera2 Output interface ────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        super().start()
        self._stop_event.clear()
        self._resync = True
        self._thread = threading.Thread(
            target=self._writer_loop, name=f"ffmpeg-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        # Called by Encoder.stop() and by our owner; must be idempotent.
        super().stop()
        self._stop_event.set()
        # Terminate first: if ffmpeg stopped reading, the writer is blocked in
        # write() and only the process exiting will release it.
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        try:
            self._queue.put_nowait(None)  # wake an idle writer
        except queue.Full:
            pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self._thread = None
        self._kill(graceful=False)  # no-op unless the writer failed to clean up

    def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
        if not self.recording or audio:
            return
        if self._resync and not keyframe:
            return
        if timestamp is None:  # picamera2's V4L2 encoders always pass one
            timestamp = time.monotonic_ns() // 1000
        try:
            self._queue.put_nowait((bytes(frame), keyframe, timestamp))
            self._resync = False
        except queue.Full:
            self._resync = True
            self._dropped += 1
            if self._dropped in (1, 10) or self._dropped % 300 == 0:
                logger.warning(
                    "[%s] ffmpeg is not keeping up — dropped %d frame(s); "
                    "resuming at next keyframe.", self.name, self._dropped,
                )

    # ── writer thread ─────────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        backoff = self.RESTART_MIN_S
        started_at = 0.0
        need_keyframe = True
        ts_base = last_pts = 0

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                proc = self._proc
                if proc is not None and proc.poll() is not None:
                    backoff, need_keyframe = self._on_exit(started_at, backoff)
                continue
            if item is None:
                break
            data, keyframe, timestamp = item

            proc = self._proc
            header = b""
            if proc is None:
                if not keyframe:
                    continue  # a fresh ffmpeg must start on a keyframe
                proc = self._spawn()
                if proc is None:
                    backoff = self._wait_backoff(backoff)
                    continue
                started_at = time.monotonic()
                need_keyframe = False
                header = ivf_header(*self._size)
                ts_base, last_pts = timestamp, -1  # each ffmpeg's timeline starts at 0
            elif need_keyframe and not keyframe:
                continue

            pts = max(timestamp - ts_base, last_pts + 1)  # strictly increasing
            last_pts = pts
            try:
                assert proc.stdin is not None
                proc.stdin.write(header + ivf_frame(data, pts))
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                backoff, need_keyframe = self._on_exit(started_at, backoff)

        # After stop() ffmpeg is already terminating; don't wait on a flush.
        self._kill(graceful=not self._stop_event.is_set())

    def _on_exit(self, started_at: float, backoff: float) -> tuple[float, bool]:
        """ffmpeg died: log why, clean up, wait, and reset keyframe gating."""
        proc = self._proc
        rc = proc.poll() if proc else None
        uptime = time.monotonic() - started_at
        self._kill()
        if self._stop_event.is_set():
            return backoff, True
        if uptime >= self.HEALTHY_AFTER_S:
            backoff = self.RESTART_MIN_S
        logger.warning(
            "[%s] ffmpeg exited (rc=%s) after %.0f s — restarting in %.0f s.",
            self.name, rc, uptime, backoff,
        )
        self.restarts += 1
        return self._wait_backoff(backoff), True

    def _wait_backoff(self, backoff: float) -> float:
        self._stop_event.wait(backoff)
        # Frames queued while we waited are stale; start over from a keyframe.
        self._drain()
        return min(backoff * 2, self.RESTART_MAX_S)

    def _drain(self) -> None:
        while True:
            try:
                if self._queue.get_nowait() is None:
                    self._stop_event.set()
            except queue.Empty:
                return

    def _spawn(self) -> Optional[subprocess.Popen]:
        try:
            proc = subprocess.Popen(
                self._cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            logger.error("[%s] could not start ffmpeg: %s", self.name, exc)
            return None
        with self._proc_lock:
            self._proc = proc
        if self._stop_event.is_set():  # stop() raced us; it won't see this process
            self._kill(graceful=False)
            return None
        logger.info("[%s] ffmpeg started: %s", self.name, self._redact(" ".join(self._cmd)))
        threading.Thread(
            target=self._log_stderr, args=(proc,), name=f"ffmpeg-{self.name}-log",
            daemon=True,
        ).start()
        return proc

    def _log_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                logger.warning("[%s] ffmpeg: %s", self.name, self._redact(line))

    def _kill(self, graceful: bool = True) -> None:
        """
        Reap the current ffmpeg.  `graceful` closes stdin so ffmpeg can flush
        and exit by itself; only the writer thread may do that, because
        closing a pipe another thread is blocked writing to deadlocks.
        """
        with self._proc_lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        if not graceful and proc.poll() is None:
            proc.kill()
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None
