"""Master search: one field, the whole catalog, grouped by type.

The per-tab search box narrows the list you are already looking at. This asks
the other question - "where is this, anywhere?" - across TV, Movies and Series
at once, so you do not have to guess which tab a title was filed under.

Each section is a real QListView driven by the catalog's own model and
delegates, so results look exactly like the lists they came from: channel rows
with logos for TV, poster cells for films and shows.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from core.db import fold
from ui import icons
from ui.models import (
    POSTER_H, POSTER_W, ROW_HEIGHT, ROLE_ITEM, CatalogModel, ChannelDelegate,
    PosterDelegate,
)

# Different caps per kind because a 44px channel row and a 238px poster cell
# fill very different amounts of a section.
SECTION_CAPS = {"live": 8, "movie": 18, "series": 18}
MIN_TERM = 2
SECTION_TITLES = (("live", "TV"), ("movie", "MOVIES"), ("series", "SERIES"))

# The same tuple apply_item_filter selects, so CatalogModel and both delegates
# take these rows unchanged.
COLUMNS = ("s.stream_id, s.name, s.icon, s.rating, s.container_extension, "
           "s.num, s.available, s.epg_channel_id, s.added")


def search_catalog(db, playlist_id, term: str, caps=None) -> dict:
    """Matches per kind: {kind: (rows, truncated)}.

    Folded, not merely lowercased: name_folded is built with fold(), so an
    un-folded term silently misses every accented title - lower('café') matches
    nothing at all where fold('café') matches plenty.

    The per-kind LIMIT is load-bearing, not a display choice. name_folded has no
    index a leading-% LIKE can use, so this is a scan either way; the cap lets
    SQLite stop early, which is the difference between 0.2ms and 190ms on a
    single common letter over 93k rows. One row beyond the cap is fetched purely
    to detect truncation - a COUNT(*) would re-scan and cost more than the
    search itself.
    """
    caps = caps or SECTION_CAPS
    needle = fold((term or "").strip())
    if len(needle) < MIN_TERM:
        return {kind: ([], False) for kind, _ in SECTION_TITLES}

    out = {}
    for kind, _ in SECTION_TITLES:
        cap = int(caps.get(kind, 12))
        # Live channels carry a provider-assigned running order; keep it.
        order = "s.num, s.name_folded" if kind == "live" else "s.name_folded"
        rows = db.query(
            f"SELECT {COLUMNS} FROM streams s WHERE s.playlist_id=? AND s.kind=?"
            " AND s.available=1 AND s.name <> ''"
            # dup_rank=1 is the sync-time dedupe: without it one film appears
            # once per category it is filed under.
            " AND s.dup_rank=1 AND s.name_folded LIKE ?"
            f" ORDER BY {order} LIMIT ?",
            (playlist_id, kind, f"%{needle}%", cap + 1),
        )
        rows = [tuple(r) for r in rows]
        out[kind] = (rows[:cap], len(rows) > cap)
    return out


def total_results(results: dict) -> int:
    return sum(len(rows) for rows, _ in results.values())


class ResultSection(QWidget):
    """One kind's matches, rendered by the catalog's own delegate."""

    activated = Signal(str, object)      # kind, row
    seeAllRequested = Signal(str)        # kind

    def __init__(self, kind: str, title: str, images, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName("resultSection")

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.heading = QLabel(title)
        self.heading.setObjectName("sectionHeading")
        self.see_all = QPushButton(f"See all in {title}  →")
        self.see_all.setObjectName("seeAllButton")
        self.see_all.setCursor(Qt.PointingHandCursor)
        self.see_all.setFocusPolicy(Qt.NoFocus)
        self.see_all.clicked.connect(lambda: self.seeAllRequested.emit(self.kind))
        self.see_all.hide()
        header.addWidget(self.heading)
        header.addWidget(self.see_all)
        header.addStretch(1)
        box.addLayout(header)

        self.model = CatalogModel(self)
        self.view = QListView()
        self.view.setObjectName("" if kind == "live" else "posterGrid")
        self.view.setModel(self.model)
        self.view.setUniformItemSizes(True)
        self.view.setFocusPolicy(Qt.NoFocus)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setSelectionMode(QListView.NoSelection)
        if kind == "live":
            self.view.setViewMode(QListView.ListMode)
            self.view.setSpacing(0)
            self.view.setItemDelegate(ChannelDelegate(images, lambda: self.model, self))
        else:
            self.view.setViewMode(QListView.IconMode)
            self.view.setResizeMode(QListView.Adjust)
            self.view.setSpacing(4)
            self.view.setItemDelegate(PosterDelegate(images, lambda: self.model, self))
        self.view.clicked.connect(self._clicked)
        self.view.doubleClicked.connect(self._clicked)
        box.addWidget(self.view)

    def _clicked(self, index):
        row = index.data(ROLE_ITEM)
        if row is not None:
            self.activated.emit(self.kind, row)

    def set_results(self, rows, truncated: bool):
        self.model.set_rows(rows, self.kind, set())
        self.see_all.setVisible(bool(truncated))
        self.setVisible(bool(rows))
        self._resize_to_content(len(rows))

    def _resize_to_content(self, count: int):
        """Size the list to its rows: the page scrolls, the sections do not.

        Nested scroll areas are miserable to use, and the caps keep the content
        small enough that it never needs one.
        """
        if not count:
            return
        if self.kind == "live":
            self.view.setFixedHeight(count * ROW_HEIGHT + 4)
            return
        width = max(self.view.viewport().width(), 600)
        cell = POSTER_W + 14 + self.view.spacing() * 2
        per_row = max(1, width // cell)
        rows = (count + per_row - 1) // per_row
        self.view.setFixedHeight(rows * (POSTER_H + 40 + self.view.spacing() * 2) + 8)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_to_content(self.model.rowCount())


class SearchPage(QWidget):
    """Full-window search across the whole catalog."""

    backRequested = Signal()
    resultActivated = Signal(str, object)     # kind, row
    seeAllRequested = Signal(str, str)        # kind, term
    searchRequested = Signal(str)             # term, after the debounce

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self.images = images
        self.setObjectName("searchPage")
        self.sections = {}
        self._build()
        images.loaded.connect(self._image_arrived)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("searchScroll")
        body = QWidget()
        body.setObjectName("searchBody")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        root = QVBoxLayout(body)
        root.setContentsMargins(28, 22, 28, 28)
        root.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.back_button = QPushButton()
        self.back_button.setObjectName("backButton")
        self.back_button.setIcon(icons.icon("back", 24, "#ffffff"))
        self.back_button.setIconSize(QSize(24, 24))
        self.back_button.setFixedSize(40, 36)
        self.back_button.setCursor(Qt.PointingHandCursor)
        self.back_button.setToolTip("Back to the catalog  (Esc)")
        self.back_button.clicked.connect(self.backRequested)

        self.field = QLineEdit()
        self.field.setObjectName("masterSearch")
        self.field.setPlaceholderText("Search TV, movies and series…")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(lambda _: self.searchRequested.emit(self.term()))
        self.field.returnPressed.connect(lambda: self.searchRequested.emit(self.term()))

        header.addWidget(self.back_button)
        header.addWidget(self.field, 1)
        root.addLayout(header)

        self.status = QLabel("")
        self.status.setObjectName("searchStatus")
        root.addWidget(self.status)

        for kind, title in SECTION_TITLES:
            section = ResultSection(kind, title, self.images)
            section.activated.connect(self.resultActivated)
            section.seeAllRequested.connect(
                lambda k: self.seeAllRequested.emit(k, self.term()))
            section.hide()
            self.sections[kind] = section
            root.addWidget(section)
        root.addStretch(1)

    # ------------------------------------------------------------------ api

    def term(self) -> str:
        return self.field.text().strip()

    def focus_field(self, preset: str | None = None):
        if preset is not None:
            self.field.setText(preset)
        self.field.setFocus(Qt.OtherFocusReason)
        self.field.selectAll()

    def show_results(self, results: dict, term: str):
        for kind, _ in SECTION_TITLES:
            rows, truncated = results.get(kind, ([], False))
            self.sections[kind].set_results(rows, truncated)
        found = total_results(results)
        if len(fold(term)) < MIN_TERM:
            self.status.setText("Type at least two characters.")
        elif not found:
            self.status.setText(f"Nothing matching “{term}”.")
        else:
            self.status.setText("")

    def _image_arrived(self, _url: str):
        for section in self.sections.values():
            if section.isVisible():
                section.view.viewport().update()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.backRequested.emit()
            return
        super().keyPressEvent(event)
