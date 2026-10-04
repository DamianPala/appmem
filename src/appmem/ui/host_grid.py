"""The one host-header mechanism, shared by both platforms.

A row is `label | left | │ | items`. The left part is a gauge and a value in
slots whose width depends only on the terminal width and this machine's
totals. The right part is a list of `Item`s laid out left to right: columns 1
and 2 are label + value pairs with the number right-aligned in a field sized
for the worst case, column 3 is detail text split into `·` separated parts.
Every position and every fit decision is derived from the terminal width, the
totals (constant for a run) and the worst-case widths declared by the items,
never from the sampled values, so nothing moves or appears when a number
changes. Items yield from the right, and the omission marker follows the last
item that stayed.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from rich.text import Text

from appmem.fmt import format_rate, size, size_in_unit, total_amount, unit_of

_GAUGE_STEPS = ((80, 10), (60, 6))
_FLEX_MIN = 105
# From 105 columns the gauge is the largest of these that leaves every row complete.
_FLEX_GAUGES = (20, 16, 12)
_LABEL_9_MIN = 80
_MARKER_CELLS = 2
_SEPARATOR_CELLS = 3
_UNBOUNDED = 10_000
_KIB = 1024

RATE_FIELD = 10  # `1023 KiB/s`: rates are unbounded, so their field is the worst case
TOTAL_FIELD = 8  # `99.9 GiB`, `1023 MiB`; 100 GiB and more drop the decimal


@dataclass(frozen=True)
class ValueFields:
    """The cells of the `used/total UNIT word` value beside a gauge. Every row
    uses the same widths, so the slash, the unit and the word start at one cell
    on every row: `used` is right-aligned, `total`, `unit` and `word` are
    left-aligned and padded."""

    number: int = 0  # widest total number (used never needs more)
    unit: int = 0
    word: int = 0

    @property
    def width(self) -> int:
        if not self.number:
            return 0
        return self.number + 1 + self.number + 1 + self.unit + 1 + self.word


@dataclass(frozen=True)
class Slots:
    """Widths that follow from this machine's totals; constant for a run."""

    label1: int  # longest label in column 1
    label2: int  # longest label in column 2
    value: int  # left value: the `fields` width, or a special form if that is wider
    amount: int  # widest size() of anything bounded by the machine's totals
    pressure_left: int  # the pressure row's short word and its amount, from 80 columns
    fields: ValueFields = ValueFields()


@dataclass(frozen=True)
class Layout:
    """Everything the grid derives from the terminal width and the slots."""

    width: int
    label: int
    gauge: int
    gap: int
    wide: bool
    slots: Slots
    left: int

    @property
    def column_1(self) -> int:
        return self.slots.label1 + 1 + RATE_FIELD

    @property
    def column_2(self) -> int:
        return self.slots.label2 + 1 + RATE_FIELD

    @property
    def column_2_start(self) -> int:
        return self.column_1 + self.gap

    @property
    def column_3_start(self) -> int:
        return self.column_2_start + self.column_2 + self.gap


def _make(width: int, slots: Slots, gauge: int, *, wide: bool) -> Layout:
    base = gauge + 1 + slots.value if gauge else slots.value
    left = max(base, slots.pressure_left) if width >= _LABEL_9_MIN else base
    label = 9 if width >= _LABEL_9_MIN else 10
    return Layout(width, label, gauge, 4 if wide else 2, wide, slots, left)


def _natural_width(build: Callable[[Layout], Sequence[Text]], lay: Layout) -> int:
    """Width of the widest row when nothing yields; `grid_row` pads the last item
    to its worst-case `need` at this width, so the sampled values never matter."""
    return max(row.cell_len for row in build(replace(lay, width=_UNBOUNDED)))


def choose(width: int, slots: Slots, build: Callable[[Layout], Sequence[Text]]) -> Layout:
    """The layout for `width`: from 105 columns the largest gauge, below it the
    step for the width; wide words when the rows are complete in that form, else
    short words. `build` renders the full rows for a candidate layout. Width and
    totals decide, never the sampled values."""
    gauges = (
        _FLEX_GAUGES
        if width >= _FLEX_MIN
        else (next((cells for minimum, cells in _GAUGE_STEPS if width >= minimum), 0),)
    )
    for gauge in gauges:
        for wide in (True, False):
            lay = _make(width, slots, gauge, wide=wide)
            if _natural_width(build, lay) <= width:
                return lay
    return _make(width, slots, gauges[-1], wide=False)


