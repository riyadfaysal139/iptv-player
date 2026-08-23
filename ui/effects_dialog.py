"""VLC's *Adjustments and Effects* panel.

Two tabs, the same controls VLC has and driven by the same libVLC calls: a
10-band equaliser with the built-in presets, and the video adjustment filter.

Non-modal on purpose — VLC's is too, and an equaliser you cannot hear while you
drag it is useless.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QTabWidget, QVBoxLayout, QWidget,
)

# (setting key, label, minimum, maximum, neutral). Ranges are VLC's own.
VIDEO_ADJUST = (
    ("contrast", "Contrast", 0.0, 2.0, 1.0),
    ("brightness", "Brightness", 0.0, 2.0, 1.0),
    ("saturation", "Saturation", 0.0, 3.0, 1.0),
    ("gamma", "Gamma", 0.01, 10.0, 1.0),
)
HUE_RANGE = (0, 360, 0)

BAND_LIMIT = 20.0        # libVLC clamps band and preamp gain to +/-20 dB


def preset_values(index: int):
    """Preamp and band gains for one of libVLC's built-in presets.

    Deliberately calls the flat C function rather than `vlc.AudioEqualizer(n)`:
    that constructor path in python-vlc treats the integer as a **pointer**
    instead of a preset index, so it segfaults the process. Verified on
    python-vlc 3.0.21203.
    """
    import vlc

    handle = vlc.libvlc_audio_equalizer_new_from_preset(int(index))
    if not handle:
        return 0.0, [0.0] * band_count()
    preamp = float(vlc.libvlc_audio_equalizer_get_preamp(handle))
    amps = [float(vlc.libvlc_audio_equalizer_get_amp_at_index(handle, band))
            for band in range(band_count())]
    return preamp, amps


def band_count() -> int:
    import vlc

    return int(vlc.libvlc_audio_equalizer_get_band_count())


def band_labels() -> list[str]:
    import vlc

    labels = []
    for band in range(band_count()):
        hertz = int(vlc.libvlc_audio_equalizer_get_band_frequency(band))
        labels.append(f"{hertz // 1000}K" if hertz >= 1000 else str(hertz))
    return labels


def preset_names() -> list[str]:
    import vlc

    return [vlc.libvlc_audio_equalizer_get_preset_name(index).decode("utf-8", "replace")
            for index in range(vlc.libvlc_audio_equalizer_get_preset_count())]


class EffectsDialog(QDialog):
    def __init__(self, parent, db, player):
        super().__init__(parent)
        self.setWindowTitle("Adjustments and Effects")
        self.setMinimumWidth(560)
        self.db = db
        self.player = player
        self._loading = True

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_audio_tab(), "Audio effects")
        tabs.addTab(self._build_video_tab(), "Video effects")
        layout.addWidget(tabs)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._load()
        self._loading = False

    # ------------------------------------------------------------ audio ui

    def _build_audio_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        top = QHBoxLayout()
        self.eq_enabled = QCheckBox("Enable")
        self.eq_enabled.toggled.connect(self._audio_changed)
        self.preset = QComboBox()
        self.preset.addItem("Custom", -1)
        for index, name in enumerate(preset_names()):
            self.preset.addItem(name, index)
        self.preset.activated.connect(self._preset_chosen)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset_audio)
        top.addWidget(self.eq_enabled)
        top.addSpacing(12)
        top.addWidget(QLabel("Preset"))
        top.addWidget(self.preset, 1)
        top.addWidget(reset)
        layout.addLayout(top)

        grid = QGridLayout()
        grid.setSpacing(4)
        self.preamp = self._band_slider()
        grid.addWidget(self._value_label(self.preamp), 0, 0, alignment=Qt.AlignHCenter)
        grid.addWidget(self.preamp, 1, 0, alignment=Qt.AlignHCenter)
        grid.addWidget(QLabel("Preamp"), 2, 0, alignment=Qt.AlignHCenter)

        self.bands = []
        for column, label in enumerate(band_labels(), start=1):
            slider = self._band_slider()
            self.bands.append(slider)
            grid.addWidget(self._value_label(slider), 0, column, alignment=Qt.AlignHCenter)
            grid.addWidget(slider, 1, column, alignment=Qt.AlignHCenter)
            grid.addWidget(QLabel(label), 2, column, alignment=Qt.AlignHCenter)
        layout.addLayout(grid)

        note = QLabel("Gain in dB. Presets come from VLC itself.")
        note.setObjectName("statusNote")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _band_slider(self) -> QSlider:
        slider = QSlider(Qt.Vertical)
        slider.setObjectName("bandSlider")
        slider.setRange(int(-BAND_LIMIT * 10), int(BAND_LIMIT * 10))
        slider.setValue(0)
        slider.setMinimumHeight(130)
        slider.valueChanged.connect(self._audio_changed)
        return slider

    @staticmethod
    def _value_label(slider: QSlider) -> QLabel:
        label = QLabel("0.0")
        label.setObjectName("statusNote")
        slider.valueChanged.connect(lambda value: label.setText(f"{value / 10:.1f}"))
        return label

    # ------------------------------------------------------------ video ui

    def _build_video_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)

        self.adjust_sliders = {}
        for row, (key, label, low, high, _neutral) in enumerate(VIDEO_ADJUST):
            slider = QSlider(Qt.Horizontal)
            slider.setRange(int(low * 100), int(high * 100))
            slider.valueChanged.connect(self._video_changed)
            readout = QLabel("1.00")
            readout.setObjectName("statusNote")
            readout.setMinimumWidth(44)
            slider.valueChanged.connect(lambda value, r=readout: r.setText(f"{value / 100:.2f}"))
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(readout, row, 2)
            self.adjust_sliders[key] = slider

        row = len(VIDEO_ADJUST)
        hue = QSlider(Qt.Horizontal)
        hue.setRange(HUE_RANGE[0], HUE_RANGE[1])
        hue.valueChanged.connect(self._video_changed)
        hue_readout = QLabel("0°")
        hue_readout.setObjectName("statusNote")
        hue_readout.setMinimumWidth(44)
        hue.valueChanged.connect(lambda value: hue_readout.setText(f"{value}°"))
        grid.addWidget(QLabel("Hue"), row, 0)
        grid.addWidget(hue, row, 1)
        grid.addWidget(hue_readout, row, 2)
        self.adjust_sliders["hue"] = hue
        layout.addLayout(grid)

        buttons = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.clicked.connect(self._reset_video)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        note = QLabel(
            "Adjustments apply while something is playing. Leaving everything "
            "at its neutral value keeps the filter switched off entirely."
        )
        note.setObjectName("statusNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------- wiring

    def _preset_chosen(self, row: int):
        index = self.preset.itemData(row)
        if index is None or index < 0:
            return
        preamp, amps = preset_values(index)
        self._loading = True
        self.preamp.setValue(int(round(preamp * 10)))
        for slider, amp in zip(self.bands, amps):
            slider.setValue(int(round(amp * 10)))
        self.eq_enabled.setChecked(True)
        self._loading = False
        self._audio_changed()

    def _audio_changed(self, *_):
        if self._loading:
            return
        enabled = self.eq_enabled.isChecked()
        preamp = self.preamp.value() / 10.0
        amps = [slider.value() / 10.0 for slider in self.bands]
        self.player.set_equalizer(enabled, preamp, amps)
        self.db.set_setting("eq_enabled", "1" if enabled else "0")
        self.db.set_setting("eq_preamp", f"{preamp:.1f}")
        self.db.set_setting("eq_amps", ",".join(f"{amp:.1f}" for amp in amps))

    def _video_changed(self, *_):
        if self._loading:
            return
        values = self._video_values()
        self.player.set_video_adjust(values)
        for key, value in values.items():
            self.db.set_setting(f"adjust_{key}", str(value))

    def _video_values(self) -> dict:
        values = {key: self.adjust_sliders[key].value() / 100.0
                  for key, *_ in VIDEO_ADJUST}
        values["hue"] = self.adjust_sliders["hue"].value()
        return values

    def _reset_audio(self):
        self._loading = True
        self.preset.setCurrentIndex(0)
        self.preamp.setValue(0)
        for slider in self.bands:
            slider.setValue(0)
        self.eq_enabled.setChecked(False)
        self._loading = False
        self._audio_changed()

    def _reset_video(self):
        self._loading = True
        for key, _label, _low, _high, neutral in VIDEO_ADJUST:
            self.adjust_sliders[key].setValue(int(neutral * 100))
        self.adjust_sliders["hue"].setValue(HUE_RANGE[2])
        self._loading = False
        self._video_changed()

    # ------------------------------------------------------------ persist

    def _load(self):
        self.eq_enabled.setChecked(self.db.get_bool("eq_enabled", False))
        self.preamp.setValue(int(float(self.db.get_setting("eq_preamp", "0") or 0) * 10))
        stored = (self.db.get_setting("eq_amps", "") or "").split(",")
        for index, slider in enumerate(self.bands):
            try:
                slider.setValue(int(float(stored[index]) * 10))
            except (IndexError, ValueError):
                slider.setValue(0)

        for key, _label, _low, _high, neutral in VIDEO_ADJUST:
            try:
                value = float(self.db.get_setting(f"adjust_{key}", "") or neutral)
            except ValueError:
                value = neutral
            self.adjust_sliders[key].setValue(int(value * 100))
        try:
            hue = int(float(self.db.get_setting("adjust_hue", "") or HUE_RANGE[2]))
        except ValueError:
            hue = HUE_RANGE[2]
        self.adjust_sliders["hue"].setValue(hue)


def load_saved_effects(db, player):
    """Re-apply stored effects to a freshly built engine, at startup."""
    enabled = db.get_bool("eq_enabled", False)
    try:
        preamp = float(db.get_setting("eq_preamp", "0") or 0)
    except ValueError:
        preamp = 0.0
    amps = []
    for chunk in (db.get_setting("eq_amps", "") or "").split(","):
        try:
            amps.append(float(chunk))
        except ValueError:
            amps.append(0.0)
    if enabled and len(amps) >= 10:
        player.set_equalizer(True, preamp, amps[:10])

    values = {}
    for key, _label, _low, _high, neutral in VIDEO_ADJUST:
        try:
            values[key] = float(db.get_setting(f"adjust_{key}", "") or neutral)
        except ValueError:
            values[key] = neutral
    try:
        values["hue"] = int(float(db.get_setting("adjust_hue", "") or 0))
    except ValueError:
        values["hue"] = 0
    player.set_video_adjust(values)
