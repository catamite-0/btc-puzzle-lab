"""Small process-local issuance and integrity registry.

This is an API integrity guard, not a security boundary against arbitrary code
already executing in this process.  It lets public value types reject instances
that bypassed their controlled factory and instances changed after issuance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import weakref
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from threading import Lock
from typing import Generic, TypeVar

_T = TypeVar("_T")


class IssuanceIntegrityError(ValueError):
    """A value cannot be represented by the issuance integrity model."""


def _integrity_tree(value: object) -> object:
    """Return a strict JSON-compatible representation of an immutable graph."""

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is bytes:
        return {"bytes": value.hex()}
    if isinstance(value, Enum):
        enum_type = type(value)
        return {
            "enum": f"{enum_type.__module__}.{enum_type.__qualname__}",
            "name": value.name,
        }
    if isinstance(value, datetime):
        value_type = type(value)
        return {
            "datetime": f"{value_type.__module__}.{value_type.__qualname__}",
            "fold": value.fold,
            "value": value.isoformat(),
        }
    if type(value) is tuple:
        return {
            "tuple_id": id(value),
            "items": [_integrity_tree(item) for item in value],
        }
    if is_dataclass(value) and not isinstance(value, type):
        value_type = type(value)
        return {
            "dataclass": f"{value_type.__module__}.{value_type.__qualname__}",
            "object_id": id(value),
            "fields": [
                [field.name, _integrity_tree(getattr(value, field.name))] for field in fields(value)
            ],
        }
    raise IssuanceIntegrityError(
        f"unsupported issuance integrity value: {type(value).__module__}.{type(value).__qualname__}"
    )


def _integrity_digest(value: object) -> bytes:
    try:
        payload = json.dumps(
            _integrity_tree(value),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (AttributeError, TypeError, ValueError) as exc:
        raise IssuanceIntegrityError(
            "value has no valid immutable integrity representation"
        ) from exc
    return hashlib.sha256(payload).digest()


class ProcessLocalIssuance(Generic[_T]):
    """Track exact factory-issued objects without extending their lifetime."""

    def __init__(self, value_type: type[_T]) -> None:
        self._value_type = value_type
        self._records: dict[int, tuple[weakref.ReferenceType[_T], bytes]] = {}
        self._lock = Lock()

    def issue(self, value: _T) -> _T:
        if type(value) is not self._value_type:
            raise IssuanceIntegrityError("only the exact registered type may be issued")
        digest = _integrity_digest(value)
        identifier = id(value)

        def discard(reference: weakref.ReferenceType[_T]) -> None:
            with self._lock:
                current = self._records.get(identifier)
                if current is not None and current[0] is reference:
                    del self._records[identifier]

        reference = weakref.ref(value, discard)
        with self._lock:
            self._records[identifier] = (reference, digest)
        return value

    def is_valid(self, value: object) -> bool:
        if type(value) is not self._value_type:
            return False
        with self._lock:
            record = self._records.get(id(value))
        if record is None or record[0]() is not value:
            return False
        try:
            current_digest = _integrity_digest(value)
        except IssuanceIntegrityError:
            return False
        return hmac.compare_digest(record[1], current_digest)


__all__: list[str] = []