def field_width(bound: int) -> int:
    """The widest `size()` of any value up to `bound`."""
    probes = [bound, *(limit for limit in (1023, 1023 * _KIB, 1023 * _KIB**2) if limit <= bound)]
    return max(len(size(probe)) for probe in probes)


def pair_width(total: int, word: str) -> int:
    """Width of `used/total unit word` for a machine total."""
    unit = unit_of(total)
    number = size_in_unit(total, unit)
    return len(value_text(total, total, word, unit, number))


def value_fields(pairs: Sequence[tuple[int, str]]) -> tuple[ValueFields, int]:
    """The field widths for the `(total, word)` pairs of the gauge rows, and the
    width of the value they make. Aligned cells can be wider than the old
    right-aligned strings (a 4-digit total on one row and a shorter word on
    that row, the words differing in length); the left part must not grow, so
    such a machine keeps the old composition: empty fields, the old width."""
    numbers, units, words, legacy = [0], [0], [0], 0
    for total, word in pairs:
        unit = unit_of(total)
        numbers.append(len(size_in_unit(total, unit)))
        units.append(len(unit))
        words.append(len(word))
        legacy = max(legacy, pair_width(total, word))
    fields = ValueFields(max(numbers), max(units), max(words))
    return (fields, fields.width) if fields.width <= legacy else (ValueFields(), legacy)


def field_value(used: str, total: int, word: str, fields: ValueFields) -> str:
    """`used/total UNIT word` in the fixed cells of `fields`; `used` is the
    figure in the pair's own unit, or a placeholder for an unknown one. Empty
    fields give the old form, right-aligned as one string by the caller."""
    unit = unit_of(total)
    total_number = size_in_unit(total, unit)
    if not fields.number:
        return f"{used:>{len(total_number)}}/{total_number} {unit} {word}"
    text = (
        f"{used:>{fields.number}}/{total_number:<{fields.number}} "
        f"{unit:<{fields.unit}} {word:<{fields.word}}"
    )
    # A zswap pool over its limit can carry one digit more than its total: it
    # takes the word's padding, as the old composition did, so the separator holds.
    return text if len(text) <= fields.width else text.rstrip().rjust(fields.width)


def unit_field(amount_text: str, word: str, fields: ValueFields) -> str:
    """A value with one number (`1.7 GiB` + `RAM`) in the same cells: the number
    at the total's cell, the unit in the unit column, the word in the word column."""
    number, _, unit = amount_text.rpartition(" ")
    if not number or not fields.number:
        return f"{amount_text} {word}"
    if len(number) <= fields.number:
        lead = " " * (fields.number + 1) + number.ljust(fields.number)
    else:  # wider than the total's cell: end at the unit column instead
        lead = number.rjust(2 * fields.number + 1)
    return f"{lead} {unit:<{fields.unit}} {word:<{fields.word}}"


def field_text(value: str, style: str = "") -> Text:
    """A field as text. The style covers the figures only, not the padding."""
    body = value.strip()
    lead = len(value) - len(value.lstrip())
    pieces: list[str | tuple[str, str]] = [" " * lead, (body, style) if style else body]
    pieces.append(" " * (len(value) - lead - len(body)))
    return Text.assemble(*pieces)


@dataclass(frozen=True)
class Item:
    """A piece of the right part. `need` is the worst-case width of `text`;
    fit decisions use it alone. `after` fills the space before the next item."""

    text: Text
    need: int
    after: Text


def spaces(count: int) -> Text:
    return Text(" " * count)


def dot(ascii_bars: bool) -> Text:
    return Text(" / " if ascii_bars else " · ", style="dim")


def unknown(ascii_bars: bool) -> str:
    return "?" if ascii_bars else "—"


def amount(value: int | None, *, ascii_bars: bool) -> str:
    return unknown(ascii_bars) if value is None else size(value)


def rate(value: int | None, *, ascii_bars: bool) -> str:
    return (unknown(ascii_bars) if value is None else format_rate(value)).rjust(RATE_FIELD)


def compact_total(value: int | None, *, ascii_bars: bool) -> str:
    """A cumulative total in `TOTAL_FIELD` cells: 100 GiB and more drop the decimal."""
    if value is None:
        return unknown(ascii_bars).rjust(TOTAL_FIELD)
    text = total_amount(value)
    if len(text) > TOTAL_FIELD:
        number, _, unit = text.partition(" ")
        text = f"{float(number):.0f} {unit}"
    return text.rjust(TOTAL_FIELD)


def pair(label: str, value: str, side: int, lay: Layout) -> Item:
    """A column 1 (`side` 1) or column 2 label + value pair with a fixed width."""
    label_width = lay.slots.label1 if side == 1 else lay.slots.label2
    column = lay.column_1 if side == 1 else lay.column_2
    text = f"{label:<{label_width}} {value:>{column - label_width - 1}}"
    return Item(Text(text), column, spaces(lay.gap))


