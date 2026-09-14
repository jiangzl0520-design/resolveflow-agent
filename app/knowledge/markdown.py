from dataclasses import dataclass
import re

from app.knowledge.errors import KnowledgeParseError

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    text: str
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    heading_path: tuple[str, ...]
    heading_line: int | None
    blocks: tuple[MarkdownBlock, ...]


@dataclass(frozen=True, slots=True)
class ParsedMarkdownDocument:
    sections: tuple[MarkdownSection, ...]
    line_count: int


class MarkdownPolicyParser:
    """Parse Markdown into heading-scoped paragraph/list blocks."""

    def parse(self, source_text: str) -> ParsedMarkdownDocument:
        normalized = source_text.lstrip("\ufeff").replace("\r\n", "\n")
        normalized = normalized.replace("\r", "\n")
        if not normalized.strip():
            raise KnowledgeParseError(
                "knowledge_document_empty",
                "The policy document has no meaningful content.",
            )

        lines = normalized.split("\n")
        sections: list[MarkdownSection] = []
        heading_stack: list[str] = []
        current_path: tuple[str, ...] = ()
        current_heading_line: int | None = None
        current_blocks: list[MarkdownBlock] = []
        paragraph_lines: list[str] = []
        paragraph_start: int | None = None
        inside_fence = False

        def flush_paragraph(line_end: int) -> None:
            nonlocal paragraph_lines, paragraph_start
            if paragraph_start is None:
                return
            text = "\n".join(paragraph_lines).strip()
            if text:
                current_blocks.append(
                    MarkdownBlock(
                        text=text,
                        line_start=paragraph_start,
                        line_end=line_end,
                    )
                )
            paragraph_lines = []
            paragraph_start = None

        def flush_section() -> None:
            if current_blocks:
                sections.append(
                    MarkdownSection(
                        heading_path=current_path,
                        heading_line=current_heading_line,
                        blocks=tuple(current_blocks),
                    )
                )
                current_blocks.clear()

        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                inside_fence = not inside_fence

            heading_match = (
                None if inside_fence else HEADING_PATTERN.match(stripped)
            )
            if heading_match is not None:
                flush_paragraph(line_number - 1)
                flush_section()
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                heading_stack[level - 1 :] = [title]
                current_path = tuple(heading_stack)
                current_heading_line = line_number
                continue

            if not stripped and not inside_fence:
                flush_paragraph(line_number - 1)
                continue

            if paragraph_start is None:
                paragraph_start = line_number
            paragraph_lines.append(line.rstrip())

        flush_paragraph(len(lines))
        flush_section()

        if not sections:
            raise KnowledgeParseError(
                "knowledge_document_no_blocks",
                "The policy document contains no indexable blocks.",
            )
        return ParsedMarkdownDocument(
            sections=tuple(sections),
            line_count=len(lines),
        )

