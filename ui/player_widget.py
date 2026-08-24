"""Embedded libVLC video surface plus VLC's control bar.

Three rules keep this from crashing, and none of them are optional:

1. libVLC event callbacks fire on libVLC's own threads. Touching Qt from there
   corrupts state and is the usual cause of hard crashes in libVLC+Qt apps, so
   every callback only emits a Qt signal (queued) and returns immediately.

2. Playback start/stop is debounced. The account allows one connection, so
   holding an arrow key must not open a socket per keypress.

3. The previous media is fully released before the next one starts, and the
   MediaPlayer is rebuilt after a hard error rather than reused.

The widgets live in `ui/transport_bar.py`; this file owns libVLC and exposes the
operations the bar drives.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame, QLabel, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget,
)

from core import vlc_setup
from ui.transport_bar import TransportBar

SWITCH_DEBOUNCE_MS = 300
# A second request arriving faster than this looks like key-repeat rather than
# a deliberate choice, so it gets debounced; a lone click starts at once.
REPEAT_WINDOW_MS = 400

# Neutral values for VLC's video adjustments; all-neutral means the filter is
# left switched off rather than inserted into the chain for nothing.
ADJUST_NEUTRAL = {
    "contrast": 1.0, "brightness": 1.0, "saturation": 1.0, "gamma": 1.0, "hue": 0,
}

# Keep a forward seek this far from the end. Landing exactly on the duration
# trips end-of-media, so the arrow key would advance to the next item instead
# of seeking - which looks like the key doing something entirely different.
SEEK_TAIL_MS = 1000


def clamp_seek(position_ms: int, delta_s: int, duration_ms: int) -> int:
    """Where a relative jump should land, kept inside the media."""
    target = int(position_ms) + int(delta_s) * 1000
    if duration_ms and duration_ms > 0:
        target = min(target, max(0, int(duration_ms) - SEEK_TAIL_MS))
    return max(0, target)


class VideoSurface(QFrame):
    """Native window libVLC renders into. Must stay opaque and un-styled."""

    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAutoFillBackground(True)
        # Focusable so the arrow keys can be routed to playback rather than to
        # the channel list. Without this the surface can never hold focus and
        # the "arrows follow focus" rule would never fire.
        self.setFocusPolicy(Qt.StrongFocus)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor("#000000"))
        self.setPalette(palette)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(180)

    def paintEvent(self, event):
        # WA_OpaquePaintEvent promises Qt that every pixel gets painted, so
        # this must fill the surface itself. Without it, whatever was behind
        # the widget shows through before libVLC attaches its own output.
        from PySide6.QtGui import QPainter

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class PlayerWidget(QWidget):
    """Video surface + VLC's transport controls."""

    stateChanged = Signal(str)
    positionChanged = Signal(float, int, int)   # fraction, position_s, duration_s
    errorOccurred = Signal(str)
    endReached = Signal()
    playbackStarted = Signal()
    playbackStopped = Signal()
    fullscreenToggled = Signal()
    videoDoubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.instance = None
        self.player = None
        self._vlc = None
        self._current_url = None
        self._current_title = ""
        self._is_live = False
        self._seekable = False
        self._pending = None
        self._resume_to = 0
        self._last_request_ms = 0.0
        self._muted = False
        self._rate = 1.0
        self._aspect = ""
        self._crop = ""
        self._deinterlace = ""
        self._ab = (None, None)
        self._equalizer = None          # must outlive set_equalizer()
        self._equalizer_state = None
        self._adjust = dict(ADJUST_NEUTRAL)
        self.snapshot_dir = None        # set by MainWindow; falls back to app_dir
        self._available = vlc_setup.ensure_vlc()

        self._switch_timer = QTimer(self)
        self._switch_timer.setSingleShot(True)
        self._switch_timer.setInterval(SWITCH_DEBOUNCE_MS)
        self._switch_timer.timeout.connect(self._start_pending)

        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._tick)

        self._build_ui()

        if self._available:
            self._create_player()
        else:
            self.overlay.setText(vlc_setup.error_message() or "VLC not available")
            self.overlay.show()

    # ------------------------------------------------------------------ ui

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        container = QWidget()
        self._stack = QStackedLayout(container)
        self._stack.setStackingMode(QStackedLayout.StackAll)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self.surface = VideoSurface()
        # Not wired straight to fullscreen: what a double-click should do
        # depends on the window mode, which only MainWindow knows.
        self.surface.doubleClicked.connect(self.videoDoubleClicked)

        self.overlay = QLabel("")
        self.overlay.setObjectName("playerOverlay")
        self.overlay.setAlignment(Qt.AlignCenter)
        self.overlay.setWordWrap(True)
        self.overlay.hide()

        self._stack.addWidget(self.overlay)
        self._stack.addWidget(self.surface)
        root.addWidget(container, 1)

        self.bar = TransportBar(self)
        self.controls = self.bar          # name kept for the fullscreen code
        self.bar.fullscreenRequested.connect(self.fullscreenToggled)
        root.addWidget(self.bar)

    # -------------------------------------------------------------- engine

    def _create_player(self):
        import vlc

        self._vlc = vlc
        args = [
            "--no-video-title-show",
            "--network-caching=1500",
            "--http-reconnect",
            "--no-snapshot-preview",
            "--quiet",
        ]
        if not self._hw_enabled():
            args.append("--avcodec-hw=none")
        self.instance = vlc.Instance(*args)
        self.player = self.instance.media_player_new()
        self._attach_surface()
        self._attach_events()
        # libVLC puts its own view over the surface and would otherwise eat
        # mouse and key events, so double-click-to-fullscreen never reaches
        # Qt. Hand input back to us.
        try:
            self.player.video_set_mouse_input(False)
            self.player.video_set_key_input(False)
        except Exception:
            pass
        self.player.audio_set_volume(self.bar.volume.value())
        self._apply_engine_settings()

    def _apply_engine_settings(self):
        """Re-apply everything that lives on the MediaPlayer, not the media.

        A new MediaPlayer is built on every hardware-decoding change and after
        an error, and it comes back with defaults — so without this the user's
        equaliser and video adjustments silently disappear.
        """
        if self.player is None:
            return
        try:
            self.player.audio_set_mute(self._muted)
            self.player.set_rate(self._rate)
            if self._aspect:
                self.player.video_set_aspect_ratio(self._aspect)
            if self._crop:
                self.player.video_set_crop_geometry(self._crop)
            if self._deinterlace:
                self.player.video_set_deinterlace(self._deinterlace)
        except Exception:
            pass
        if self._equalizer_state is not None:
            self.set_equalizer(*self._equalizer_state)
        self.set_video_adjust(self._adjust)

    def _hw_enabled(self) -> bool:
        return getattr(self, "_hw_pref", True)

    def set_hardware_decoding(self, enabled: bool):
        """Rebuild the engine; the flag is only read at Instance creation."""
        self._hw_pref = enabled
        if not self._available:
            return
        was = self._current_url
        self.stop()
        self._release_player()
        self._create_player()
        if was:
            self.play(was, self._current_title, self._is_live)

    def _attach_surface(self):
        handle = int(self.surface.winId())
        if sys.platform == "darwin":
            self.player.set_nsobject(handle)
        elif sys.platform.startswith("win"):
            self.player.set_hwnd(handle)
        else:
            self.player.set_xwindow(handle)

    def reattach_surface(self):
        """Re-bind libVLC to the surface after its native handle was recreated.

        Only needed if a window-level change (the Picture-in-Picture stay-on-top
        flag) makes Qt rebuild the native window. On macOS/Qt 6 it does not, but
        the caller checks the handle and this is what it calls when it did.
        """
        if self.player is not None:
            self._attach_surface()

    def _attach_events(self):
        vlc = self._vlc
        manager = self.player.event_manager()
        # Callbacks run on libVLC threads: emit and return, nothing else.
        manager.event_attach(
            vlc.EventType.MediaPlayerEncounteredError,
            lambda e: self.errorOccurred.emit("Playback failed for this stream."),
        )
        manager.event_attach(
            vlc.EventType.MediaPlayerEndReached, lambda e: self.endReached.emit()
        )
        manager.event_attach(
            vlc.EventType.MediaPlayerPlaying, lambda e: self.stateChanged.emit("playing")
        )
        manager.event_attach(
            vlc.EventType.MediaPlayerPaused, lambda e: self.stateChanged.emit("paused")
        )

    def _release_player(self):
        if self.player is not None:
            try:
                self.player.stop()
                self.player.set_media(None)
                self.player.release()
            except Exception:
                pass
            self.player = None
        if self.instance is not None:
            try:
                self.instance.release()
            except Exception:
                pass
            self.instance = None

    # ------------------------------------------------------------ playback

    @property
    def available(self) -> bool:
        return self._available

    @property
    def current_url(self) -> str:
        return self._current_url or ""

    @property
    def current_title(self) -> str:
        return self._current_title

    @property
    def is_live(self) -> bool:
        return self._is_live

    @property
    def pending(self) -> bool:
        """A stream is queued but has not started yet.

        `current_url` is only set once _start_pending runs, so on its own it
        cannot tell "stopped" from "about to start" during the debounce.
        """
        return self._pending is not None

    def play(self, url: str, title: str = "", is_live: bool = False,
             resume_secs: int = 0, immediate: bool = True):
        """Queue a stream.

        The debounce exists so that holding an arrow key cannot open a socket
        per keypress on a one-connection account. A single deliberate click is
        not that, so it starts immediately; only a request following hard on
        the heels of another gets delayed.
        """
        if not self._available:
            self.errorOccurred.emit(vlc_setup.error_message() or "VLC not available")
            return
        now = time.monotonic() * 1000.0
        looks_like_repeat = (now - self._last_request_ms) < REPEAT_WINDOW_MS
        self._last_request_ms = now

        self._pending = (url, title, is_live, resume_secs)
        self._ab = (None, None)
        self.bar.set_live(is_live)
        self.overlay.setText(f"Opening {title or 'stream'}…")
        self.overlay.show()

        if immediate and not looks_like_repeat:
            # Zero-delay timer rather than a direct call: this runs on the next
            # event-loop turn (still sub-millisecond) but avoids starting libVLC
            # re-entrantly from inside a click handler, which can hand it a
            # widget whose native window is not ready yet.
            self._switch_timer.stop()
            QTimer.singleShot(0, self._start_pending)
        else:
            self._switch_timer.start()

    def _start_pending(self):
        if not self._pending or self.player is None:
            return
        url, title, is_live, resume_secs = self._pending
        self._pending = None
        self._current_url = url
        self._current_title = title
        self._is_live = is_live
        self._resume_to = resume_secs

        try:
            # Release the previous media before opening the next one: the
            # account allows a single connection.
            self.player.stop()
            media = self.instance.media_new(url)
            if is_live:
                media.add_option(":network-caching=2000")
            self.player.set_media(media)
            media.release()
            self.player.play()
            # Rate and the video filters are reset by a new media, so they are
            # re-stated here rather than only at engine creation.
            self._apply_engine_settings()
            self._poll.start()
            self._claim_focus()
            self.playbackStarted.emit()
        except Exception as exc:
            self.errorOccurred.emit(f"Could not start playback: {exc}")

    def _claim_focus(self):
        """Hand keyboard focus to the video once something is actually playing.

        This is what makes the arrow keys control playback straight after you
        press play, instead of still scrolling the channel list. Skipped when a
        text field holds focus, so a queued start cannot steal the caret from
        someone mid-search.
        """
        from PySide6.QtWidgets import (
            QAbstractSpinBox, QApplication, QComboBox, QLineEdit, QPlainTextEdit,
            QTextEdit,
        )

        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
            return
        if isinstance(focused, QComboBox) and focused.isEditable():
            return
        self.surface.setFocus(Qt.OtherFocusReason)

    def set_chrome_visible(self, visible: bool):
        """Show/hide the transport bar (used by fullscreen auto-hide)."""
        if self.controls.isVisible() != visible:
            self.controls.setVisible(visible)

    def detach_bar(self):
        """Hand the bar out so fullscreen can float it over the video.

        Only the bar moves. The video surface must never be reparented: libVLC
        is bound to its native handle and loses the drawable if it moves.
        """
        self.layout().removeWidget(self.bar)
        self.bar.setParent(None)
        return self.bar

    def attach_bar(self):
        self.bar.setParent(self)
        self.layout().addWidget(self.bar)
        self.bar.show()

    def stop(self):
        self._pending = None
        self._switch_timer.stop()
        self._poll.stop()
        if self.player is not None:
            try:
                self.player.stop()
                self.player.set_media(None)
            except Exception:
                pass
        self._current_url = None
        self._ab = (None, None)
        self.bar.reset()
        self.overlay.hide()
        self.playbackStopped.emit()

    def toggle_pause(self):
        if self.player is None or not self._current_url:
            return
        self.player.pause()
        self.bar.set_playing(bool(self.player.is_playing()))

    def is_playing(self) -> bool:
        try:
            return bool(self.player.is_playing()) if self.player else False
        except Exception:
            return False

    def set_volume(self, value: int):
        if self.player is not None:
            self.player.audio_set_volume(int(value))

    def restart_after_error(self):
        """Rebuild the engine rather than reuse a poisoned MediaPlayer."""
        url, title, live = self._current_url, self._current_title, self._is_live
        self._release_player()
        self._create_player()
        if url:
            self.play(url, title, live)

    # ---------------------------------------------------------- seek/state

    def seek_fraction(self, fraction: float):
        if self.player is not None and self._seekable:
            try:
                self.player.set_position(max(0.0, min(1.0, float(fraction))))
            except Exception:
                pass

    def seek_relative(self, seconds: int) -> bool:
        """VLC's arrow-key jumps. False when the stream cannot be seeked."""
        if self.player is None or not self._seekable:
            return False
        try:
            target = clamp_seek(self.player.get_time(), seconds,
                                self.player.get_length())
            self.player.set_time(int(target))
        except Exception:
            return False
        # Repaint now rather than waiting up to 500 ms for the next tick, or
        # the bar lags visibly behind the keypress.
        self.refresh_now()
        return True

    def refresh_now(self):
        self._tick()

    def position_secs(self) -> int:
        if self.player is None:
            return 0
        try:
            return max(0, self.player.get_time() // 1000)
        except Exception:
            return 0

    def duration_secs(self) -> int:
        if self.player is None:
            return 0
        try:
            return max(0, self.player.get_length() // 1000)
        except Exception:
            return 0

    def _tick(self):
        if self.player is None:
            return
        try:
            playing = self.player.is_playing()
            length = self.player.get_length()
            current = self.player.get_time()
        except Exception:
            return

        if playing:
            self.overlay.hide()
        self.bar.set_playing(bool(playing))

        self._seekable = bool(length and length > 0 and not self._is_live)

        if self._resume_to and self._seekable:
            try:
                self.player.set_time(int(self._resume_to * 1000))
            except Exception:
                pass
            self._resume_to = 0

        position = max(0, current // 1000)
        duration = max(0, length // 1000)
        self._enforce_ab_loop(position)
        self.bar.update_position(position, duration, self._seekable)

        if self._seekable:
            self.positionChanged.emit(current / length, position, duration)

    # ------------------------------------------------------------ A-B loop

    def cycle_ab_loop(self) -> str:
        """VLC's A→B button: first click marks A, second B, third clears."""
        start, end = self._ab
        if not self._seekable:
            self._ab = (None, None)
            return "off"
        now = self.position_secs()
        if start is None:
            self._ab = (now, None)
            return "a"
        if end is None:
            if now <= start:
                self._ab = (None, None)
                return "off"
            self._ab = (start, now)
            return "ab"
        self._ab = (None, None)
        return "off"

    def ab_loop(self):
        return self._ab

    def _enforce_ab_loop(self, position: int):
        start, end = self._ab
        if start is None or end is None or not self._seekable:
            return
        if position >= end or position < start - 1:
            try:
                self.player.set_time(int(start * 1000))
            except Exception:
                pass

    # ------------------------------------------------------- audio / video

    def is_muted(self) -> bool:
        # libVLC's audio_get_mute() returns -1 when it does not know, which is
        # truthy; tracking it here avoids reporting "muted" for "unknown".
        return self._muted

    def set_muted(self, muted: bool):
        self._muted = bool(muted)
        if self.player is not None:
            try:
                self.player.audio_set_mute(self._muted)
            except Exception:
                pass

    def toggle_mute(self) -> bool:
        self.set_muted(not self._muted)
        return self._muted

    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float):
        self._rate = float(rate)
        if self.player is not None:
            try:
                self.player.set_rate(self._rate)
            except Exception:
                pass

    def next_frame(self):
        """Step one frame, pausing first — as VLC's own frame-step button does.

        `next_frame()` is a no-op on a *playing* stream: libVLC only advances a
        paused one. Without the pause the button looked dead.
        """
        if self.player is None:
            return
        try:
            if self.player.is_playing():
                self.player.set_pause(1)
            self.player.next_frame()
            self.bar.set_playing(False)
        except Exception:
            pass

    def aspect_ratio(self) -> str:
        return self._aspect

    def set_aspect_ratio(self, value: str):
        self._aspect = value or ""
        if self.player is not None:
            try:
                self.player.video_set_aspect_ratio(self._aspect or None)
            except Exception:
                pass

    def crop(self) -> str:
        return self._crop

    def set_crop(self, value: str):
        self._crop = value or ""
        if self.player is not None:
            try:
                self.player.video_set_crop_geometry(self._crop or None)
            except Exception:
                pass

    def deinterlace(self) -> str:
        return self._deinterlace

    def set_deinterlace(self, value: str):
        self._deinterlace = value or ""
        if self.player is not None:
            try:
                self.player.video_set_deinterlace(self._deinterlace or None)
            except Exception:
                pass

    def take_snapshot(self) -> str:
        if self.player is None or not self._current_url:
            return ""
        from core.db import app_dir

        directory = Path(self.snapshot_dir) if self.snapshot_dir else app_dir() / "snapshots"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        stem = "".join(ch for ch in (self._current_title or "snapshot")
                       if ch.isalnum() or ch in " -_")[:60].strip() or "snapshot"
        target = directory / f"{stem} {time.strftime('%Y-%m-%d %H.%M.%S')}.png"
        try:
            if self.player.video_take_snapshot(0, str(target), 0, 0) != 0:
                return ""
        except Exception:
            return ""
        return str(target) if target.exists() else ""

    # ----------------------------------------------------------- equalizer

    def equalizer_state(self):
        return self._equalizer_state

    def set_equalizer(self, enabled: bool, preamp: float, amps) -> bool:
        """Apply a 10-band equalizer.

        The AudioEqualizer handle is stored on the widget because libVLC does
        not copy it — letting it be garbage-collected takes the filter with it.
        """
        self._equalizer_state = (bool(enabled), float(preamp), list(amps))
        if self.player is None or self._vlc is None:
            return False
        vlc = self._vlc
        try:
            if not enabled:
                self.player.set_equalizer(None)
                self._equalizer = None
                return True
            equalizer = vlc.AudioEqualizer()
            equalizer.set_preamp(float(preamp))
            for index, amp in enumerate(amps):
                equalizer.set_amp_at_index(float(amp), index)
            self._equalizer = equalizer
            return self.player.set_equalizer(equalizer) == 0
        except Exception:
            return False

    def set_video_adjust(self, values: dict):
        """Contrast/brightness/hue/saturation/gamma, as VLC's Effects panel."""
        self._adjust = {**ADJUST_NEUTRAL, **(values or {})}
        if self.player is None or self._vlc is None:
            return
        vlc = self._vlc
        option = vlc.VideoAdjustOption
        active = any(self._adjust[key] != neutral for key, neutral in ADJUST_NEUTRAL.items())
        try:
            # Enable first: libVLC only inserts the adjust filter once it is on,
            # and values set before that are dropped.
            self.player.video_set_adjust_int(option.Enable, 1 if active else 0)
            if active:
                self.player.video_set_adjust_float(option.Contrast, float(self._adjust["contrast"]))
                self.player.video_set_adjust_float(option.Brightness, float(self._adjust["brightness"]))
                self.player.video_set_adjust_float(option.Saturation, float(self._adjust["saturation"]))
                self.player.video_set_adjust_float(option.Gamma, float(self._adjust["gamma"]))
                self.player.video_set_adjust_int(option.Hue, int(self._adjust["hue"]))
        except Exception:
            pass

    def video_adjust(self) -> dict:
        return dict(self._adjust)

    # ------------------------------------------------------------ subtitles

    def add_subtitle_file(self, path: str) -> bool:
        if self.player is None:
            return False
        try:
            uri = Path(path).absolute().as_uri()
            media = self.player.get_media()
            if media is not None:
                media.slaves_add(self._vlc.MediaSlaveType.subtitle, 4, uri)
            result = self.player.video_set_subtitle_file(path)
            return bool(result) or media is not None
        except Exception:
            return False

    def subtitle_tracks(self) -> list:
        return self._tracks(self.player.video_get_spu_description if self.player else None)

    def audio_tracks(self) -> list:
        return self._tracks(self.player.audio_get_track_description if self.player else None)

    def current_subtitle_track(self) -> int:
        try:
            return int(self.player.video_get_spu()) if self.player else -1
        except Exception:
            return -1

    def current_audio_track(self) -> int:
        try:
            return int(self.player.audio_get_track()) if self.player else -1
        except Exception:
            return -1

    @staticmethod
    def _tracks(getter) -> list:
        if getter is None:
            return []
        try:
            return [
                (tid, name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name))
                for tid, name in (getter() or [])
            ]
        except Exception:
            return []

    def set_subtitle_track(self, track_id: int):
        if self.player is not None:
            try:
                self.player.video_set_spu(int(track_id))
            except Exception:
                pass

    def set_audio_track(self, track_id: int):
        if self.player is not None:
            try:
                self.player.audio_set_track(int(track_id))
            except Exception:
                pass

    def set_subtitle_delay(self, ms: int):
        if self.player is not None:
            try:
                self.player.video_set_spu_delay(int(ms) * 1000)
            except Exception:
                pass

    def shutdown(self):
        self._poll.stop()
        self._switch_timer.stop()
        self._release_player()