def span(text: Text, need: int, lay: Layout) -> Item:
    """Text that covers columns 1 and 2; column 3 still starts where it does elsewhere."""
    return Item(text, need, spaces(lay.column_3_start - need))


def parts(texts: Sequence[tuple[Text, int]], *, ascii_bars: bool) -> list[Item]:
    """Column 3: `·` separated parts, the last without a filler."""
    return [
        Item(text, need, dot(ascii_bars) if index < len(texts) - 1 else Text())
        for index, (text, need) in enumerate(texts)
    ]


def value_text(used: int, total: int, suffix: str, unit: str, total_number: str) -> str:
    """`used/total UNIT suffix` with `used` right-aligned to the width of `total`."""
    return f"{size_in_unit(used, unit):>{len(total_number)}}/{total_number} {unit} {suffix}"


def gauge_left(lay: Layout, bar: Text | None, value: Text) -> Text:
    """Gauge (or its blank slot) and the value right-aligned in a fixed field."""
    head = Text()
    if lay.gauge:
        head = (bar or Text()).copy()
        head.truncate(lay.gauge)
        head += Text(" " * (lay.gauge - head.cell_len))
    total = max(lay.left, head.cell_len + (1 if lay.gauge else 0) + value.cell_len)
    return head + Text(" " * (total - head.cell_len - value.cell_len)) + value


@dataclass(frozen=True)
class State:
    """A state word in two forms: full (with its detail) and short (the word)."""

    full: Text
    full_need: int
    short: Text
    short_need: int


def state_left(lay: Layout, state: State, value: Text | None, value_need: int) -> tuple[Text, bool]:
    """State word and value share the left part, the word at the gauge slot and
    the value right-aligned at its end. Candidates in order: full form with the
    value, short word with the value, full form alone, short word alone; the
    first that fits the worst-case widths wins, so a word is never cut. Returns
    the part and whether the value had to be omitted."""
    left_width = lay.left
    forms = ((state.full, state.full_need), (state.short, state.short_need))
    candidates = [(word, need, True) for word, need in forms] if value is not None else []
    candidates += [(word, need, False) for word, need in forms]
    for word, need, with_value in candidates:
        if need + (1 + value_need if with_value else 0) <= left_width:
            tail = value if with_value and value is not None else Text()
            filler = left_width - word.cell_len - tail.cell_len
            return word + Text(" " * filler) + tail, value is not None and not with_value
    word = state.short
    return word + Text(" " * max(0, left_width - word.cell_len)), value is not None


def _fit(items: Sequence[Item], available: int, *, marker: bool) -> int:
    """How many leading items stay; the marker needs room whenever any is omitted."""
    kept, end, used = 0, 0, 0
    for index, item in enumerate(items, 1):
        end = used + item.need
        if end + (_MARKER_CELLS if index < len(items) or marker else 0) > available:
            break
        kept = index
        used = end + item.after.cell_len
    return kept


def grid_row(
    label: str,
    left: Text,
    lay: Layout,
    items: Sequence[Item],
    *,
    ascii_bars: bool = False,
    hidden: bool = False,
) -> Text:
    """One header row. `hidden` says the left part already omitted data."""
    text = Text(label.ljust(lay.label)) + left
    available = lay.width - text.cell_len - _SEPARATOR_CELLS
    kept = _fit(items, available, marker=hidden)
    marker = hidden or kept < len(items)
    if kept:
        text += Text(" | " if ascii_bars else " │ ", style="dim")
        measuring = lay.width >= _UNBOUNDED  # the row is as wide as its needs, not its text
        for index, item in enumerate(items[:kept], 1):
            piece = item.text.copy()
            if measuring:
                piece.truncate(item.need)
            text += piece
            if index < kept:
                text += Text(" " * (item.need - piece.cell_len)) + item.after
            elif measuring:
                text += Text(" " * (item.need - piece.cell_len))
            # With a marker the last item's own text ends the row: the marker
            # follows it at once, not the item's worst-case padding.
    if text.cell_len > lay.width - (_MARKER_CELLS if marker else 0):
        marker = True
        text.truncate(max(0, lay.width - _MARKER_CELLS))
        text.rstrip()
    if marker:
        text += Text(" " if text.cell_len < lay.width - 1 else "")
        text += Text(">" if ascii_bars else "…", style="")
    text.no_wrap = True
    text.overflow = "crop"
    return text
