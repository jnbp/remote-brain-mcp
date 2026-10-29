"""Markdown helpers: frontmatter, title, wikilinks and line handling."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import yaml

MAX_FRONTMATTER_CHARS = 64 * 1024
MAX_FRONTMATTER_DEPTH = 32
MAX_FRONTMATTER_ITEMS = 10_000

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")


class Fences:
    """Track fenced code blocks the CommonMark way: a block closes only with a fence of the
    same character that is at least as long as the opening one."""

    def __init__(self) -> None:
        self.open: str | None = None

    def skip(self, line: str) -> bool:
        """Feed one line; True if it is a fence line or inside a fenced block."""
        match = _FENCE_OPEN.match(line)
        if self.open is None:
            if match is None:
                return False
            fence = match.group(1)
            if fence[0] == "`" and "`" in line[match.end():]:
                return False  # backtick fences cannot have backticks in their info string
            self.open = fence
            return True
        if (
            match is not None
            and match.group(1)[0] == self.open[0]
            and len(match.group(1)) >= len(self.open)
            and line[match.end():].strip() == ""
        ):
            self.open = None
        return True


def code_spans(line: str) -> list[tuple[int, int]]:
    """Character ranges of inline code spans (`code`, ``code``)."""
    return [(m.start(), m.end()) for m in _CODE_SPAN.finditer(line)]


def _in_spans(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


class _NoAliasLoader(yaml.SafeLoader):
    """Safe loader that refuses YAML aliases (prevents self-references and 'billion laughs')."""

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.events.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(None, None, "aliases are not allowed", event.start_mark)
        return super().compose_node(parent, index)


def _within_limits(value: Any) -> bool:
    """Check depth and total size of parsed frontmatter without recursion."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    items = 0
    while stack:
        current, depth = stack.pop()
        items += 1
        if depth > MAX_FRONTMATTER_DEPTH or items > MAX_FRONTMATTER_ITEMS:
            return False
        if isinstance(current, dict):
            stack.extend((v, depth + 1) for v in current.values())
            stack.extend((k, depth + 1) for k in current)
        elif isinstance(current, (list, tuple, set)):
            stack.extend((v, depth + 1) for v in current)
    return True


def parse_yaml(raw: str) -> dict[str, Any]:
    """Parse frontmatter defensively: any problem yields an empty dict, never an exception."""
    if not raw.strip() or len(raw) > MAX_FRONTMATTER_CHARS:
        return {}
    try:
        data = yaml.load(raw, Loader=_NoAliasLoader)
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or not _within_limits(data):
        return {}
    return data


_LINK = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_LINE = re.compile(r"[^\n]*\n|[^\n]+$")


def norm(text: str) -> str:
    """Comparison form: NFC, case-folded."""
    return unicodedata.normalize("NFC", text).casefold()


def split_lines(text: str) -> list[str]:
    """Split on '\\n' only, keeping line endings (unlike str.splitlines)."""
    return _LINE.findall(text)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    """Return (frontmatter dict, body, number of lines used by the frontmatter)."""
    lines = split_lines(text)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text, 0
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") in ("---", "..."):
            raw = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return parse_yaml(raw), body, index + 1
    return {}, text, 0


def title_of(body: str, fallback: str) -> str:
    fences = Fences()
    for line in split_lines(body):
        if fences.skip(line):
            continue
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


@dataclass(frozen=True)
class Link:
    target: str  # as written, without alias and heading
    line: int  # 1-based line number in the whole file
    text: str  # the line, shortened


def parse_link_target(raw: str) -> str:
    """`Name|alias`, `Name#Heading`, `Name\\|alias` (inside tables) -> `Name`."""
    target = raw.split("|", 1)[0]
    target = target.removesuffix("\\")
    target = target.split("#", 1)[0]
    return target.strip()


def extract_links(text: str) -> list[Link]:
    links: list[Link] = []
    fences = Fences()
    for number, line in enumerate(split_lines(text), start=1):
        if fences.skip(line) or "[[" not in line:
            continue
        spans = code_spans(line)
        for match in _LINK.finditer(line):
            if _in_spans(match.start(), spans):
                continue
            target = parse_link_target(match.group(2))
            if target:
                links.append(Link(target=target, line=number, text=line.strip()[:300]))
    return links


