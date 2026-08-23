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

ROLE_NODE = Qt.UserRole + 10   # ("all"|"favourites"|...|"group"|"category", payload)
ROLE_COUNT = Qt.UserRole + 11
ROLE_PINNED = Qt.UserRole + 12

PINNED = [
    ("all", "ALL"),
    ("continue", "CONTINUE WATCHING"),
    ("favourites", "FAVOURITES"),
    ("recent", "RECENTLY ADDED"),
    ("downloads", "DOWNLOADS"),
]


class CategoryDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), 30)

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        pinned = bool(index.data(ROLE_PINNED))
        node = index.data(ROLE_NODE) or ("", None)
        is_group = node[0] == "group"

        if selected:
            painter.fillRect(rect, QColor("#131c48"))
        elif option.state & QStyle.State_MouseOver:
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
        text_rect = QRect(rect.left() + indent, rect.top(), rect.width() - indent - 62,
                          rect.height())
        label = index.data(Qt.DisplayRole) or ""
        painter.drawText(
            text_rect, Qt.AlignVCenter | Qt.AlignLeft,
            painter.fontMetrics().elidedText(label, Qt.ElideRight, text_rect.width()),
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
        self._flat = False
        self._filter = ""
        self._data = {"groups": [], "totals": {}}

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
                    item.setEditable(False)
                    root.appendRow(item)
                continue

            group_item = QStandardItem(group_name.upper())
            group_item.setData(("group", group_name), ROLE_NODE)
            group_item.setData(group_total, ROLE_COUNT)
            group_item.setEditable(False)
            for category_id, sub_name, count in visible:
                child = QStandardItem(sub_name)
                child.setData(("category", category_id), ROLE_NODE)
                child.setData(count, ROLE_COUNT)
                child.setEditable(False)
                group_item.appendRow(child)
            root.appendRow(group_item)
            if self._filter:
                self.expand(group_item.index())

    # ------------------------------------------------------------- events

    def _on_clicked(self, index):
        node = index.data(ROLE_NODE)
        if not node:
            return
        kind, payload = node
        if kind == "spacer":
            return
        if kind == "group":
            self.setExpanded(index, not self.isExpanded(index))
        self.nodeSelected.emit(kind, payload)

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
