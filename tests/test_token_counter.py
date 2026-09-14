from app.context_engineering import tokens


class StubEncoding:
    def encode(
        self,
        text: str,
        *,
        disallowed_special: tuple[()] = (),
    ) -> list[str]:
        del disallowed_special
        return text.split()


def test_tiktoken_counter_uses_model_encoding_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        tokens.tiktoken,
        "encoding_for_model",
        lambda model: StubEncoding(),
    )

    counter = tokens.TiktokenTokenCounter("known-model")

    assert counter.count("one two three") == 3


def test_unknown_model_uses_conservative_fallback_when_download_fails(
    monkeypatch,
) -> None:
    def unknown_model(model: str) -> StubEncoding:
        raise KeyError(model)

    def unavailable_encoding(name: str) -> StubEncoding:
        raise OSError(f"encoding unavailable: {name}")

    monkeypatch.setattr(tokens.tiktoken, "encoding_for_model", unknown_model)
    monkeypatch.setattr(tokens.tiktoken, "get_encoding", unavailable_encoding)

    counter = tokens.TiktokenTokenCounter("future-model")

    assert counter.count("A中") == len("A中".encode("utf-8"))


def test_known_model_uses_conservative_fallback_when_cache_is_unavailable(
    monkeypatch,
) -> None:
    def unavailable_model_encoding(model: str) -> StubEncoding:
        raise OSError(f"encoding unavailable: {model}")

    monkeypatch.setattr(
        tokens.tiktoken,
        "encoding_for_model",
        unavailable_model_encoding,
    )

    counter = tokens.TiktokenTokenCounter("known-model")

    assert counter.count("") == 0
    assert counter.count("中文") == len("中文".encode("utf-8"))
