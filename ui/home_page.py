"""The homepage: one wall covering channels, films and shows together.

Every other view in the app shows a single kind, because the catalog list pins
s.kind to whichever tab is open. This is the one place they mix - what you were
part-way through, what you marked a favourite, what is new - in horizontal rails
of posters.

The rails are real QListViews driven by the catalog's own model and poster
delegate, so nothing new is painted here; only the flow differs, scrolling
sideways instead of wrapping.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListView, QPushButton,
    QScrollArea, QStyle, QStyleOptionViewItem, QVBoxLayout, QWidget,
)

from ui.gridnav import move_cursor
from ui.models import (
    POSTER_H, POSTER_W, ROLE_ITEM, ROLE_KIND, CatalogModel, PosterDelegate,
)

ROW_CAP = 20            # posters per rail
GENRE_ROWS = 5          # how many of the provider's genres get a rail
MIN_RAIL = 6            # below this it is not a row, it is a gap
MAX_RAILS = 11          # a wall you scroll, not one you get lost in
RECENT_DAYS = 30

# The keyboard cursor's poster is drawn this much larger, into a halo of empty
# space every cell reserves. GROW_X/GROW_Y are sized so the scaled paint still
# lands inside the cell's own rectangle: anything spilling past it would be
# overdrawn by the next poster and clipped away by a partial repaint.
FOCUS_SCALE = 1.18
GROW_X, GROW_Y = 14, 22
CURSOR_COLOUR = "#f5d90a"
PAGE_RAILS = 3          # how far PageUp/PageDown jump
FAR_END = 10 ** 6       # Home/End, expressed as a move the clamp absorbs

# The same tuple apply_item_filter selects, so CatalogModel and PosterDelegate
# take these rows unchanged.
COLUMNS = ("s.stream_id, s.name, s.icon, s.rating, s.container_extension, "
           "s.num, s.available, s.epg_channel_id, s.added")


def _rows(db, sql, params):
    return [tuple(r) for r in db.query(sql, params)]


# --------------------------------------------------------------------------
# categories the user has pinned
# --------------------------------------------------------------------------


def pin_rail(db, playlist_id, kind: str, node_type: str, payload: str, title: str):
    """Pin a sidebar group or category to the homepage. Idempotent."""
    db.execute(
        "INSERT INTO home_rails(playlist_id, kind, node_type, payload, title,"
        " pinned_at) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(playlist_id, kind, node_type, payload) DO UPDATE SET"
        " title=excluded.title",
        (playlist_id, kind, node_type, str(payload), title, int(time.time())),
    )


def unpin_rail(db, playlist_id, kind: str, node_type: str, payload: str):
    db.execute(
        "DELETE FROM home_rails WHERE playlist_id=? AND kind=? AND node_type=?"
        " AND payload=?",
        (playlist_id, kind, node_type, str(payload)),
    )


def pinned_rails(db, playlist_id):
    """Everything pinned, oldest first: [(kind, node_type, payload, title)]."""
    return [(r["kind"], r["node_type"], r["payload"], r["title"])
            for r in db.query(
                "SELECT kind, node_type, payload, title FROM home_rails"
                " WHERE playlist_id=? ORDER BY pinned_at, rowid",
                (playlist_id,),
            )]


def pin_keys(db, playlist_id) -> set:
    """What the sidebar needs to know to light its pins."""
    return {(kind, node_type, payload)
            for kind, node_type, payload, _title in pinned_rails(db, playlist_id)}


def is_pinned(db, playlist_id, kind: str, node_type: str, payload: str) -> bool:
    return (kind, node_type, str(payload)) in pin_keys(db, playlist_id)


def _personal(db, playlist_id, table, order_column, cap):
    """Continue Watching and Favourites: small tables, joined the fast way.

    Driven from the small table and CROSS JOINed into streams, which is NOT
    stylistic. Written as a plain JOIN, SQLite reorders it to drive from the
    92k-row streams table and probe the other side with a bloom filter: 57ms
    warm, a full second cold. Selecting the handful of watched ids first and
    seeking streams by primary key measures at 0.0ms. CROSS JOIN is how SQLite
    is told to keep that order.

    History is keyed per *episode*, so it is grouped first - otherwise a show
    with eight watched episodes fills the rail eight times over.
    """
    if table == "history":
        inner = (
            "SELECT kind, stream_id, MAX(watched_at) AS sort_key FROM history"
            " WHERE playlist_id=? GROUP BY kind, stream_id"
            " ORDER BY sort_key DESC LIMIT ?"
        )
    else:
        inner = (
            "SELECT kind, stream_id, added_at AS sort_key FROM favourites"
            " WHERE playlist_id=? ORDER BY sort_key DESC LIMIT ?"
        )
    return _rows(
        db,
        f"SELECT {COLUMNS}, s.kind FROM ({inner}) t CROSS JOIN streams s"
        " ON s.playlist_id=? AND s.kind=t.kind AND s.stream_id=t.stream_id"
        " WHERE s.available=1 AND s.name <> ''"
        f" ORDER BY t.sort_key DESC LIMIT {int(cap)}",
        # A few extra inside, because available=0 and nameless rows are dropped
        # after the cap and would otherwise short the rail.
        (playlist_id, int(cap) * 2 + 10, playlist_id),
    )


def _newest(db, playlist_id, kind, cap):
    """What the provider added lately. idx_streams_added serves this directly."""
    return _rows(
        db,
        f"SELECT {COLUMNS} FROM streams s WHERE s.playlist_id=? AND s.kind=?"
        " AND s.available=1 AND s.name <> '' AND s.dup_rank=1 AND s.added > ?"
        " ORDER BY s.added DESC LIMIT ?",
        (playlist_id, kind, int(time.time()) - RECENT_DAYS * 86400, int(cap)),
    )


def _by_group(db, playlist_id, kind, group_name, cap, order):
    return _rows(
        db,
        f"SELECT {COLUMNS} FROM streams s WHERE s.playlist_id=? AND s.kind=?"
        " AND s.category_id IN (SELECT category_id FROM categories"
        "  WHERE playlist_id=? AND kind=? AND group_name=?)"
        " AND s.available=1 AND s.name <> '' AND s.dup_rank=1"
        f" ORDER BY {order} LIMIT ?",
        (playlist_id, kind, playlist_id, kind, group_name, int(cap)),
    )


def _by_category(db, playlist_id, kind, category_id, cap):
    return _rows(
        db,
        f"SELECT {COLUMNS} FROM streams s WHERE s.playlist_id=? AND s.kind=?"
        " AND s.category_id=? AND s.available=1 AND s.name <> '' AND s.dup_rank=1"
        " ORDER BY s.added DESC LIMIT ?",
        (playlist_id, kind, category_id, int(cap)),
    )


def _biggest_group(db, playlist_id, kind):
    row = db.one(
        "SELECT group_name, SUM(item_count) AS n FROM categories"
        " WHERE playlist_id=? AND kind=? GROUP BY group_name"
        " ORDER BY n DESC LIMIT 1",
        (playlist_id, kind),
    )
    return row["group_name"] if row else ""


def _genres(db, playlist_id, limit):
    """The provider's own genres, biggest first, as (kind, category_id, name).

    Ranked across films and shows together rather than a fixed quota each, so
    the wall follows what the provider actually has: this catalog's film genres
    run to 1468 titles while its show genres bottom out at one, and a rail of
    one is a gap with a heading on it.

    Read from the catalog rather than hard-coded, so a provider shipping no
    genre taxonomy gets no genre rails instead of a screenful of empty ones. One
    category per genre, which is what lets "See all" hand straight over to the
    sidebar's own category node.
    """
    return [(r["kind"], r["category_id"], r["sub_name"]) for r in db.query(
        "SELECT kind, category_id, sub_name, item_count FROM categories"
        " WHERE playlist_id=? AND kind IN ('movie','series')"
        " AND group_name='Genres' AND sub_name <> '' AND item_count >= ?"
        " ORDER BY item_count DESC LIMIT ?",
        (playlist_id, MIN_RAIL, int(limit)),
    )]


def home_sections(db, playlist_id, cap: int = ROW_CAP, genres: int = GENRE_ROWS):
    """The wall, in order: [(key, title, rows, kinds, target)].

    Empty rails are dropped rather than shown blank. `kinds` is a per-row list,
    because Continue Watching and Favourites can hold a film and a show side by
    side and clicking one has to know which it is.

    `target` is the sidebar node "See all" hands over to - (kind, node_type,
    payload) - or None for the two personal rails, whose sidebar equivalents are
    per-kind while the rail is not.
    """
    out = []

    for key, table, title in (("continue", "history", "CONTINUE WATCHING"),
                              ("favourites", "favourites", "FAVOURITES")):
        rows = _personal(db, playlist_id, table, "sort_key", cap)
        if rows:
            # The trailing column is the kind; the model wants the tuple without
            # it, so it is split off here rather than widening the row shape.
            out.append((key, title, [r[:-1] for r in rows],
                        [r[-1] for r in rows], None))

    # Your choices, after your history and before anything the app guessed.
    pinned_targets = set()
    for kind, node_type, payload, title in pinned_rails(db, playlist_id):
        pinned_targets.add((kind, node_type, payload))
        if node_type == "group":
            rows = _by_group(db, playlist_id, kind, payload, cap, "s.added DESC")
        else:
            rows = _by_category(db, playlist_id, kind, payload, cap)
        if rows:
            # No MIN_RAIL here: a small rail you asked for is not a gap.
            out.append((f"pin_{kind}_{node_type}_{payload}", title.upper(), rows,
                        [kind] * len(rows), (kind, node_type, payload)))

    for key, kind, title in (("new_movie", "movie", "NEW MOVIES"),
                             ("new_series", "series", "NEW SERIES"),
                             ("new_live", "live", "NEW CHANNELS")):
        rows = _newest(db, playlist_id, kind, cap)
        if len(rows) >= MIN_RAIL:
            out.append((key, title, rows, [kind] * len(rows),
                        (kind, "recent", None)))

    group = _biggest_group(db, playlist_id, "live")
    if group:
        rows = _by_group(db, playlist_id, "live", group, cap, "s.num, s.name_folded")
        if len(rows) >= MIN_RAIL:
            out.append(("live_group", f"LIVE · {group.upper()}", rows,
                        ["live"] * len(rows), ("live", "group", group)))

    for kind, category_id, name in _genres(db, playlist_id, genres):
        if (kind, "category", category_id) in pinned_targets:
            continue        # already on the wall by choice; twice is confusing
        rows = _by_category(db, playlist_id, kind, category_id, cap)
        if len(rows) >= MIN_RAIL:
            # Films get the bare genre name; shows are marked, because the two
            # taxonomies overlap (both have a Documentary) and two rails with
            # the same heading are just confusing.
            title = name.upper() if kind == "movie" else f"SHOWS · {name.upper()}"
            out.append((f"genre_{kind}_{category_id}", title, rows,
                        [kind] * len(rows), (kind, "category", category_id)))

    # Your history and your pins always stay: the cap exists to trim what the
    # app guessed, never what you asked for.
    keep = [s for s in out
            if s[0] in ("continue", "favourites") or s[0].startswith("pin_")]
    generated = [s for s in out if s not in keep]
    return keep + generated[:max(0, MAX_RAILS - len(keep))]


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------


class RailDelegate(PosterDelegate):
    """The catalog's poster, with the cursor's own copy drawn bigger.

    A subclass rather than an edit to PosterDelegate, because the same delegate
    draws the catalog grid and the search page and neither wants a halo.

    The growth is a painter scale about the cell's centre, so the poster, its
    rating badge and its title all zoom together and none of the parent's
    geometry is repeated here. The cursor is the view's selection, which is what
    lets the parent's own selected colours come through unchanged.
    """

    def sizeHint(self, option, index) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + 2 * GROW_X, hint.height() + 2 * GROW_Y)

    def paint(self, painter, option, index):
        inner = QStyleOptionViewItem(option)
        inner.rect = option.rect.adjusted(GROW_X, GROW_Y, -GROW_X, -GROW_Y)
        if not option.state & QStyle.State_Selected:
            # Exactly the cell it would have had before the halo existed.
            super().paint(painter, inner, index)
            return

        centre = option.rect.center()
        painter.save()
        painter.translate(centre)
        painter.scale(FOCUS_SCALE, FOCUS_SCALE)
        painter.translate(-centre)
        super().paint(painter, inner, index)
        # Drawn inside the scaled transform, so it frames the poster the parent
        # just drew whatever the scale is.
        painter.setPen(QPen(QColor(CURSOR_COLOUR), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.art_rect(inner.rect).adjusted(0, 0, -1, -1))
        painter.restore()


class HomeRail(QWidget):
    """One horizontal row of posters, scrolling sideways."""

    activated = Signal(str, object)      # kind, row
    seeAllRequested = Signal(object)     # (kind, node_type, payload)
    unpinRequested = Signal(object)      # (kind, node_type, payload)
    cursorRequested = Signal(str, int)   # rail key, column

    def __init__(self, key: str, title: str, images, parent=None):
        super().__init__(parent)
        self.key = key
        self.target = None
        self.setObjectName("homeRail")

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.heading = QLabel(title)
        self.heading.setObjectName("railHeading")
        self.see_all = QPushButton("See all  →")
        self.see_all.setObjectName("seeAllButton")
        self.see_all.setCursor(Qt.PointingHandCursor)
        self.see_all.setFocusPolicy(Qt.NoFocus)
        self.see_all.clicked.connect(self._see_all_clicked)
        self.unpin = QPushButton("✕ Remove")
        self.unpin.setObjectName("unpinButton")
        self.unpin.setCursor(Qt.PointingHandCursor)
        self.unpin.setFocusPolicy(Qt.NoFocus)
        self.unpin.setToolTip("Take this off the homepage")
        self.unpin.clicked.connect(self._unpin_clicked)
        self.unpin.hide()
        header.addWidget(self.heading)
        header.addWidget(self.see_all)
        header.addWidget(self.unpin)
        header.addStretch(1)
        box.addLayout(header)

        self.model = CatalogModel(self)
        self.view = QListView()
        self.view.setObjectName("posterGrid")
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.IconMode)
        # The rail: one line that scrolls sideways rather than wrapping.
        self.view.setFlow(QListView.LeftToRight)
        self.view.setWrapping(False)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(4)
        # NoFocus: the keyboard belongs to the page, which drives every rail's
        # cursor. The cursor itself *is* the selection, so the poster delegate's
        # existing selected painting is what marks it.
        self.view.setFocusPolicy(Qt.NoFocus)
        self.view.setSelectionMode(QListView.SingleSelection)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollMode(QListView.ScrollPerPixel)
        self.view.setFixedHeight(POSTER_H + 40 + 2 * GROW_Y
                                 + self.view.spacing() * 2 + 14)
        self.view.setItemDelegate(RailDelegate(images, lambda: self.model, self))
        # Double-click, not single: a rail is dragged sideways, and a drag that
        # ends in a click would otherwise start playing something.
        self.view.doubleClicked.connect(self._activated)
        self.view.pressed.connect(self._pressed)
        box.addWidget(self.view)

    def _activated(self, index):
        row = index.data(ROLE_ITEM)
        if row is not None:
            self.activated.emit(index.data(ROLE_KIND) or "movie", row)

    def _pressed(self, index):
        """A click puts the cursor where you clicked, and the keyboard here."""
        if index.isValid():
            self.cursorRequested.emit(self.key, index.row())

    def set_cursor(self, column):
        """Put the cursor on `column`, or None to take it off this rail."""
        if column is None or not 0 <= column < self.model.rowCount():
            self.view.clearSelection()
            self.view.setCurrentIndex(QModelIndex())
            return None
        index = self.model.index(column, 0)
        self.view.setCurrentIndex(index)
        return index

    def activate(self, column: int):
        if 0 <= column < self.model.rowCount():
            self._activated(self.model.index(column, 0))

    def _see_all_clicked(self):
        if self.target is not None:
            self.seeAllRequested.emit(self.target)

    def _unpin_clicked(self):
        if self.target is not None:
            self.unpinRequested.emit(self.target)

    def set_target(self, target, pinned: bool = False):
        """Where "See all" goes, or None to hide the link.

        The two personal rails have none: their sidebar equivalents are per-kind
        and the rail is mixed, so there is no one node to send you to. `pinned`
        adds the way back out, because unpinning from the thing you are looking
        at is more obvious than hunting for it in the sidebar again.
        """
        self.target = target
        self.see_all.setVisible(target is not None)
        self.unpin.setVisible(bool(pinned) and target is not None)

    def set_rows(self, rows, kinds):
        self.model.set_rows(rows, kinds[0] if kinds else "movie", set(), kinds)
        self.setVisible(bool(rows))

    def rows(self) -> int:
        return self.model.rowCount()


class HomePage(QWidget):
    """The wall of rails, rebuilt each time it is opened."""

    itemActivated = Signal(str, object)      # kind, row
    seeAllRequested = Signal(object)         # (kind, node_type, payload)
    unpinRequested = Signal(object)          # (kind, node_type, payload)

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self.images = images
        self.setObjectName("homePage")
        self.rails = {}
        self._order = []
        # (rail key, column), plus the rail's position for the case where the
        # key itself is gone by the next rebuild.
        self._cursor = None
        self._cursor_row = 0
        # The page owns the arrows; the scroll area must not take them, or
        # clicking the wall would leave them scrolling it instead.
        self.setFocusPolicy(Qt.StrongFocus)
        self._build()
        images.loaded.connect(self._image_arrived)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("homeScroll")
        scroll.setFocusPolicy(Qt.NoFocus)
        body = QWidget()
        body.setObjectName("homeBody")
        body.setFocusPolicy(Qt.NoFocus)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.scroll = scroll

        self.root = QVBoxLayout(body)
        self.root.setContentsMargins(24, 20, 24, 28)
        self.root.setSpacing(18)

        self.empty = QLabel("")
        self.empty.setObjectName("homeEmpty")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.hide()
        self.root.addWidget(self.empty)
        self.root.addStretch(1)

    def show_sections(self, sections):
        """Replace the wall. Rails are reused across refreshes by key."""
        wanted = [key for key, _, _, _, _ in sections]
        for key in list(self.rails):
            if key not in wanted:
                rail = self.rails.pop(key)
                self.root.removeWidget(rail)
                rail.deleteLater()

        for position, (key, title, rows, kinds, target) in enumerate(sections):
            rail = self.rails.get(key)
            if rail is None:
                rail = HomeRail(key, title, self.images)
                rail.activated.connect(self.itemActivated)
                rail.seeAllRequested.connect(self.seeAllRequested)
                rail.unpinRequested.connect(self.unpinRequested)
                rail.cursorRequested.connect(self._cursor_pressed)
                self.rails[key] = rail
            rail.heading.setText(title)
            rail.set_target(target, pinned=key.startswith("pin_"))
            rail.set_rows(rows, kinds)
            # Insert before the trailing stretch, in the order given.
            self.root.insertWidget(position + 1, rail)

        self._order = wanted
        self.empty.setVisible(not sections)
        if not sections:
            self.empty.setText(
                "Nothing to show yet.\n\nOnce the catalog has been fetched, what "
                "you watch and what you favourite turn up here."
            )
        # Setting a model's rows drops its selection, and the wall is rebuilt on
        # every visit, so the cursor has to be put back rather than reset - or
        # opening the homepage would throw you to the top-left each time.
        self._apply_cursor()

    def rail_keys(self):
        return list(self._order)

    def _image_arrived(self, _url):
        for rail in self.rails.values():
            if rail.isVisible():
                rail.view.viewport().update()

    # ----------------------------------------------------------- the cursor

    def _keys(self):
        return [key for key in self._order if key in self.rails]

    def _lengths(self, keys):
        return [self.rails[key].rows() for key in keys]

    def _position(self, keys):
        """The cursor as (rail index, column), for move_cursor to work on."""
        if self._cursor is None:
            return None
        key, column = self._cursor
        return (keys.index(key) if key in keys else self._cursor_row), column

    def _move(self, d_rail: int = 0, d_column: int = 0):
        keys = self._keys()
        target = move_cursor(self._lengths(keys), self._position(keys),
                             d_rail, d_column)
        if target is None:
            return
        self._cursor = (keys[target[0]], target[1])
        self._apply_cursor(scroll=True)

    def _apply_cursor(self, scroll: bool = False):
        keys = self._keys()
        target = move_cursor(self._lengths(keys), self._position(keys))
        if target is None:
            self._cursor = None
            for rail in self.rails.values():
                rail.set_cursor(None)
            return
        rail_index, column = target
        self._cursor = (keys[rail_index], column)
        self._cursor_row = rail_index
        for key, rail in self.rails.items():
            # Exactly one rail may hold a selection, or two posters look focused
            # at once and neither of them is where the arrows will act.
            rail.set_cursor(column if key == self._cursor[0] else None)
        if scroll:
            self.ensure_visible()

    def ensure_visible(self):
        """Follow the cursor sideways along its rail, and down the wall."""
        if self._cursor is None:
            return
        key, column = self._cursor
        rail = self.rails.get(key)
        if rail is None or not 0 <= column < rail.rows():
            return
        rail.view.scrollTo(rail.model.index(column, 0),
                           QAbstractItemView.EnsureVisible)
        self.scroll.ensureWidgetVisible(rail, 0, 0)

    def _cursor_pressed(self, key: str, column: int):
        """A click on a poster: the cursor goes there, and so does the keyboard.

        The rails take no focus of their own, so without this a click would
        leave the arrows wherever they were - usually the category tree.
        """
        self.setFocus(Qt.MouseFocusReason)
        self._cursor = (key, int(column))
        self._apply_cursor()

    def _activate_cursor(self):
        if self._cursor is None:
            return
        key, column = self._cursor
        rail = self.rails.get(key)
        if rail is not None:
            rail.activate(column)

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        """The wall's own keyboard.

        Space is deliberately absent: it is the global play/pause and the
        up-next card's play, and taking it here would break both.
        """
        moves = {
            Qt.Key_Left: (0, -1), Qt.Key_Right: (0, 1),
            Qt.Key_Up: (-1, 0), Qt.Key_Down: (1, 0),
            Qt.Key_PageUp: (-PAGE_RAILS, 0), Qt.Key_PageDown: (PAGE_RAILS, 0),
            Qt.Key_Home: (0, -FAR_END), Qt.Key_End: (0, FAR_END),
        }
        key = event.key()
        if key in moves:
            self._move(*moves[key])
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._activate_cursor()
            event.accept()
            return
        super().keyPressEvent(event)
