"""Playbook registry. Adding a use case = writing a Playbook subclass and registering it here."""
from __future__ import annotations

from .base import Playbook

_REGISTRY: dict[str, Playbook] = {}


def register(pb: type[Playbook]) -> type[Playbook]:
    _REGISTRY[pb.key] = pb()
    return pb


def get(key: str) -> Playbook:
    if key not in _REGISTRY:
        raise KeyError(f"unknown playbook '{key}'. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[key]


def all_playbooks() -> list[Playbook]:
    return list(_REGISTRY.values())


from . import medical_records, client_checkin, bill_followup  # noqa: E402,F401  (registers)
