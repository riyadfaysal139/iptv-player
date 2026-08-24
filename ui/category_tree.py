"""Sidebar category tree.

The provider ships a flat list (194 live / 133 VOD / 122 series categories)
where almost every name repeats a prefix. classify.py folds those into
group -> subcategory, which is what this view renders; a flat mode is kept for
users who prefer the provider's original ordering.

Counts are drawn right-aligned in the row, matching the reference design.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QTreeView

from core.classify import UNCATEGORIZED
from ui import icons

ROLE_NODE = Qt.UserRole + 10   # ("all"|"favourites"|...|"group"|"category", payload)
ROLE_COUNT = Qt.UserRole + 11
# Two different meanings, deliberately named apart: ROLE_PINNED is "one of the
# fixed rows at the top of the sidebar", ROLE_ON_HOME is "the user pinned this
# category to the homepage".
ROLE_PINNED = Qt.UserRole + 12
ROLE_ON_HOME = Qt.UserRole + 13

# Only these can go on the homepage; ALL and DOWNLOADS are not categories.
PINNABLE = ("group", "category")

PINNED = [
    ("all", "ALL"),
    ("continue", "CONTINUE WATCHING"),
    ("favourites", "FAVOURITES"),
    ("recent", "RECENTLY ADDED"),
    ("downloads", "DOWNLOADS"),
]


class CategoryDelegate(QStyledItemDelegate):
    @staticmethod
    def pin_rect(row_rect: QRect) -> QRect:
        """Where the homepage pin sits — left of the count, right of the label.

        Static and shared with the click handler, the way the favourite heart
        on channel rows is: a click inside this must toggle the pin rather than
        select the category.
        """
        return QRect(row_rect.right() - 86, row_rect.top(), 24, row_rect.height())

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 30)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        pinned = bool(index.data(ROLE_PINNED))
        on_home = bool(index.data(ROLE_ON_HOME))
        node = index.data(ROLE_NODE) or ("", None)
        is_group = node[0] == "group"
        pinnable = node[0] in PINNABLE

        if selected:
            painter.fillRect(rect, QColor("#131c48"))
        elif hovered:
            painter.fillRect(rect, QColor("#101841"))

        font = QFont(option.font)
        if pinned or is_group:
            font.setBold(True)
            font.setLetterSpacing(QFont.PercentageSpacing, 103)
        painter.setFont(font)

        colour = QColor("#f5d90a") if selected else (
            QColor("#e2e7ff") if (pinned or is_group) else QColor("#aab2da")
        )
        painter.setPen(QPen(colour))

        indent = 30 if not (pinned or is_group) else 14
        # Reserve the pin's column on rows that can carry one, so a long name
        # never runs underneath it.
        reserved = 88 if pinnable else 62
        text_rect = QRect(rect.left() + indent, rect.top(),
                          rect.width() - indent - reserved, rect.height())
        label = index.data(Qt.DisplayRole) or ""
        painter.drawText(
            text_rect, Qt.AlignVCenter | Qt.AlignLeft,
            painter.fontMetrics().elidedText(label, Qt.ElideRight, text_rect.width()),
        )

        # The pin: always shown once it is on the homepage, otherwise only
        # while the row is under the pointer.
        if pinnable and (on_home or hovered):
            colour = "#f5d90a" if on_home else "#5c6493"
            pixmap = icons.pixmap("pin", colour, 14)
            box = self.pin_rect(rect)
            painter.drawPixmap(
                box.left() + (box.width() - 14) // 2,
                box.top() + (box.height() - 14) // 2, pixmap,
            )

        count = index.data(ROLE_COUNT)
        if count is not None:
            painter.setPen(QPen(QColor("#f5d90a") if selected else QColor("#7b84b4")))
            painter.drawText(
                QRect(rect.right() - 58, rect.top(), 50, rect.height()),
                Qt.AlignVCenter | Qt.AlignRight, f"{count:,}",
            )
        painter.restore()


class CategoryTree(QTreeView):
    """Emits (node_type, payload) when the user picks something."""

    nodeSelected = Signal(str, object)
    pinToggled = Signal(str, object, str)      # node_type, payload, title
    menuRequested = Signal(str, object, str, object)   # + the global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("categoryTree")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(10)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(False)
        self.setEditTriggers(QTreeView.NoEditTriggers)
        self.setVerticalScrollMode(QTreeView.ScrollPerPixel)
        self.setSelectionMode(QTreeView.SingleSelection)
        self.setItemDelegate(CategoryDelegate(self))

        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.clicked.connect(self._on_clicked)
        # Hover has to repaint for the pin to appear under the pointer.
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self._flat = False
        self._filter = ""
        self._data = {"groups": [], "totals": {}}
        self._home_pins = set()     # (kind, node_type, payload), for the glyph
        self._kind = "live"
        self._press_pos = None

    # ------------------------------------------------------------ building

    def set_flat(self, flat: bool):
        self._flat = flat
        self.rebuild()

    def set_filter(self, text: str):
        self._filter = (text or "").strip().lower()
        self.rebuild()

    def load(self, groups, totals: dict):
        """groups: list of (group_name, total, [(category_id, sub_name, count)])."""
        self._data = {"groups": groups, "totals": totals}
        self.rebuild()

    def set_home_pins(self, keys, kind: str, rebuild: bool = True):
        """Which rows are on the homepage, for the kind currently listed.

        `rebuild=False` when load() is about to rebuild anyway, so a catalog
        reload does not paint the whole tree three times over.
        """
        self._home_pins = set(keys or ())
        self._kind = kind
        if rebuild:
            self.rebuild()

    def _on_home(self, node_type: str, payload) -> bool:
        return (self._kind, node_type, str(payload)) in self._home_pins

    def rebuild(self):
        self._model.clear()
        root = self._model.invisibleRootItem()
        totals = self._data["totals"]

        for key, label in PINNED:
            count = totals.get(key)
            if self._filter and self._filter not in label.lower():
                continue
            item = QStandardItem(label)
            item.setData((key, None), ROLE_NODE)
            item.setData(count, ROLE_COUNT)
            item.setData(True, ROLE_PINNED)
            item.setEditable(False)
            root.appendRow(item)

        separator = QStandardItem("")
        separator.setData(("spacer", None), ROLE_NODE)
        separator.setSelectable(False)
        separator.setEnabled(False)
        separator.setEditable(False)
        root.appendRow(separator)

        for group_name, group_total, children in self._data["groups"]:
            visible = [
                c for c in children
                if not self._filter
                or self._filter in c[1].lower()
                or self._filter in group_name.lower()
            ]
            if self._filter and not visible:
                continue

            if self._flat:
                for category_id, sub_name, count in visible:
                    item = QStandardItem(sub_name)
                    item.setData(("category", category_id), ROLE_NODE)
                    item.setData(count, ROLE_COUNT)
                    item.setData(self._on_home("category", category_id), ROLE_ON_HOME)
                    item.setEditable(False)
                    root.appendRow(item)
                continue

            group_item = QStandardItem(group_name.upper())
            group_item.setData(("group", group_name), ROLE_NODE)
            group_item.setData(group_total, ROLE_COUNT)
            group_item.setData(self._on_home("group", group_name), ROLE_ON_HOME)
            group_item.setEditable(False)
            for category_id, sub_name, count in visible:
                child = QStandardItem(sub_name)
                child.setData(("category", category_id), ROLE_NODE)
                child.setData(count, ROLE_COUNT)
                child.setData(self._on_home("category", category_id), ROLE_ON_HOME)
                child.setEditable(False)
                group_item.appendRow(child)
            root.appendRow(group_item)
            if self._filter:
                self.expand(group_item.index())

    # ------------------------------------------------------------- events

    def mousePressEvent(self, event):
        # Remember where the press landed. `clicked` carries no position, and
        # asking the global cursor instead would make the hit-test depend on
        # where the real pointer happens to be rather than where the click was.
        self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def _on_clicked(self, index):
        node = index.data(ROLE_NODE)
        if not node:
            return
        node_type, payload = node
        if node_type == "spacer":
            return
        # The pin first: a click on it toggles the homepage rail and must not
        # also select the category, the same rule the favourite heart follows.
        if node_type in PINNABLE and self._press_pos is not None:
            if CategoryDelegate.pin_rect(self.visualRect(index)).contains(
                    self._press_pos):
                self.pinToggled.emit(node_type, payload,
                                     index.data(Qt.DisplayRole) or "")
                return
        if node_type == "group":
            self.setExpanded(index, not self.isExpanded(index))
        self.nodeSelected.emit(node_type, payload)

    def _on_context_menu(self, point):
        index = self.indexAt(point)
        node = index.data(ROLE_NODE) if index.isValid() else None
        if not node or node[0] == "spacer":
            return
        self.menuRequested.emit(node[0], node[1], index.data(Qt.DisplayRole) or "",
                                self.viewport().mapToGlobal(point))

    def select_first(self):
        if self._model.rowCount():
            index = self._model.index(0, 0)
            self.setCurrentIndex(index)
            self._on_clicked(index)


def build_groups(rows) -> list:
    """Turn category rows into ordered (group, total, children) tuples.

    Rows come from the categories table, already carrying group_name/sub_name.
    Groups sort by size so the biggest are reachable first, with Uncategorized
    pinned last so nothing is ever unreachable.
    """
    from core.classify import era_sort_key

    buckets: dict[str, list] = {}
    for row in rows:
        buckets.setdefault(row["group_name"], []).append(
            (row["category_id"], row["sub_name"], row["item_count"])
        )

    groups = []
    for name, children in buckets.items():
        children.sort(key=lambda c: era_sort_key(c[1]))
        groups.append((name, sum(c[2] for c in children), children))

    groups.sort(key=lambda g: (g[0] == UNCATEGORIZED, -g[1], g[0].lower()))
    return groups
