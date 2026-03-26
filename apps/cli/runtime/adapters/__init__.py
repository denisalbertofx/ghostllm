"""Compatibility boundaries: legacy intake helpers. Prefer importing contract_intake from its module."""

from apps.cli.runtime.adapters.legacy_intake import (
    is_taskspec_engine_enabled,
    legacy_fill_session,
)

__all__ = ["is_taskspec_engine_enabled", "legacy_fill_session"]
