"""Package repository spec (ADR 2026-10-13-package-repositories).

Config YAML (camelCase) and the REST API (snake_case) describe a repository
with the same shape; the aliases on SpecModel translate.
"""

from typing import Any, ClassVar, Literal, Optional

from pydantic import Field, model_validator

from app.schemas.spec.base import SpecModel

AUTH_TYPES = Literal["none", "bearer", "header"]


class PackageRepositoryAuthSpec(SpecModel):
    # For a private repository: a token sent with every request to it.
    type: AUTH_TYPES = "none"
    secret: Optional[str] = None  # the *name* of a Secret, never its value
    header: Optional[str] = None  # for type "header": the header to send it in


class PackageRepositorySpec(SpecModel):
    WHOLE_SPEC_FIELDS: ClassVar[frozenset[str]] = frozenset({"auth"})

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: Optional[str] = None
    # A GitHub repository (https://github.com/<owner>/<repo>, optionally
    # /tree/<branch>), or the base URL its index.json is served under.
    url: str = Field(min_length=1, max_length=2048, pattern=r"^https?://")
    auth: PackageRepositoryAuthSpec = Field(default_factory=PackageRepositoryAuthSpec)
    is_active: bool = True

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("description") == "":
            data = {**data, "description": None}  # the console sends "" for none
        return data

    @property
    def key(self) -> str:
        return self.name

    def whole_spec_problems(self) -> list[str]:
        auth = self.auth
        if auth.type in ("bearer", "header") and not auth.secret:
            return [f"auth.secret required for {auth.type} auth"]
        if auth.type == "header" and not auth.header:
            return ["auth.header required for header auth"]
        return []

    @model_validator(mode="after")
    def _whole_spec(self) -> "PackageRepositorySpec":
        problems = self.whole_spec_problems()
        if problems:
            raise ValueError(problems[0])
        return self

    def to_config(self) -> dict[str, Any]:
        out = super().to_config()
        if out.get("auth") == {"type": "none"}:
            out.pop("auth")
        return out
