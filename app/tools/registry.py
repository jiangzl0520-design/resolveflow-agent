from app.domain.auth import AuthenticatedActor
from app.tools.contracts import (
    ToolDefinition,
    ToolDescriptor,
)
from app.tools.errors import ToolNotFoundError


class ToolRegistry:
    def __init__(self, definitions: list[ToolDefinition]) -> None:
        self._definitions = {
            (definition.name, definition.version): definition
            for definition in definitions
        }
        if len(self._definitions) != len(definitions):
            raise ValueError("Tool name/version pairs must be unique.")

    def resolve(self, name: str, version: str) -> ToolDefinition:
        try:
            return self._definitions[(name, version)]
        except KeyError as exc:
            raise ToolNotFoundError(name, version) from exc

    def descriptors_for(
        self,
        actor: AuthenticatedActor,
    ) -> tuple[ToolDescriptor, ...]:
        allowed = (
            definition
            for definition in self._definitions.values()
            if actor.has_permission(definition.required_permission)
        )
        return tuple(
            ToolDescriptor(
                name=definition.name,
                version=definition.version,
                description=definition.description,
                input_schema=definition.input_schema,
                risk_level=definition.risk_level,
                side_effect=definition.side_effect,
            )
            for definition in sorted(
                allowed,
                key=lambda item: (item.name, item.version),
            )
        )
