"""VLC's control bar, rebuilt in this app's palette.

Layout follows VLC's own, top to bottom:

    [elapsed]  ═══════════●───────────────  [−remaining]
    ● snap ▣ A↔B                1.00x  Auto  AUDIO  SUBS
    ▶ ⏮ ⏭ ■   ⛶ ⚙ ☰   ⟲ ⇄                    🔊 ▬▬▬●──

The middle row is VLC's *advanced controls*, toggled from the View menu just as
it is there.

Everything that is a libVLC operation is done straight through `self.player`;
only the things the application owns — stepping to the next item in the list,
recording, showing the browse panes, subtitle search — leave as signals. That
split is deliberate: the bar has no idea what a "playlist" or a "catalog" is.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QSlider,
    QStyle, QToolTip, QVBoxLayout, QWidget,
)

from ui import icons

# (label, libVLC value). "" means let VLC decide.
ASPECTS = (
    ("Default", ""), ("16:9", "16:9"), ("4:3", "4:3"), ("1:1", "1:1"),
    ("16:10", "16:10"), ("2.21:1", "221:100"), ("2.35:1", "235:100"),
    ("2.39:1", "239:100"), ("5:4", "5:4"),
)
CROPS = (
    ("Default", ""), ("16:9", "16:9"), ("4:3", "4:3"), ("1:1", "1:1"),
    ("16:10", "16:10"), ("2.21:1", "221:100"), ("2.35:1", "235:100"),
    ("2.39:1", "239:100"), ("5:4", "5:4"),
)
DEINTERLACE = (
    ("Off", ""), ("Blend", "blend"), ("Bob", "bob"), ("Discard", "discard"),
    ("Linear", "linear"), ("Mean", "mean"), ("X", "x"), ("Yadif", "yadif"),
    ("Yadif (2x)", "yadif2x"),
)
RATES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 4.0)

LOOP_MODES = ("off", "all", "one")
LOOP_TOOLTIPS = {
    "off": "Loop: off",
    "all": "Loop: repeat the whole list",
    "one": "Loop: repeat this item",
}

ICON_SIZE = 19


def fmt_time(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class TransportButton(QPushButton):
    """Icon button whose artwork carries its own hover and disabled colours."""

    def __init__(self, name: str, tooltip: str, parent=None, colour: str = icons.FG):
        super().__init__(parent)
        self.setObjectName("transportButton")
        self._icon_name = name
        self._colour = colour
        self.setIcon(icons.icon(name, ICON_SIZE, colour))
        self.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.setToolTip(tooltip)
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def set_glyph(self, name: str, colour: str | None = None):
        colour = colour or self._colour
        if (name, colour) == (self._icon_name, self._colour):
            return
        self._icon_name, self._colour = name, colour
        self.setIcon(icons.icon(name, ICON_SIZE, colour))


class ToggleButton(TransportButton):
    """A latching button — loop, shuffle, mute, A↔B.

    Qt does not re-run a stylesheet when a dynamic property changes, so the
    unpolish/polish pair is required or the "on" rule never repaints.
    """

    def __init__(self, name: str, tooltip: str, parent=None):
        super().__init__(name, tooltip, parent)
        self.setObjectName("transportToggle")
        self.setProperty("on", False)

    def set_on(self, on: bool):
        on = bool(on)
        if self.property("on") == on:
            return
        self.setProperty("on", on)
        self.set_glyph(self._icon_name, icons.ACCENT if on else icons.FG)
        self.style().unpolish(self)
        self.style().polish(self)


class ChipButton(QPushButton):
    """Small text button that drops a menu: speed, aspect, AUDIO, SUBS."""

    def __init__(self, text: str, tooltip: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("transportChip")
        self.setToolTip(tooltip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumWidth(48)

    def drop_menu(self, menu: QMenu):
        """Open upwards, so the bar does not cover the video with a menu."""
        height = menu.sizeHint().height()
        menu.exec(self.mapToGlobal(QPoint(0, -height)))


class SeekSlider(QSlider):
    """Click-anywhere-to-seek, with VLC's hover preview of the target time."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setObjectName("seekSlider")
        self.setRange(0, 1000)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self._duration = 0

    def set_duration(self, seconds: int):
        self._duration = max(0, int(seconds or 0))

    def _fraction_at(self, x: int) -> float:
        value = QStyle.sliderValueFromPosition(0, 1000, x, max(1, self.width()))
        return min(1.0, max(0.0, value / 1000.0))

    def mousePressEvent(self, event):
        # Jump to the click, then hand the event on so dragging still works —
        # the handle is under the cursor by then, so Qt starts a normal drag.
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.setValue(int(round(self._fraction_at(int(event.position().x())) * 1000)))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isEnabled() and self._duration > 0:
            target = self._fraction_at(int(event.position().x())) * self._duration
            QToolTip.showText(event.globalPosition().toPoint(), fmt_time(target), self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class VolumeSlider(QSlider):
    """Wheel steps by 5, as VLC does."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setObjectName("volumeSlider")
        self.setRange(0, 125)          # VLC allows boosting past 100
        self.setValue(85)
        self.setSingleStep(5)
        self.setPageStep(10)
        self.setFixedWidth(96)
        self.setFocusPolicy(Qt.NoFocus)

    def wheelEvent(self, event):
        step = 5 if event.angleDelta().y() > 0 else -5
        self.setValue(max(0, min(self.maximum(), self.value() + step)))
        event.accept()


class TransportBar(QWidget):
    """The bar. Owns its widgets; drives `player` directly."""

    previousRequested = Signal()
    nextRequested = Signal()
    recordRequested = Signal()
    playlistToggled = Signal()
    fullscreenRequested = Signal()
    pipRequested = Signal()
    effectsRequested = Signal()
    subtitleSearchRequested = Signal()
    subtitleFileRequested = Signal()
    message = Signal(str)

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.setObjectName("transportBar")
        self._loop_index = 0
        self._shuffle = False
        self._show_remaining = True
        self._live = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 7)
        root.setSpacing(5)
        root.addLayout(self._build_seek_row())
        self.advanced_row = self._build_advanced_row()
        root.addWidget(self.advanced_row)
        root.addLayout(self._build_main_row())

    # ------------------------------------------------------------------ ui

    def _build_seek_row(self):
        row = QHBoxLayout()
        row.setSpacing(9)

        self.elapsed_label = QLabel("--:--")
        self.elapsed_label.setObjectName("timeLabel")
        self.elapsed_label.setMinimumWidth(52)

        self.seek = SeekSlider()
        self.seek.setEnabled(False)
        self.seek.sliderPressed.connect(self._seek_pressed)
        self.seek.sliderReleased.connect(self._seek_released)
        self._seeking = False

        self.total_label = QLabel("--:--")
        self.total_label.setObjectName("timeLabel")
        self.total_label.setMinimumWidth(58)
        self.total_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.total_label.setCursor(Qt.PointingHandCursor)
        self.total_label.setToolTip("Click to switch between remaining and total time")
        self.total_label.mousePressEvent = self._toggle_time_display

        row.addWidget(self.elapsed_label)
        row.addWidget(self.seek, 1)
        row.addWidget(self.total_label)
        return row

    def _build_advanced_row(self):
        holder = QWidget()
        holder.setObjectName("advancedRow")
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.btn_record = TransportButton(
            "record", "Record this channel or download this title",
            colour=icons.RECORD,
        )
        self.btn_record.clicked.connect(self.recordRequested)

        self.btn_snapshot = TransportButton("snapshot", "Take a snapshot")
        self.btn_snapshot.clicked.connect(self._snapshot)

        self.btn_frame = TransportButton("frame_step", "Advance one frame")
        self.btn_frame.clicked.connect(self._frame_step)

        self.btn_ab = ChipButton("A↔B", "Loop between two points: click for A, again for B, again to clear")
        self.btn_ab.setObjectName("transportChipToggle")
        self.btn_ab.setProperty("on", False)
        self.btn_ab.clicked.connect(self._cycle_ab)

        self.rate_chip = ChipButton("1.00x", "Playback speed")
        self.rate_chip.clicked.connect(self._rate_menu)

        self.video_chip = ChipButton("Auto", "Aspect ratio, crop and deinterlacing")
        self.video_chip.clicked.connect(self._video_menu)

        self.audio_chip = ChipButton("AUDIO", "Audio track")
        self.audio_chip.clicked.connect(self._audio_menu)

        self.subs_chip = ChipButton("SUBS", "Subtitle track")
        self.subs_chip.clicked.connect(self._subtitle_menu)

        for widget in (self.btn_record, self.btn_snapshot, self.btn_frame, self.btn_ab):
            row.addWidget(widget)
        row.addStretch(1)
        for widget in (self.rate_chip, self.video_chip, self.audio_chip, self.subs_chip):
            row.addWidget(widget)
        return holder

    def _build_main_row(self):
        row = QHBoxLayout()
        row.setSpacing(4)

        self.btn_play = TransportButton("play", "Play / Pause  (Space)")
        self.btn_play.clicked.connect(self.player.toggle_pause)
        self.btn_prev = TransportButton("previous", "Previous item")
        self.btn_prev.clicked.connect(self.previousRequested)
        self.btn_next = TransportButton("next", "Next item")
        self.btn_next.clicked.connect(self.nextRequested)
        self.btn_stop = TransportButton("stop", "Stop")
        self.btn_stop.clicked.connect(self.player.stop)

        self.btn_full = TransportButton("fullscreen", "Fullscreen  (F)")
        self.btn_full.clicked.connect(self.fullscreenRequested)
        # Latching, because it is a mode: the window can also be put into and
        # taken out of it by the menu, P, Escape and double-click, so the state
        # is pushed back here with set_pip() rather than tracked locally.
        self.btn_pip = ToggleButton("pip", "Picture-in-Picture  (P)")
        self.btn_pip.clicked.connect(self.pipRequested)
        self.btn_effects = TransportButton("effects", "Adjustments and effects")
        self.btn_effects.clicked.connect(self.effectsRequested)
        self.btn_playlist = TransportButton("playlist", "Show or hide the browser")
        self.btn_playlist.clicked.connect(self.playlistToggled)

        self.btn_loop = ToggleButton("loop", LOOP_TOOLTIPS["off"])
        self.btn_loop.clicked.connect(self._cycle_loop)
        self.btn_shuffle = ToggleButton("shuffle", "Random")
        self.btn_shuffle.clicked.connect(self._toggle_shuffle)

        self.btn_mute = ToggleButton("volume", "Mute")
        self.btn_mute.clicked.connect(self.toggle_mute)
        self.volume = VolumeSlider()
        self.volume.valueChanged.connect(self._volume_changed)

        row.addWidget(self.btn_play)
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_next)
        row.addWidget(self.btn_stop)
        row.addWidget(self._separator())
        row.addWidget(self.btn_full)
        row.addWidget(self.btn_pip)
        row.addWidget(self.btn_effects)
        row.addWidget(self.btn_playlist)
        row.addWidget(self._separator())
        row.addWidget(self.btn_loop)
        row.addWidget(self.btn_shuffle)
        row.addStretch(1)
        row.addWidget(self.btn_mute)
        row.addWidget(self.volume)
        return row

    @staticmethod
    def _separator():
        # A plain widget, not a QFrame VLine: a styled QFrame draws its line
        # from the palette and ignores the stylesheet's background-color.
        line = QWidget()
        line.setObjectName("transportSeparator")
        line.setFixedWidth(1)
        line.setFixedHeight(18)
        line.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return line

    # ------------------------------------------------------------- seeking

    def _seek_pressed(self):
        self._seeking = True

    def _seek_released(self):
        self._seeking = False
        self.player.seek_fraction(self.seek.value() / 1000.0)

    def _toggle_time_display(self, event):
        self._show_remaining = not self._show_remaining
        self.player.refresh_now()

    # ------------------------------------------------------------- actions

    def _snapshot(self):
        path = self.player.take_snapshot()
        self.message.emit(f"Snapshot saved to {path}" if path
                          else "Could not take a snapshot of this stream")

    def _frame_step(self):
        self.player.next_frame()

    def _cycle_ab(self):
        state = self.player.cycle_ab_loop()
        labels = {"off": "A↔B", "a": "A↔•", "ab": "A↔B"}
        self.btn_ab.setText(labels.get(state, "A↔B"))
        self.btn_ab.setProperty("on", state != "off")
        self.btn_ab.style().unpolish(self.btn_ab)
        self.btn_ab.style().polish(self.btn_ab)
        self.message.emit({
            "a": "Loop start set — click A↔B again to set the end",
            "ab": "Looping between the two points",
            "off": "A↔B loop cleared",
        }[state])

    def _cycle_loop(self):
        self._loop_index = (self._loop_index + 1) % len(LOOP_MODES)
        mode = self.loop_mode
        self.btn_loop.set_glyph("loop_one" if mode == "one" else "loop",
                                icons.ACCENT if mode != "off" else icons.FG)
        self.btn_loop.set_on(mode != "off")
        self.btn_loop.setToolTip(LOOP_TOOLTIPS[mode])
        self.message.emit(LOOP_TOOLTIPS[mode])

    def _toggle_shuffle(self):
        self._shuffle = not self._shuffle
        self.btn_shuffle.set_on(self._shuffle)
        self.message.emit("Random: on" if self._shuffle else "Random: off")

    def toggle_mute(self):
        muted = self.player.toggle_mute()
        self.btn_mute.set_glyph("volume_muted" if muted else "volume",
                                icons.ACCENT if muted else icons.FG)
        self.btn_mute.set_on(muted)
        self.btn_mute.setToolTip("Unmute" if muted else "Mute")

    def set_pip(self, on: bool):
        """Latch the PiP button to match the window's actual mode."""
        self.btn_pip.set_on(bool(on))
        self.btn_pip.setToolTip("Leave Picture-in-Picture  (P)" if on
                                else "Picture-in-Picture  (P)")

    def nudge_volume(self, delta: int) -> int:
        """Step the volume and return the new level.

        Goes through the slider rather than the player so the control, the
        engine and the mute state all stay in step - `_volume_changed` already
        un-mutes on the way up.
        """
        value = max(0, min(self.volume.maximum(), self.volume.value() + int(delta)))
        self.volume.setValue(value)
        return value

    def _volume_changed(self, value: int):
        self.player.set_volume(value)
        if self.player.is_muted() and value > 0:
            self.player.set_muted(False)
            self.btn_mute.set_glyph("volume", icons.FG)
            self.btn_mute.set_on(False)

    # --------------------------------------------------------------- menus

    def _rate_menu(self):
        menu = QMenu(self)
        current = self.player.rate()
        for rate in RATES:
            action = menu.addAction(f"{rate:.2f}x")
            action.setCheckable(True)
            action.setChecked(abs(rate - current) < 0.01)
            action.triggered.connect(lambda _=False, r=rate: self.apply_rate(r))
        menu.addSeparator()
        menu.addAction("Normal speed", lambda: self.apply_rate(1.0))
        self.rate_chip.drop_menu(menu)

    def apply_rate(self, rate: float):
        self.player.set_rate(rate)
        self.rate_chip.setText(f"{rate:.2f}x")

    def _video_menu(self):
        menu = QMenu(self)
        aspect = menu.addMenu("Aspect ratio")
        self._fill_choice(aspect, ASPECTS, self.player.aspect_ratio(),
                          self._set_aspect)
        crop = menu.addMenu("Crop")
        self._fill_choice(crop, CROPS, self.player.crop(), self.player.set_crop)
        deint = menu.addMenu("Deinterlace")
        self._fill_choice(deint, DEINTERLACE, self.player.deinterlace(),
                          self.player.set_deinterlace)
        self.video_chip.drop_menu(menu)

    def _set_aspect(self, value: str):
        self.player.set_aspect_ratio(value)
        label = next((name for name, val in ASPECTS if val == value), "Auto")
        self.video_chip.setText("Auto" if label == "Default" else label)

    @staticmethod
    def _fill_choice(menu, options, current, setter):
        for label, value in options:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked((value or "") == (current or ""))
            action.triggered.connect(lambda _=False, v=value: setter(v))

    def _audio_menu(self):
        menu = QMenu(self)
        tracks = self.player.audio_tracks()
        if not tracks:
            menu.addAction("No audio tracks").setEnabled(False)
        current = self.player.current_audio_track()
        for track_id, name in tracks:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(track_id == current)
            action.triggered.connect(
                lambda _=False, t=track_id: self.player.set_audio_track(t))
        self.audio_chip.drop_menu(menu)

    def _subtitle_menu(self):
        menu = QMenu(self)
        tracks = self.player.subtitle_tracks()
        current = self.player.current_subtitle_track()
        if not tracks:
            menu.addAction("No subtitle tracks").setEnabled(False)
        for track_id, name in tracks:
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(track_id == current)
            action.triggered.connect(
                lambda _=False, t=track_id: self.player.set_subtitle_track(t))
        menu.addSeparator()
        menu.addAction("Search with VLSub…", self.subtitleSearchRequested.emit)
        menu.addAction("Add subtitle file…", self.subtitleFileRequested.emit)
        self.subs_chip.drop_menu(menu)

    # --------------------------------------------------------------- state

    @property
    def loop_mode(self) -> str:
        return LOOP_MODES[self._loop_index]

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    def restore(self, loop_mode: str, shuffle: bool, advanced: bool, volume: int):
        if loop_mode in LOOP_MODES:
            self._loop_index = LOOP_MODES.index(loop_mode)
            mode = self.loop_mode
            self.btn_loop.set_glyph("loop_one" if mode == "one" else "loop",
                                    icons.ACCENT if mode != "off" else icons.FG)
            self.btn_loop.set_on(mode != "off")
            self.btn_loop.setToolTip(LOOP_TOOLTIPS[mode])
        self._shuffle = bool(shuffle)
        self.btn_shuffle.set_on(self._shuffle)
        self.advanced_row.setVisible(bool(advanced))
        self.volume.setValue(int(volume))

    def set_advanced_visible(self, visible: bool):
        self.advanced_row.setVisible(bool(visible))

    def set_live(self, is_live: bool):
        """Grey out what a live stream cannot do, exactly as VLC does."""
        self._live = bool(is_live)
        for widget in (self.btn_frame, self.btn_ab, self.rate_chip):
            widget.setEnabled(not is_live)

    def set_playing(self, playing: bool):
        self.btn_play.set_glyph("pause" if playing else "play")

    def update_position(self, position: int, duration: int, seekable: bool):
        self.seek.setEnabled(seekable)
        self.seek.set_duration(duration)
        if seekable and duration > 0:
            if not self._seeking:
                self.seek.setValue(int(1000 * min(1.0, max(0.0, position / duration))))
            self.elapsed_label.setText(fmt_time(position))
            self.total_label.setText(
                f"−{fmt_time(duration - position)}" if self._show_remaining
                else fmt_time(duration)
            )
        elif self._live:
            self.seek.setValue(0)
            self.elapsed_label.setText(fmt_time(position))
            self.total_label.setText("LIVE")
        else:
            self.seek.setValue(0)
            self.elapsed_label.setText("--:--")
            self.total_label.setText("--:--")

    def reset(self):
        self.seek.setValue(0)
        self.seek.setEnabled(False)
        self.elapsed_label.setText("--:--")
        self.total_label.setText("--:--")
        self.btn_play.set_glyph("play")
        self.rate_chip.setText("1.00x")
        self.btn_ab.setText("A↔B")
        self.btn_ab.setProperty("on", False)
        self.btn_ab.style().unpolish(self.btn_ab)
        self.btn_ab.style().polish(self.btn_ab)
