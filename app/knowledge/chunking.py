from dataclasses import dataclass
import re

from app.knowledge.errors import KnowledgeParseError
from app.knowledge.markdown import (
    MarkdownBlock,
    MarkdownSection,
    ParsedMarkdownDocument,
)

SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+")


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    section_path: tuple[str, ...]
    content: str
    source_line_start: int
    source_line_end: int


class HeadingAwareChunker:
    """Keep heading meaning while packing paragraph/sentence units."""

    def __init__(
        self,
        *,
        target_chars: int = 800,
        max_chars: int = 1200,
    ) -> None:
        if target_chars < 100:
            raise ValueError("target_chars must be at least 100.")
        if max_chars < target_chars:
            raise ValueError("max_chars must not be below target_chars.")
        self._target_chars = target_chars
        self._max_chars = max_chars

    def chunk(
        self,
        document: ParsedMarkdownDocument,
        *,
        fallback_title: str,
    ) -> tuple[ChunkDraft, ...]:
        drafts: list[ChunkDraft] = []
        for section in document.sections:
            drafts.extend(
                self._chunk_section(
                    section,
                    fallback_title=fallback_title,
                )
            )
        if not drafts:
            raise KnowledgeParseError(
                "knowledge_document_no_chunks",
                "The policy document produced no chunks.",
            )
        return tuple(drafts)

    def _chunk_section(
        self,
        section: MarkdownSection,
        *,
        fallback_title: str,
    ) -> list[ChunkDraft]:
        section_path = section.heading_path or (fallback_title,)
        prefix = "\n".join(
            f"{'#' * (index + 1)} {title}"
            for index, title in enumerate(section_path)
        )
        body_budget = self._max_chars - len(prefix) - 2
        if body_budget < 50:
            raise KnowledgeParseError(
                "knowledge_heading_too_long",
                "The heading path leaves no room for chunk content.",
            )

        units: list[MarkdownBlock] = []
        for block in section.blocks:
            units.extend(self._split_block(block, body_budget))

        result: list[ChunkDraft] = []
        current: list[MarkdownBlock] = []
        current_size = 0
        for unit in units:
            separator_size = 2 if current else 0
            proposed = current_size + separator_size + len(unit.text)
            if current and (
                proposed > body_budget
                or current_size >= self._target_chars
            ):
                result.append(self._build_draft(section, prefix, current))
                current = []
                current_size = 0
                separator_size = 0
            current.append(unit)
            current_size += separator_size + len(unit.text)
        if current:
            result.append(self._build_draft(section, prefix, current))
        return result

    def _split_block(
        self,
        block: MarkdownBlock,
        body_budget: int,
    ) -> list[MarkdownBlock]:
        if len(block.text) <= body_budget:
            return [block]
        sentences = [
            item.strip()
            for item in SENTENCE_BOUNDARY.split(block.text)
            if item.strip()
        ]
        if len(sentences) <= 1:
            sentences = [
                block.text[index : index + body_budget]
                for index in range(0, len(block.text), body_budget)
            ]

        pieces: list[MarkdownBlock] = []
        buffer = ""
        for sentence in sentences:
            if len(sentence) > body_budget:
                if buffer:
                    pieces.append(self._piece(block, buffer))
                    buffer = ""
                pieces.extend(
                    self._piece(block, sentence[index : index + body_budget])
                    for index in range(0, len(sentence), body_budget)
                )
                continue
            proposed = f"{buffer} {sentence}".strip()
            if buffer and len(proposed) > body_budget:
                pieces.append(self._piece(block, buffer))
                buffer = sentence
            else:
                buffer = proposed
        if buffer:
            pieces.append(self._piece(block, buffer))
        return pieces

    @staticmethod
    def _piece(block: MarkdownBlock, text: str) -> MarkdownBlock:
        return MarkdownBlock(
            text=text,
            line_start=block.line_start,
            line_end=block.line_end,
        )

    @staticmethod
    def _build_draft(
        section: MarkdownSection,
        prefix: str,
        blocks: list[MarkdownBlock],
    ) -> ChunkDraft:
        body = "\n\n".join(block.text for block in blocks)
        start = min(block.line_start for block in blocks)
        if section.heading_line is not None:
            start = min(start, section.heading_line)
        return ChunkDraft(
            section_path=section.heading_path,
            content=f"{prefix}\n\n{body}",
            source_line_start=start,
            source_line_end=max(block.line_end for block in blocks),
        )