def frontmatter_shape(value: Any) -> Any:
    """Frontmatter with every wikilink target blanked: equal shapes mean only targets differ."""
    if isinstance(value, dict):
        return sorted(((frontmatter_shape(k), frontmatter_shape(v)) for k, v in value.items()), key=repr)
    if isinstance(value, (list, tuple)):
        return [frontmatter_shape(v) for v in value]
    if isinstance(value, str):
        return _LINK.sub(lambda m: m.group(1) + "[[]]", value)
    return value


def frontmatter_link_targets(value: Any) -> list[str]:
    """Every wikilink target in parsed frontmatter (keys and values), as YAML decoded it."""
    targets: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.keys())
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
        elif isinstance(current, str):
            targets.extend(_split_link_inner(m.group(2))[0].strip() for m in _LINK.finditer(current))
    return targets


def frontmatter_values(value: Any) -> list[str]:
    """Flatten a frontmatter value into normalised strings for matching."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(frontmatter_values(item))
        return result
    if isinstance(value, dict):
        return []
    return [normalize_value(str(value))]


def normalize_value(value: str) -> str:
    """`[[X]]`, `"[[X|alias]]"` and `X` compare equal."""
    text = value.strip().strip('"').strip("'").strip()
    match = re.fullmatch(r"\[\[(.+)\]\]", text)
    if match:
        text = parse_link_target(match.group(1))
    return norm(text)


def count_overlapping(haystack: str, needle: str) -> int:
    count = 0
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return count
        count += 1
        start = index + 1


# ---------------------------------------------------------------- link rewriting


def _split_link_inner(inner: str) -> tuple[str, str]:
    """Split `Target#Heading|alias` (or `Target\\|alias` in tables) into (target, rest)."""
    cut = len(inner)
    for marker in ("#", "|"):
        position = inner.find(marker)
        if position != -1 and position < cut:
            cut = position
    if cut < len(inner) and inner[cut] == "|" and cut > 0 and inner[cut - 1] == "\\":
        cut -= 1
    elif cut == len(inner) and inner.endswith("\\"):
        cut -= 1  # `[[Name\\]]`: the index reads the target as `Name`, so do we
    return inner[:cut], inner[cut:]


# Characters that cannot round-trip through a wikilink target.
LINK_UNSAFE = set("[]|#^`\\")


_YAML_SAFE = re.compile(r"[\w .\-/()+]+")


def yaml_safe_link_text(text: str) -> bool:
    """True if `text` can sit inside a YAML scalar (quoted or not) without any escaping."""
    return bool(_YAML_SAFE.fullmatch(text)) and text == text.strip()


def link_safe_path(rel: str) -> bool:
    """True if every segment of `rel` can be written into a [[wikilink]] unchanged."""
    for segment in rel.split("/"):
        if set(segment) & LINK_UNSAFE or "%%" in segment or segment != segment.strip():
            return False
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in segment):
            return False
    return True


def rewrite_links(text: str, new_target_for) -> tuple[str, int]:
    """Rewrite wikilink targets outside code blocks.

    `new_target_for(target)` returns the replacement target text or None to keep the
    link. Alias, heading/block part and the embed marker are preserved.
    """
    changed = 0
    fences = Fences()
    out: list[str] = []
    for line in split_lines(text):
        if fences.skip(line) or "[[" not in line:
            out.append(line)
            continue
        spans = code_spans(line)

        def replace(match: re.Match[str], spans: list[tuple[int, int]] = spans) -> str:
            nonlocal changed
            if _in_spans(match.start(), spans):
                return match.group(0)
            target, rest = _split_link_inner(match.group(2))
            replacement = new_target_for(target.strip())
            if replacement is None or replacement == target.strip():
                return match.group(0)
            changed += 1
            return f"{match.group(1)}[[{replacement}{rest}]]"

        out.append(_LINK.sub(replace, line))
    return "".join(out), changed


# ---------------------------------------------------------------- sections

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*\r?\n?$")


def headings(lines: list[str], start: int = 0) -> list[tuple[int, int, str]]:
    """(line index, level, title) for every ATX heading outside code blocks,
    ignoring the first `start` lines (the frontmatter)."""
    found = []
    fences = Fences()
    for index, line in enumerate(lines):
        if index < start or fences.skip(line):
            continue
        match = _HEADING.match(line)
        if match and match.group(2).strip():
            found.append((index, len(match.group(1)), match.group(2).strip()))
    return found
