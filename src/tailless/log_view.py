from dataclasses import dataclass
import re
from typing import Mapping

from rich.text import Text
from rich.segment import Segment
from rich.style import Style

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.reactive import reactive, var
from textual.scroll_view import ScrollView
from textual.cache import LRUCache

from textual.strip import Strip


from textual.suggester import Suggester

from .filter_dialog import FilterDialog
from .line_panel import LinePanel
from .mapped_file import MappedFile
from .watcher import Watcher

SPLIT_REGEX = r"[\s/\[\]]"


class SearchSuggester(Suggester):
    def __init__(self, search_index: Mapping[str, str]) -> None:
        self.search_index = search_index
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        word = re.split(SPLIT_REGEX, value)[-1]
        start = value[: -len(word)]

        if not word:
            return None
        search_hit = self.search_index.get(word.lower(), None)
        if search_hit is None:
            return None
        return start + search_hit


class LineKey:
    dimmed: bool
    highlighted: bool


class LogLines(ScrollView):
    DEFAULT_CSS = """
    LogLines {
        border: heavy transparent;        
        .loglines--filter-highlight {
            background: $secondary;
            color: auto;
        }
        .loglines--pointer-highlight {
            background: $primary;
        }
        &:focus {
            border: heavy $accent;
        }
    }
    """
    COMPONENT_CLASSES = {"loglines--filter-highlight", "loglines--pointer-highlight"}

    show_find = reactive(False)
    find = reactive("")
    case_sensitive = reactive(False)
    regex = reactive(False)
    show_gutter = reactive(False)
    pointer_line: reactive[int | None] = reactive(None)
    show_timestamps: reactive[bool] = reactive(True)
    is_scrolling: reactive[int] = reactive(int)

    GUTTER_WIDTH = 2

    @dataclass
    class PointerMoved(Message):
        pointer_line: int | None

        def can_replace(self, message: Message) -> bool:
            return isinstance(message, LogLines.PointerMoved)

    def __init__(self, watcher: Watcher, file_path: str) -> None:
        super().__init__()
        self.watcher = watcher
        self.mapped_file = MappedFile(watcher, file_path)
        self._render_line_cache: LRUCache[object, Strip] = LRUCache(maxsize=1000)
        self._max_width = 0
        self._search_index: LRUCache[str, str] = LRUCache(maxsize=10000)
        self._suggester = SearchSuggester(self._search_index)
        self.icons: dict[int, str] = {}

    @property
    def line_count(self) -> int:
        return self.mapped_file.line_count

    def clear_caches(self) -> None:
        self._render_line_cache.clear()

    def notify_style_update(self) -> None:
        self.clear_caches()

    def on_mount(self) -> None:
        self.mapped_file.open()
        self.mapped_file.scan_block(0, self.mapped_file.size)

    def on_unmount(self) -> None:
        self.mapped_file.close()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        index = y + scroll_y
        style = self.rich_style
        width, height = self.size
        if index >= self.mapped_file.line_count:
            return Strip.blank(width, style)

        is_pointer = self.pointer_line is not None and index == self.pointer_line
        cache_key = (index, is_pointer)

        try:
            strip = self._render_line_cache[cache_key]
        except KeyError:
            line, text, timestamp = self.mapped_file.get_text(index)
            if timestamp is not None and self.show_timestamps:
                text = Text.assemble((f"{timestamp} ", "bold  magenta"), text)
            text.stylize_before(style)

            if is_pointer:
                pointer_style = self.get_component_rich_style(
                    "loglines--pointer-highlight"
                )
                text.stylize(Style(bgcolor=pointer_style.bgcolor, bold=True))

            search_index = self._search_index

            for word in re.split(SPLIT_REGEX, text.plain):
                if len(word) <= 1:
                    continue
                for offset in range(1, len(word) - 1):
                    sub_word = word[:offset]
                    if sub_word in search_index:
                        if len(search_index[sub_word]) < len(word):
                            search_index[sub_word.lower()] = word
                    else:
                        search_index[sub_word.lower()] = word

            if self.find and self.show_find:
                self.highlight_find(text)
            strip = Strip(text.render(self.app.console), text.cell_len)
            self._max_width = max(self._max_width, strip.cell_length)
            self._render_line_cache[cache_key] = strip

        if is_pointer:
            pointer_style = self.get_component_rich_style("loglines--pointer-highlight")
            strip = strip.crop_extend(scroll_x, scroll_x + width, pointer_style)
        else:
            strip = strip.crop_extend(scroll_x, scroll_x + width, None)

        if self.show_gutter:
            if self.pointer_line is not None and index == self.pointer_line:
                icon = "👉"
            else:
                icon = self.icons.get(index, " ")
            icon_strip = Strip([Segment(icon)])
            icon_strip = icon_strip.adjust_cell_length(3)
            strip = Strip.join([icon_strip, strip])

        return strip

    def highlight_find(self, text: Text) -> None:
        filter_style = self.get_component_rich_style("loglines--filter-highlight")
        if self.regex:
            try:
                re.compile(self.find)
            except Exception:
                # Invalid regex
                return
            matches = list(
                re.finditer(
                    self.find,
                    text.plain,
                    flags=0 if self.case_sensitive else re.IGNORECASE,
                )
            )
            if matches:
                for match in matches:
                    text.stylize(filter_style, *match.span())
            else:
                text.stylize("dim")
        else:
            if not text.highlight_words(
                [self.find], filter_style, case_sensitive=self.case_sensitive
            ):
                text.stylize("dim")

    def check_match(self, line: str) -> bool:
        if not line:
            return True
        if self.regex:
            return (
                re.match(
                    self.find, line, flags=0 if self.case_sensitive else re.IGNORECASE
                )
                is not None
            )
        else:
            if self.case_sensitive:
                return self.find in line
            else:
                return self.find.lower() in line.lower()

    def advance_search(self, direction: int = 1) -> None:
        first = self.pointer_line is None
        start_line = (
            (self.scroll_offset.y if direction == 1 else self.max_scroll_y)
            if self.pointer_line is None
            else self.pointer_line + direction
        )
        if direction == 1:
            line_range = range(start_line, self.line_count)
        else:
            line_range = range(start_line, -1, -1)

        check_match = self.check_match
        scroll_y = self.scroll_offset.y
        max_scroll_y = scroll_y + self.scrollable_content_region.height - 1
        for line_no in line_range:
            line = self.mapped_file.get_line(line_no)
            if check_match(line):
                self.pointer_line = line_no
                break
        if self.pointer_line is not None and (
            self.pointer_line < scroll_y or self.pointer_line > max_scroll_y
        ):
            y_offset = self.pointer_line - self.scrollable_content_region.height // 2
            self.is_scrolling += 1

            async def on_complete():
                self.is_scrolling -= 1

            self.scroll_to(
                y=y_offset,
                animate=abs(y_offset - self.scroll_offset.y) > 1,
                duration=0.2,
                on_complete=on_complete,
            )

    def on_idle(self) -> None:
        self.virtual_size = Size(
            self._max_width + (self.GUTTER_WIDTH if self.show_gutter else 0),
            self.mapped_file.line_count,
        )
        if self.pointer_line is not None and not self.is_scrolling:
            scroll_y = self.scroll_offset.y
            if self.pointer_line < scroll_y:
                self.pointer_line = scroll_y
            elif self.pointer_line >= scroll_y + self.scrollable_content_region.height:
                self.pointer_line = scroll_y + self.scrollable_content_region.height - 1

    def watch_show_find(self, show_find: bool) -> None:
        self.clear_caches()
        if not show_find:
            self.pointer_line = None
            self.show_gutter = False

    def watch_find(self, find: str) -> None:
        if not find:
            self.pointer_line = None
            self.show_gutter = False
        self.clear_caches()

    def watch_case_sensitive(self) -> None:
        self.clear_caches()

    def watch_regex(self) -> None:
        self.clear_caches()

    def watch_show_timestamps(self) -> None:
        self.clear_caches()

    def watch_pointer_line(self, pointer_line: int | None) -> None:
        self.post_message(LogLines.PointerMoved(pointer_line))

    def on_click(self, event: events.Click) -> None:
        if self.show_find:
            self.show_gutter = True
            self.pointer_line = event.y + self.scroll_offset.y - self.gutter.top


