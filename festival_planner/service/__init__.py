"""The service layer: the only write path. Every mutation records an activity row.

This module holds the types shared by every service module and adapter (Task 4 interface).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    """Who made a change: `source` is web, email, system or a token name; `actor` is the
    organizer email, token name or similar. Both come only from the authenticated identity."""

    source: str
    actor: str


class ValidationError(Exception):
    """Input failed validation. `errors` maps a field name to a message."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("validation failed")
        self.errors = errors


class NotFound(Exception):
    """The requested entity does not exist (or is soft-deleted)."""
