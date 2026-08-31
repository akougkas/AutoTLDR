"""Typed error types for AutoTLDR."""

from __future__ import annotations


class MissingOptionalDependency(ImportError):
    """Raised when an optional dependency required for an input format is missing.

    Carries explicit feature, dependency, and install extra information without
    leaking raw module exception objects or confusing programmer defects.
    """

    def __init__(
        self,
        feature: str,
        dependency: str,
        extra: str = "data",
        detail: str | None = None,
    ) -> None:
        self.feature = feature
        self.dependency = dependency
        self.extra = extra
        self.detail = detail or f"{dependency} is required for {feature} support"
        super().__init__(f"{self.detail}; install it with: pip install 'autotldr[{extra}]'")
