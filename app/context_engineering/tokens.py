from typing import Protocol

import tiktoken


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class Utf8ByteTokenCounter:
    """Conservative local fallback when a tokenizer is unavailable.

    A BPE token contains at least one UTF-8 byte, so the encoded byte length
    cannot underestimate the token count. It may reserve more space than
    necessary, which is safer than overflowing the model context window.
    """

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


class TiktokenTokenCounter:
    def __init__(
        self,
        model: str,
        *,
        fallback: TokenCounter | None = None,
    ) -> None:
        self._fallback = fallback or Utf8ByteTokenCounter()
        try:
            self._encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            try:
                self._encoding = tiktoken.get_encoding("o200k_base")
            except OSError:
                self._encoding = None
        except OSError:
            self._encoding = None

    def count(self, text: str) -> int:
        if self._encoding is None:
            return self._fallback.count(text)
        return len(
            self._encoding.encode(
                text,
                disallowed_special=(),
            )
        )
