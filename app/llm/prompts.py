from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Mapping


class PromptNotFoundError(LookupError):
    pass


class PromptRenderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    name: str
    version: str
    instructions: str
    input_text: str
    prompt_hash: str


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: str
    instructions: str
    input_template: str
    required_variables: frozenset[str]

    def render(self, variables: Mapping[str, object]) -> RenderedPrompt:
        missing = self.required_variables - variables.keys()
        unexpected = variables.keys() - self.required_variables
        if missing or unexpected:
            raise PromptRenderError(
                f"Prompt variables mismatch: missing={sorted(missing)}, "
                f"unexpected={sorted(unexpected)}"
            )
        serialized = {
            key: json.dumps(
                variables[key],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for key in self.required_variables
        }
        input_text = self.input_template.format(**serialized)
        digest_input = json.dumps(
            {
                "name": self.name,
                "version": self.version,
                "instructions": self.instructions,
                "input_text": input_text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return RenderedPrompt(
            name=self.name,
            version=self.version,
            instructions=self.instructions,
            input_text=input_text,
            prompt_hash=sha256(digest_input).hexdigest(),
        )


class PromptRegistry:
    def __init__(self, prompts: list[PromptTemplate]) -> None:
        self._prompts = {
            (prompt.name, prompt.version): prompt for prompt in prompts
        }
        if len(self._prompts) != len(prompts):
            raise ValueError("Prompt name/version pairs must be unique.")

    def get(self, name: str, version: str) -> PromptTemplate:
        try:
            return self._prompts[(name, version)]
        except KeyError as exc:
            raise PromptNotFoundError(f"{name}@{version}") from exc