class LogView(Horizontal):
    DEFAULT_CSS = """
    LogView {
        &.show-panel {
            LinePanel {
                display: block;
            }
        }

        LogLines {
            width: 1fr;            
        } 
        
        LinePanel {
            width: 1fr;
            display: none;
        }
    }
    """

    show_find = reactive(False)
    show_timestamps = reactive(True)
    show_panel = reactive(False)

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        super().__init__()

    def compose(self) -> ComposeResult:
        yield (log_lines := LogLines(self.app.watcher, self.file_path))
        yield LinePanel(log_lines.mapped_file)
        yield FilterDialog(log_lines._suggester)

    @on(FilterDialog.Update)
    def filter_dialog_update(self, event: FilterDialog.Update) -> None:
        log_lines = self.query_one(LogLines)
        log_lines.find = event.find
        log_lines.regex = event.regex
        log_lines.case_sensitive = event.case_sensitive

    def watch_show_find(self, show_find: bool) -> None:
        filter_dialog = self.query_one(FilterDialog)
        filter_dialog.set_class(show_find, "visible")
        self.query_one(LogLines).show_find = show_find
        if show_find:
            filter_dialog.query_one("Input").focus()
        else:
            self.query_one(LogLines).focus()

    def watch_show_timestamps(self, show_timestamps: bool) -> None:
        self.query_one(LogLines).show_timestamps = show_timestamps

    def watch_show_panel(self, show_panel: bool) -> None:
        self.set_class(show_panel, "show-panel")

    @on(FilterDialog.Dismiss)
    def dismiss_filter_dialog(self, event: FilterDialog.Dismiss) -> None:
        event.stop()
        log_lines = self.query_one(LogLines)
        self.show_find = False
        log_lines.show_gutter = False

    @on(FilterDialog.MovePointer)
    def move_pointer(self, event: FilterDialog.MovePointer) -> None:
        event.stop()
        log_lines = self.query_one(LogLines)
        log_lines.show_gutter = True
        log_lines.advance_search(event.direction)

    @on(FilterDialog.SelectLine)
    def select_line(self) -> None:
        self.show_panel = not self.show_panel

    @on(LogLines.PointerMoved)
    def pointer_moved(self, event: LogLines.PointerMoved):
        line_panel = self.query_one(LinePanel)
        if event.pointer_line is None:
            self.show_panel = False
        else:
            line_panel.line_no = event.pointer_line


if __name__ == "__main__":
    import sys

    mapped_file = MappedFile(sys.argv[1])

    mapped_file.open()
    mapped_file.scan_block(0, mapped_file.size)

    for n in range(10):
        print(repr(mapped_file.get_line(n)))

    mapped_file.close()