import pytest

from app.knowledge.chunking import HeadingAwareChunker
from app.knowledge.errors import KnowledgeParseError
from app.knowledge.markdown import MarkdownPolicyParser


def test_parser_preserves_heading_hierarchy_blocks_and_source_lines() -> None:
    source = """政策前言。

# 售后政策

## 未收到货

物流显示签收时，必须查询签收证明。

- 没有证明：转人工
- 有证明：核验签收信息

## 退款

退款必须经过人工审批。
"""

    parsed = MarkdownPolicyParser().parse(source)
    chunks = HeadingAwareChunker().chunk(
        parsed,
        fallback_title="售后政策",
    )

    assert [item.section_path for item in chunks] == [
        (),
        ("售后政策", "未收到货"),
        ("售后政策", "退款"),
    ]
    assert chunks[0].source_line_start == 1
    assert chunks[1].source_line_start == 5
    assert chunks[1].source_line_end == 10
    assert "# 售后政策\n## 未收到货" in chunks[1].content
    assert "必须查询签收证明" in chunks[1].content


def test_markdown_heading_inside_fence_is_not_a_section() -> None:
    source = """# 政策

```text
# 这不是标题
```

正文内容。
"""

    parsed = MarkdownPolicyParser().parse(source)

    assert len(parsed.sections) == 1
    assert parsed.sections[0].heading_path == ("政策",)
    assert "# 这不是标题" in parsed.sections[0].blocks[0].text


def test_long_chinese_paragraph_splits_at_sentence_boundaries() -> None:
    source = "# 规则\n\n" + "第一条规则必须核验。" * 30

    chunks = HeadingAwareChunker(
        target_chars=100,
        max_chars=140,
    ).chunk(
        MarkdownPolicyParser().parse(source),
        fallback_title="规则",
    )

    assert len(chunks) >= 2
    assert all(len(item.content) <= 140 for item in chunks)
    assert all(item.content.endswith("。") for item in chunks)


@pytest.mark.parametrize("source", ["", "  \n\n  ", "\ufeff\n"])
def test_empty_document_is_rejected(source: str) -> None:
    with pytest.raises(
        KnowledgeParseError,
        match="no meaningful content",
    ):
        MarkdownPolicyParser().parse(source)

