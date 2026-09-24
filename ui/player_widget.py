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

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStackedLayout,
    QVBoxLayout, QWidget,
)

from core import vlc_setup
from ui.transport_bar import TransportBar

SWITCH_DEBOUNCE_MS = 300
# A second request arriving faster than this looks like key-repeat rather than
# a deliberate choice, so it gets debounced; a lone click starts at once.
REPEAT_WINDOW_MS = 400
# An "end" this far from the known duration is a dropped stream, not the
# credits: the CDN closed the socket mid-episode, and libVLC reports that with
# the same EndReached it uses for a real finish.
EARLY_END_SLACK_S = 30
EARLY_END_RETRIES = 4
# Ticks (½ s each) over which the volume is re-asserted after a start: the
# audio output is created when the first samples arrive, and PulseAudio's
# stream-restore then hands it whatever level it remembers for "VLC".
VOLUME_SYNC_TICKS = 10
SUBTITLE_SYNC_TICKS = 40

# Neutral values for VLC's video adjustments; all-neutral means the filter is
# left switched off rather than inserted into the chain for nothing.
ADJUST_NEUTRAL = {
    "contrast": 1.0, "brightness": 1.0, "saturation": 1.0, "gamma": 1.0, "hue": 0,
}

# Keep a forward seek this far from the end. Landing exactly on the duration
# trips end-of-media, so the arrow key would advance to the next item instead
# of seeking - which looks like the key doing something entirely different.
SEEK_TAIL_MS = 1000

# The up-next card's thumbnail, 16:9 at a size that survives the PiP window.
UP_NEXT_STILL = (280, 158)


