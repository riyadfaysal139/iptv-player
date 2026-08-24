"""Where an arrow key lands, for anything laid out as rows of varying length.

The homepage's wall of rails and a show's wrapping grid of episode cards are the
same shape as far as the keyboard is concerned: a list of rows, each with its own
number of items. One mover serves both rather than each page growing its own
off-by-one.
"""

from __future__ import annotations


def move_cursor(lengths, cursor, d_rail: int = 0, d_column: int = 0):
    """Where an arrow key lands on rows holding `lengths` items each.

    Clamps at every edge rather than wrapping - the same choice next_episode()
    makes, and for the same reason: running off the end of a row should stop,
    not silently take you somewhere else.

    Empty rows are skipped, and a cursor sitting on one (its category was
    unpinned, or the provider dropped it) is snapped to the nearest row that
    still has items. Returns (row, column), or None when there is nothing.
    """
    live = [i for i, n in enumerate(lengths) if n > 0]
    if not live:
        return None
    rail, column = (live[0], 0) if cursor is None else cursor
    rail = max(0, min(len(lengths) - 1, int(rail)))
    if rail not in live:
        rail = min(live, key=lambda i: (abs(i - rail), i))
    if d_rail:
        at = live.index(rail)
        rail = live[max(0, min(len(live) - 1, at + d_rail))]
    column = max(0, min(lengths[rail] - 1, int(column) + d_column))
    return rail, column


def rows_from_geometry(rectangles) -> list:
    """How many items sit on each row of a wrapping grid, top row first.

    Read from the widgets' real geometry rather than computed from a column
    count: the grid rewraps whenever the pane changes width, and with the video
    docked beside a show there may be two cards to a row where there were five.

    Rectangles are taken in layout order, and a new row starts wherever the top
    edge changes - FlowLayout gives every item on a line the same y, whatever
    their heights.
    """
    rows = []
    top = None
    for rect in rectangles:
        if top is None or rect.y() != top:
            rows.append(0)
            top = rect.y()
        rows[-1] += 1
    return rows