def _using_pulse() -> bool:
    """True when a PulseAudio / PipeWire socket is there to play through."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("PULSE_SERVER"):
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if not runtime:
        return False
    return (Path(runtime, "pulse", "native").exists()
            or Path(runtime, "pipewire-0").exists())


def _linux_audio_args() -> list[str]:
    """Route audio through the sound server so it follows the system default.

    libVLC's default output picker can land on the ALSA plugin, which opens one
    HDMI/analog device directly and exclusively: it does not follow the desktop
    default, and it fights whatever suspended the device (on this projector the
    HDMI sink drops the start of a stream, or comes up silent, every time it has
    to wake). Pinning the PulseAudio output - which is really PipeWire here -
    makes the app behave like a browser: one stream on the default sink, moved
    automatically when the default changes. Only forced when a PulseAudio or
    PipeWire socket is actually present, so a pure-ALSA box is left alone.
    """
    return ["--aout=pulse"] if _using_pulse() else []


# What an English subtitle track gets called, across providers and muxers.
ENGLISH_TOKENS = frozenset({"english", "eng", "en"})


def _tokens(name: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9]+", (name or "").lower())


def match_track(want: str, tracks):
    """The id of the track named like `want`, or None.

    Exact first, then a loose match: providers label the same language
    "English", "eng", "English [SDH]" from one file to the next.
    """
    want_l = (want or "").strip().lower()
    if not want_l:
        return None
    for tid, name in tracks:
        if name.strip().lower() == want_l:
            return tid
    for tid, name in tracks:
        low = name.strip().lower()
        if low and (want_l in low or low in want_l):
            return tid
    return None


def english_track(tracks):
    """The id of the first track labelled as English, or None.

    Whole-word: "en" must not light up on "French", and "Track 1 - [eng]"
    still counts. A plain "English" beats "English [Forced]"/"SDH" variants
    when both exist, since the plain one is what most people want by default.
    """
    plain = None
    any_english = None
    for tid, name in tracks:
        words = _tokens(name)
        if not (set(words) & ENGLISH_TOKENS):
            continue
        if any_english is None:
            any_english = tid
        if plain is None and not (set(words) & {"forced", "sdh", "cc", "commentary"}):
            plain = tid
    return plain if plain is not None else any_english


class _AudioKeepAlive:
    """Hold the audio device open with an inaudible stream, VLC's absence of.

    On HDMI / S-PDIF the codec parks when nothing is playing, and every start,
    stop and pause then costs an audible pop or the first fraction of a second
    of sound while the link re-locks. A second libVLC that loops pure silence
    keeps the device permanently awake for as long as the app is open, so the
    real stream never pays that cost - pausing included. Best-effort: any
    failure here must never take playback down with it, so everything is
    guarded and a broken keep-alive just does nothing.
    """

    def __init__(self):
        self._instance = None
        self._player = None
        self._path = None

    def start(self):
        if self._player is not None or not _using_pulse():
            return
        try:
            import vlc

            self._path = self._silence_file()
            self._instance = vlc.Instance("--quiet", "--no-video", "--aout=pulse")
            self._player = self._instance.media_player_new()
            media = self._instance.media_new(self._path)
            media.add_option("input-repeat=65535")   # ~18 h of a 1 s clip; a session
            self._player.set_media(media)
            media.release()
            # Not volume 0: PulseAudio remembers a level per application, and
            # this instance is "VLC" just like the real one - a muted keep-alive
            # was being restored onto the next stream, which then came up silent
            # until the wheel touched the slider. The file is silence anyway.
            self._player.play()
        except Exception:
            self.stop()

    def stop(self):
        for obj, release in ((self._player, "stop"), (self._instance, None)):
            if obj is None:
                continue
            try:
                if release:
                    getattr(obj, release)()
                obj.release()
            except Exception:
                pass
        self._player = self._instance = None

    @staticmethod
    def _silence_file() -> str:
        """A one-second stereo 48 kHz WAV of zeros, written once to a temp path."""
        import struct
        import tempfile
        import wave

        path = Path(tempfile.gettempdir()) / "iptvplayer-keepalive.wav"
        if not path.exists():
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(48000)
                handle.writeframes(struct.pack("<%dh" % (48000 * 2), *([0] * 48000 * 2)))
        return str(path)


def clamp_seek(position_ms: int, delta_s: int, duration_ms: int) -> int:
    """Where a relative jump should land, kept inside the media."""
    target = int(position_ms) + int(delta_s) * 1000
    if duration_ms and duration_ms > 0:
        target = min(target, max(0, int(duration_ms) - SEEK_TAIL_MS))
    return max(0, target)


class VideoSurface(QFrame):
    """Native window libVLC renders into. Must stay opaque and un-styled."""

    clicked = Signal(float)            # a single left click, sent at once; x 0..1
    doubleClicked = Signal(float)      # x as a fraction of the width, 0..1
    wheelScrolled = Signal(int, int)   # angleDelta().y(), int(modifiers)

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

    def mousePressEvent(self, event):
        # Reported immediately rather than after the double-click interval: a
        # pause that lands 400 ms late feels broken. A double-click therefore
        # always arrives after one click has already been acted on, and the
        # receiver undoes that first (see PlayerWidget._surface_double_clicked).
        if event.button() == Qt.LeftButton:
            width = max(1, self.width())
            self.clicked.emit(event.position().x() / width)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            width = max(1, self.width())
            self.doubleClicked.emit(event.position().x() / width)
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        # VLC changes the volume on a wheel over the video (Shift to seek). The
        # app-level event filter in MainWindow also covers this, for the wheel
        # events that libVLC's embedded child window forwards to the top-level
        # rather than to this widget; whichever sees the event first handles it.
        delta = event.angleDelta().y()
        if delta:
            self.wheelScrolled.emit(delta, int(event.modifiers().value))
            event.accept()
            return
        super().wheelEvent(event)


class UpNextPanel(QFrame):
    """What an episode ending leaves on screen: the next one, or the end.

    Deliberately knows nothing about the catalog — it is handed a heading, a
    title, a still and a countdown. It shares the player's stacked layout with
    the video surface, which is hidden while this shows: Qt cannot paint over
    libVLC's native view, and this does not try to. By the time it appears the
    media has been stopped, so there is nothing to paint over.
    """

    playRequested = Signal()
    cancelled = Signal()      # the second button: Cancel, or Back to the show

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("upNext")
        self.setCursor(Qt.PointingHandCursor)

        root = QVBoxLayout(self)
        root.setContentsMargins(36, 28, 36, 28)
        root.setSpacing(0)
        root.addStretch(1)

        self.heading = QLabel("")
        self.heading.setObjectName("upNextHeading")
        root.addWidget(self.heading, 0, Qt.AlignHCenter)
        root.addSpacing(14)

        self.still = QLabel()
        self.still.setObjectName("upNextStill")
        self.still.setFixedSize(UP_NEXT_STILL[0], UP_NEXT_STILL[1])
        self.still.setAlignment(Qt.AlignCenter)
        self.still.setScaledContents(False)
        root.addWidget(self.still, 0, Qt.AlignHCenter)
        root.addSpacing(14)

        self.title = QLabel("")
        self.title.setObjectName("upNextTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setWordWrap(True)
        root.addWidget(self.title)

        self.subtitle = QLabel("")
        self.subtitle.setObjectName("upNextSubtitle")
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setWordWrap(True)
        root.addWidget(self.subtitle)
        root.addSpacing(18)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.play_button = QPushButton("Play")
        self.play_button.setObjectName("upNextPlay")
        self.play_button.setCursor(Qt.PointingHandCursor)
        self.play_button.clicked.connect(self.playRequested)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("upNextCancel")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.clicked.connect(self.cancelled)
        buttons.addWidget(self.play_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        root.addLayout(buttons)

        root.addSpacing(10)
        self.hint = QLabel("")
        self.hint.setObjectName("upNextHint")
        self.hint.setAlignment(Qt.AlignCenter)
        root.addWidget(self.hint)
        root.addStretch(1)

    def set_still(self, pixmap):
        """The episode's thumbnail. Hidden outright when there is none — an
        empty bordered rectangle reads as a picture that failed to load."""
        if pixmap is None or pixmap.isNull():
            self.still.clear()
            self.still.hide()
            return
        self.still.show()
        self.still.setPixmap(pixmap.scaled(
            self.still.width(), self.still.height(),
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

    def mouseReleaseEvent(self, event):
        # The whole card is a target, which is the third of the four ways in
        # (the others being Space, the play button, and the countdown).
        if event.button() == Qt.LeftButton and self.play_button.isVisible():
            self.playRequested.emit()
        super().mouseReleaseEvent(event)


class PlayerWidget(QWidget):
    """Video surface + VLC's transport controls."""

    stateChanged = Signal(str)
    positionChanged = Signal(float, int, int)   # fraction, position_s, duration_s
    errorOccurred = Signal(str)
    endReached = Signal()
    playbackStarted = Signal()
    playbackStopped = Signal()
    fullscreenToggled = Signal()
    videoClicked = Signal(float)         # x fraction across the surface
    videoDoubleClicked = Signal(float)
    upNextRequested = Signal()      # play what the card is offering
    upNextDismissed = Signal()      # the end-of-show card's way out
    subtitlePreferenceChanged = Signal(object)   # track name, "" for off

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
        self._volume_sync = 0
        self._last_position = 0
        self._last_duration = 0
        self._drop_retries = 0
        self._drop_at = -1
        self._preferred_subtitle = None   # None: English by default; "": off; else a track name
        self._subtitle_sync = 0
        self._rate = 1.0
        self._aspect = ""
        self._crop = ""
        self._deinterlace = ""
        self._ab = (None, None)
        self._equalizer = None          # must outlive set_equalizer()
        self._equalizer_state = None
        self._adjust = dict(ADJUST_NEUTRAL)
        self.snapshot_dir = None        # set by MainWindow; falls back to app_dir
        # Called just before libVLC is told to stop: the window uses it to cut
        # the play-through cache's connection first, so stop() cannot wait on
        # a socket the player has already stopped reading.
        self.before_stop = None
        self._available = vlc_setup.ensure_vlc()

        self._switch_timer = QTimer(self)
        self._switch_timer.setSingleShot(True)
        self._switch_timer.setInterval(SWITCH_DEBOUNCE_MS)
        self._switch_timer.timeout.connect(self._start_pending)

        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._tick)

        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._countdown_tick)
        self._countdown_left = 0
        self._up_next_mode = "next"

        self._keepalive = _AudioKeepAlive()
        # The keep-alive only runs around actual playback: started when a stream
        # starts, stopped a short while after everything stops. That spans the
        # pauses and channel-hops that pop the audio device without holding it
        # open (and a Bluetooth sink awake) when the app is just sitting idle.
        self._keepalive_off = QTimer(self)
        self._keepalive_off.setSingleShot(True)
        self._keepalive_off.setInterval(8000)
        self._keepalive_off.timeout.connect(self._keepalive.stop)

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
        # Not wired to pause/seek/fullscreen here: what a click should do
        # depends on the window mode, which only MainWindow knows.
        self.surface.clicked.connect(self.videoClicked)
        self.surface.doubleClicked.connect(self.videoDoubleClicked)

        self.overlay = QLabel("")
        self.overlay.setObjectName("playerOverlay")
        self.overlay.setAlignment(Qt.AlignCenter)
        self.overlay.setWordWrap(True)
        self.overlay.hide()

        self.up_next = UpNextPanel()
        self.up_next.hide()
        self.up_next.playRequested.connect(self._up_next_play)
        self.up_next.cancelled.connect(self._secondary_pressed)

        self._stack.addWidget(self.up_next)
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
            # IPTV_VLC_DEBUG=1 turns libVLC's own log on, for chasing vout trouble.
            "--verbose=2" if os.environ.get("IPTV_VLC_DEBUG") else "--quiet",
        ]
        if not self._hw_enabled():
            args.append("--avcodec-hw=none")
        args += _linux_audio_args()
        self.instance = vlc.Instance(*args)
        self.player = self.instance.media_player_new()
        self._attach_surface()
        self._attach_events()
        self._disown_input()
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

    def _disown_input(self):
        """Tell libVLC not to consume mouse and key events on its video window.

        Without this, libVLC's own view over the surface eats the wheel (volume
        / seek), double-click-to-fullscreen and the shortcut keys before Qt
        sees them. On X11/XWayland libVLC builds a fresh video window for each
        media, and it comes back grabbing input, so this is re-stated on every
        playback start rather than only at engine creation.
        """
        if self.player is None:
            return
        try:
            self.player.video_set_mouse_input(False)
            self.player.video_set_key_input(False)
        except Exception:
            pass

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
        # Whatever the card was offering, something is starting now.
        self.hide_up_next()
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
        if url != self._current_url:
            self._drop_retries = 0
            self._drop_at = -1
        self._last_position = self._last_duration = 0
        self._volume_sync = VOLUME_SYNC_TICKS
        self._subtitle_sync = SUBTITLE_SYNC_TICKS
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
            # re-stated here rather than only at engine creation - and so is the
            # input handoff, since libVLC's new video window grabs it again.
            self._apply_engine_settings()
            self._disown_input()
            self._keepalive_off.stop()
            self._keepalive.start()
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

    # ----------------------------------------------------------- up next

    @property
    def up_next_showing(self) -> bool:
        return self.up_next.isVisible()

    def show_up_next(self, title: str, subtitle: str, pixmap, seconds: int = 0):
        """Offer the next episode in the video area.

        The surface is hidden rather than merely covered: Qt cannot paint over
        libVLC's native view, and the caller has already stopped the media, so
        there is nothing left to show there anyway.
        """
        self.up_next.heading.setText("NEXT EPISODE")
        self.up_next.title.setText(title)
        self.up_next.subtitle.setText(subtitle)
        self.up_next.set_still(pixmap)
        self.up_next.play_button.setText("Play")
        self.up_next.play_button.show()
        self.up_next.cancel_button.setVisible(seconds > 0)
        self.up_next.cancel_button.setText("Cancel")
        self._up_next_mode = "next"
        self._reveal_up_next()
        if seconds > 0:
            self._countdown_left = int(seconds)
            self._show_countdown()
            self._countdown.start()
        else:
            self.up_next.hint.setText("Space, the play button, or click the card")

    def show_finished(self, show: str, pixmap=None):
        """The end of the show: nothing to offer, so say so and get out."""
        self.up_next.heading.setText("YOU'VE FINISHED")
        self.up_next.title.setText(show)
        self.up_next.subtitle.setText("")
        self.up_next.set_still(pixmap)
        self.up_next.play_button.hide()          # nothing to play
        self.up_next.cancel_button.show()
        self.up_next.cancel_button.setText("← Back to the show")
        self.up_next.hint.setText("")
        self._up_next_mode = "finished"
        self._reveal_up_next()

    def _reveal_up_next(self):
        self._countdown.stop()
        self.overlay.hide()
        self.surface.hide()
        self.up_next.show()
        self._stack.setCurrentWidget(self.up_next)

    def hide_up_next(self):
        """Put the video area back. Safe to call when no card is showing."""
        self._countdown.stop()
        if not self.up_next.isVisible():
            return
        self.up_next.hide()
        self.surface.show()
        self._stack.setCurrentWidget(self.surface)

    def _secondary_pressed(self):
        """The card's second button, which means different things per card.

        On the up-next card it stops the countdown and leaves the offer up; on
        the end-of-show card it is the only button and means "done". Decided
        from the mode rather than by rewiring the signal, so there is no state
        to leave behind when one card replaces the other.
        """
        if self._up_next_mode == "finished":
            self.upNextDismissed.emit()
            return
        self._countdown.stop()
        self.up_next.cancel_button.hide()
        self.up_next.hint.setText("Space, the play button, or click the card")

    def cancel_countdown(self):
        """Stop the clock but leave the offer standing (the Cancel button)."""
        self._secondary_pressed()

    def _show_countdown(self):
        self.up_next.hint.setText(f"Playing in {self._countdown_left}…")

    def _countdown_tick(self):
        self._countdown_left -= 1
        if self._countdown_left > 0:
            self._show_countdown()
            return
        self._countdown.stop()
        self.upNextRequested.emit()

    def _up_next_play(self):
        self._countdown.stop()
        self.upNextRequested.emit()

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
        # ⏹ while the card is up means "no thanks". The end-of-episode path
        # stops first and shows the card after, so this is a no-op there.
        self.hide_up_next()
        if self.before_stop is not None:
            try:
                self.before_stop()
            except Exception:
                pass
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
        # Let the device idle only after a grace period - a stop is very often
        # the front half of a channel change.
        self._keepalive_off.start()
        self.playbackStopped.emit()

    def toggle_pause(self):
        # Space and the bar's play button both arrive here, so the card only
        # has to be caught once for both of them.
        if self.up_next_showing:
            self._up_next_play()
            return
        if self.player is None or not self._current_url:
            return
        self.player.pause()
        self.bar.set_playing(bool(self.player.is_playing()))

    @property
    def has_media(self) -> bool:
        """Something is loaded and the video area is showing it (no card)."""
        return self.player is not None and bool(self._current_url) and not self.up_next_showing

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
            self._sync_after_start()
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
        if current >= 0:
            self._last_position = position
        if duration > 0:
            self._last_duration = duration
        self._enforce_ab_loop(position)
        self.bar.update_position(position, duration, self._seekable)

        if self._seekable:
            self.positionChanged.emit(current / length, position, duration)

    def _sync_after_start(self):
        """Re-state what a fresh media forgets, once it is actually playing.

        Volume: libVLC only owns a level once the audio output exists, and on
        PulseAudio the output arrives with the server's remembered level, which
        it then reports back over ours. Subtitles: the track the user picked on
        the previous episode, matched by name because the ids are per media,
        and failing that whichever track is labelled English.
        Both are retried for a few ticks since the ES arrive after "playing".
        """
        if self._volume_sync > 0:
            self._volume_sync -= 1
            want = int(self.bar.volume.value())
            try:
                if self.player.audio_get_volume() != want:
                    self.player.audio_set_volume(want)
                self.player.audio_set_mute(self._muted)
            except Exception:
                pass
        if self._subtitle_sync > 0:
            self._subtitle_sync -= 1
            if self._apply_preferred_subtitle():
                self._subtitle_sync = 0

    def _apply_preferred_subtitle(self) -> bool:
        tracks = [(tid, name) for tid, name in self.subtitle_tracks() if tid >= 0]
        if not tracks:
            return False
        want = self._preferred_subtitle
        target = -1
        if want:
            # The track chosen last time, by name: the same series ships the
            # same track labels from one episode to the next.
            target = match_track(want, tracks)
            if target is None:
                target = english_track(tracks)
        elif want is None:
            # Nothing chosen yet: English is the default whenever there is one.
            target = english_track(tracks)
        if target is None:
            return False
        try:
            if self.player.video_get_spu() != target:
                self.player.video_set_spu(target)
        except Exception:
            pass
        return True

    @property
    def preferred_subtitle(self):
        return self._preferred_subtitle

    def set_preferred_subtitle(self, name):
        """The track name to pick on every new media (None to stop doing so)."""
        self._preferred_subtitle = name
        self._subtitle_sync = SUBTITLE_SYNC_TICKS

    # ------------------------------------------------------- dropped stream

    def ended_early(self) -> bool:
        """EndReached arrived with most of the file still to play.

        A VOD that stops well short of its duration has lost its connection;
        treating that as the end would skip to the next episode mid-scene.
        """
        if self._is_live or not self._seekable:
            return False
        if self._last_duration <= 0:
            return False
        if self._last_duration - self._last_position <= EARLY_END_SLACK_S:
            return False
        # Progress since the last drop resets the budget; the same spot dying
        # again and again does not.
        if self._drop_at < 0 or self._last_position > self._drop_at + EARLY_END_SLACK_S:
            self._drop_retries = 0
        return self._drop_retries < EARLY_END_RETRIES

    def resume_after_drop(self):
        """Reopen the same stream a couple of seconds before where it died."""
        self._drop_retries += 1
        self._drop_at = self._last_position
        resume = max(0, self._last_position - 2)
        url, title, live = self._current_url, self._current_title, self._is_live
        self.play(url, title, live, resume_secs=resume)
        self.overlay.setText(f"Reconnecting… ({self._drop_retries}/{EARLY_END_RETRIES})")
        self.overlay.show()

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
        # A deliberate choice from the menu carries over to the next episode.
        names = dict(self.subtitle_tracks())
        if int(track_id) < 0:
            self._preferred_subtitle = ""
        elif int(track_id) in names:
            self._preferred_subtitle = names[int(track_id)]
        self._subtitle_sync = 0
        self.subtitlePreferenceChanged.emit(self._preferred_subtitle)

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
        self._keepalive_off.stop()
        self._keepalive.stop()
        self._release_player()
