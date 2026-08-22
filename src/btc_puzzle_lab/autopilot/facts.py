"""Immutable public facts for autopilot planning.

The records here describe catalog, chain-provider, and host observations.  They
perform no I/O, read no environment state, and contain no solution or transfer
material.  Decisions, estimates, engine adapters, and execution plans do not
belong in this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import base58
from cryptography.hazmat.primitives.asymmetric import ec

from btc_puzzle_lab.crypto import (
    SECP256K1_N,
    address_hash160,
    decode_segwit_address,
    hash160,
    is_valid_btc_address,
)


class DomainValidationError(ValueError):
    """A public fact would encode invalid or contradictory data."""


def _require_int(value: object, *, name: str, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise DomainValidationError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise DomainValidationError(f"{name} must be >= {minimum}")
    return value


def _require_bool(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise DomainValidationError(f"{name} must be a boolean")
    return value


def _require_text(value: object, *, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{name} must be text")
    if not value or value != value.strip():
        raise DomainValidationError(f"{name} must be non-empty and trimmed")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise DomainValidationError(f"{name} contains invalid characters")
    return value


def _require_datetime(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DomainValidationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{name} must be timezone-aware")
    return value


def _require_enum(value: object, enum_type: type[StrEnum], *, name: str) -> None:
    if not isinstance(value, enum_type):
        raise DomainValidationError(f"{name} must be a {enum_type.__name__}")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class TargetMode(StrEnum):
    LIVE = "live"
    PRACTICE = "practice"


class ResourceClass(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class EngineName(StrEnum):
    """Stable engine identity used by planning."""

    SEQUENTIAL = "sequential"
    KEYHUNT = "keyhunt"
    KANGAROO = "kangaroo"
    RCKANGAROO = "rckangaroo"
    BITCRACK = "bitcrack"


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyRange:
    """An inclusive secp256k1 private-scalar search interval."""

    start: int
    end: int

    def __post_init__(self) -> None:
        _require_int(self.start, name="start", minimum=1)
        _require_int(self.end, name="end", minimum=1)
        if self.start > self.end:
            raise DomainValidationError("start must not exceed end")
        if self.end >= SECP256K1_N:
            raise DomainValidationError("key range exceeds the secp256k1 scalar domain")

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def contains(self, candidate: int) -> bool:
        _require_int(candidate, name="candidate")
        return self.start <= candidate <= self.end

    def contains_range(self, other: KeyRange) -> bool:
        if not isinstance(other, KeyRange):
            raise DomainValidationError("other must be a KeyRange")
        return self.start <= other.start and other.end <= self.end


_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _public_key_bytes(value: str) -> bytes:
    _require_text(value, name="public_key_hex", maximum=130)
    if len(value) % 2 or not _HEX_RE.fullmatch(value):
        raise DomainValidationError("public_key_hex must be lower-case hexadecimal")
    encoded = bytes.fromhex(value)
    if not (
        (len(encoded) == 33 and encoded[0] in (2, 3)) or (len(encoded) == 65 and encoded[0] == 4)
    ):
        raise DomainValidationError("public_key_hex must encode a SEC public key")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), encoded)
    except ValueError as exc:
        raise DomainValidationError("public_key_hex is not a secp256k1 point") from exc
    return encoded


def _public_key_matches_address(public_key: bytes, address: str) -> bool:
    if address.startswith("1"):
        try:
            return address_hash160(address) == hash160(public_key)
        except ValueError:
            return False
    if address.startswith("bc1") and len(public_key) == 33:
        try:
            witness_version, witness_program = decode_segwit_address("bc", address)
        except ValueError:
            return False
        return witness_version == 0 and witness_program == hash160(public_key)
    return False


def _script_pubkey_for_address(address: str) -> str:
    """Return the canonical locking script for a validated mainnet address."""

    if address.startswith("1"):
        return (b"\x76\xa9\x14" + address_hash160(address) + b"\x88\xac").hex()
    if address.startswith("3"):
        try:
            payload = base58.b58decode_check(address)
        except ValueError as exc:
            raise DomainValidationError("invalid P2SH address") from exc
        if len(payload) != 21 or payload[0] != 5:
            raise DomainValidationError("invalid mainnet P2SH address")
        return (b"\xa9\x14" + payload[1:] + b"\x87").hex()
    try:
        witness_version, witness_program = decode_segwit_address("bc", address)
    except ValueError as exc:
        raise DomainValidationError("invalid SegWit address") from exc
    opcode = 0 if witness_version == 0 else 0x50 + witness_version
    return bytes((opcode, len(witness_program))).hex() + witness_program.hex()


@dataclass(frozen=True, slots=True, kw_only=True)
class PuzzleTarget:
    puzzle_id: int
    key_range: KeyRange
    address: str
    mode: TargetMode
    bits_label: int | None = None
    public_key_hex: str | None = None
    practice_fixture_id: str | None = None

    def __post_init__(self) -> None:
        _require_int(self.puzzle_id, name="puzzle_id", minimum=1)
        if not isinstance(self.key_range, KeyRange):
            raise DomainValidationError("key_range must be a KeyRange")
        _require_text(self.address, name="address", maximum=90)
        if not is_valid_btc_address(self.address):
            raise DomainValidationError("address must be a valid mainnet Bitcoin address")
        _require_enum(self.mode, TargetMode, name="mode")
        if self.bits_label is not None:
            _require_int(self.bits_label, name="bits_label", minimum=1)
        if self.public_key_hex is not None:
            public_key = _public_key_bytes(self.public_key_hex)
            if not _public_key_matches_address(public_key, self.address):
                raise DomainValidationError("public_key_hex does not match target address")
        if self.mode is TargetMode.PRACTICE:
            if self.practice_fixture_id is None:
                raise DomainValidationError("practice target requires a public fixture id")
            _require_text(
                self.practice_fixture_id,
                name="practice_fixture_id",
                maximum=256,
            )
        elif self.practice_fixture_id is not None:
            raise DomainValidationError("live target cannot carry a practice fixture id")

    @property
    def range_size(self) -> int:
        """Return the real range size; ``bits_label`` is display-only."""

        return self.key_range.size

    @property
    def has_public_key(self) -> bool:
        return self.public_key_hex is not None


class ChainState(StrEnum):
    FUNDED_CONFIRMED = "FUNDED_CONFIRMED"
    FUNDED_UNCONFIRMED = "FUNDED_UNCONFIRMED"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"


class ChainPurpose(StrEnum):
    SELECTION = "selection"
    LAUNCH = "launch"
    TRANSFER = "transfer"


class ProviderAuthority(StrEnum):
    TRUSTED_NODE = "trusted_node"
    PUBLIC = "public"


class ProviderOutcome(StrEnum):
    OK = "ok"
    ERROR = "error"


MAX_BITCOIN_SUPPLY_SATS: Final = 2_100_000_000_000_000
CHAIN_TTL_SECONDS: Final = MappingProxyType(
    {
        ChainPurpose.SELECTION: 300,
        ChainPurpose.LAUNCH: 60,
        ChainPurpose.TRANSFER: 15,
    }
)
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


def _require_safe_id(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise DomainValidationError(f"{name} must be a lower-case safe identifier")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ChainUtxo:
    txid: str
    vout: int
    value_sats: int
    script_pubkey_hex: str
    confirmed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.txid, str) or len(self.txid) != 64:
            raise DomainValidationError("txid must be 32-byte lower-case hexadecimal")
        if not _HEX_RE.fullmatch(self.txid):
            raise DomainValidationError("txid must be 32-byte lower-case hexadecimal")
        _require_int(self.vout, name="vout", minimum=0)
        _require_int(self.value_sats, name="value_sats", minimum=1)
        if self.value_sats > MAX_BITCOIN_SUPPLY_SATS:
            raise DomainValidationError("value_sats exceeds Bitcoin's maximum supply")
        if (
            not isinstance(self.script_pubkey_hex, str)
            or not self.script_pubkey_hex
            or len(self.script_pubkey_hex) % 2
            or not _HEX_RE.fullmatch(self.script_pubkey_hex)
        ):
            raise DomainValidationError("script_pubkey_hex must be lower-case byte-aligned hex")
        _require_bool(self.confirmed, name="confirmed")

    @property
    def outpoint(self) -> tuple[str, int]:
        return self.txid, self.vout


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderObservation:
    provider_id: str
    authority: ProviderAuthority
    independence_group: str
    outcome: ProviderOutcome
    address: str
    checked_at: datetime
    tip_height: int | None = None
    utxos: tuple[ChainUtxo, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.provider_id, name="provider_id")
        _require_enum(self.authority, ProviderAuthority, name="authority")
        _require_safe_id(self.independence_group, name="independence_group")
        _require_enum(self.outcome, ProviderOutcome, name="outcome")
        _require_text(self.address, name="address", maximum=90)
        if not is_valid_btc_address(self.address):
            raise DomainValidationError("address must be a valid mainnet Bitcoin address")
        _require_datetime(self.checked_at, name="checked_at")
        if type(self.utxos) is not tuple:
            raise DomainValidationError("utxos must be a tuple")
        if any(not isinstance(utxo, ChainUtxo) for utxo in self.utxos):
            raise DomainValidationError("utxos must contain ChainUtxo values")
        canonical = tuple(sorted(self.utxos, key=lambda item: item.outpoint))
        if len({item.outpoint for item in canonical}) != len(canonical):
            raise DomainValidationError("provider observation contains duplicate outpoints")
        if sum(item.value_sats for item in canonical) > MAX_BITCOIN_SUPPLY_SATS:
            raise DomainValidationError("provider observation exceeds Bitcoin's maximum supply")
        object.__setattr__(self, "utxos", canonical)
        if self.outcome is ProviderOutcome.OK:
            _require_int(self.tip_height, name="tip_height", minimum=0)
            if self.error_code is not None:
                raise DomainValidationError("successful observation cannot have error_code")
        else:
            if self.tip_height is not None or self.utxos:
                raise DomainValidationError("failed observation cannot claim chain data")
            if self.error_code is None:
                raise DomainValidationError("failed observation requires error_code")
            _require_safe_id(self.error_code, name="error_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChainSnapshot:
    """A purpose-bound chain fact derived only from typed observations."""

    target_id: int
    address: str
    purpose: ChainPurpose
    observations: tuple[ProviderObservation, ...]
    state: ChainState = field(init=False)
    agreed_utxos: tuple[ChainUtxo, ...] = field(init=False)
    unknown_reason: str | None = field(init=False)
    evidence_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_int(self.target_id, name="target_id", minimum=1)
        _require_text(self.address, name="address", maximum=90)
        if not is_valid_btc_address(self.address):
            raise DomainValidationError("address must be a valid mainnet Bitcoin address")
        _require_enum(self.purpose, ChainPurpose, name="purpose")
        if type(self.observations) is not tuple or not self.observations:
            raise DomainValidationError("observations must be a non-empty tuple")
        if any(
            not isinstance(observation, ProviderObservation) for observation in self.observations
        ):
            raise DomainValidationError("observations must contain ProviderObservation values")
        if any(observation.address != self.address for observation in self.observations):
            raise DomainValidationError("provider observation queried a different address")
        expected_script = _script_pubkey_for_address(self.address)
        if any(
            utxo.script_pubkey_hex != expected_script
            for observation in self.observations
            for utxo in observation.utxos
        ):
            raise DomainValidationError("UTXO script_pubkey_hex does not match address")
        observations = tuple(
            sorted(
                self.observations,
                key=lambda item: (
                    item.authority.value,
                    item.independence_group,
                    item.provider_id,
                ),
            )
        )
        if len({item.provider_id for item in observations}) != len(observations):
            raise DomainValidationError("provider observations must have unique provider ids")
        object.__setattr__(self, "observations", observations)
        agreed, reason = self._derive_agreement()
        object.__setattr__(self, "agreed_utxos", agreed)
        object.__setattr__(self, "unknown_reason", reason)
        object.__setattr__(self, "state", self._derive_state(agreed, reason))
        object.__setattr__(self, "evidence_fingerprint", self._derive_fingerprint())

    def _derive_agreement(self) -> tuple[tuple[ChainUtxo, ...], str | None]:
        successful = tuple(
            observation
            for observation in self.observations
            if observation.outcome is ProviderOutcome.OK
        )
        trusted = tuple(
            observation
            for observation in successful
            if observation.authority is ProviderAuthority.TRUSTED_NODE
        )
        if trusted:
            return self._agree(trusted, disagreement="trusted_node_disagreement")
        public = tuple(
            observation
            for observation in successful
            if observation.authority is ProviderAuthority.PUBLIC
        )
        if len({item.independence_group for item in public}) < 2:
            reason = (
                "provider_error"
                if any(item.outcome is ProviderOutcome.ERROR for item in self.observations)
                else "independent_provider_quorum_missing"
            )
            return (), reason
        return self._agree(public, disagreement="provider_utxo_disagreement")

    @staticmethod
    def _agree(
        observations: tuple[ProviderObservation, ...],
        *,
        disagreement: str,
    ) -> tuple[tuple[ChainUtxo, ...], str | None]:
        first = observations[0].utxos
        if any(observation.utxos != first for observation in observations[1:]):
            return (), disagreement
        tips = tuple(observation.tip_height for observation in observations)
        if max(tips) - min(tips) > 2:
            return (), "provider_tip_disagreement"
        return first, None

    @staticmethod
    def _derive_state(
        utxos: tuple[ChainUtxo, ...],
        unknown_reason: str | None,
    ) -> ChainState:
        if unknown_reason is not None:
            return ChainState.UNKNOWN
        if any(utxo.confirmed for utxo in utxos):
            return ChainState.FUNDED_CONFIRMED
        if utxos:
            return ChainState.FUNDED_UNCONFIRMED
        return ChainState.EMPTY

    def _derive_fingerprint(self) -> str:
        return _canonical_digest(
            {
                "target_id": self.target_id,
                "address": self.address,
                "purpose": self.purpose.value,
                "observations": [
                    {
                        "provider_id": observation.provider_id,
                        "authority": observation.authority.value,
                        "independence_group": observation.independence_group,
                        "outcome": observation.outcome.value,
                        "address": observation.address,
                        "checked_at": _utc_text(observation.checked_at),
                        "tip_height": observation.tip_height,
                        "error_code": observation.error_code,
                        "utxos": [
                            {
                                "txid": utxo.txid,
                                "vout": utxo.vout,
                                "value_sats": utxo.value_sats,
                                "script_pubkey_hex": utxo.script_pubkey_hex,
                                "confirmed": utxo.confirmed,
                            }
                            for utxo in observation.utxos
                        ],
                    }
                    for observation in self.observations
                ],
            }
        )

    @property
    def checked_at(self) -> datetime:
        return min(observation.checked_at for observation in self.observations)

    @property
    def fresh_until(self) -> datetime:
        ttl = timedelta(seconds=CHAIN_TTL_SECONDS[self.purpose])
        return min(observation.checked_at + ttl for observation in self.observations)

    @property
    def confirmed_sats(self) -> int:
        return sum(utxo.value_sats for utxo in self.agreed_utxos if utxo.confirmed)

    @property
    def unconfirmed_sats(self) -> int:
        return sum(utxo.value_sats for utxo in self.agreed_utxos if not utxo.confirmed)

    def is_fresh(self, at: datetime, *, purpose: ChainPurpose | None = None) -> bool:
        """Require every observation to be present-time and inside the fixed TTL."""

        _require_datetime(at, name="at")
        if purpose is not None:
            _require_enum(purpose, ChainPurpose, name="purpose")
            if purpose is not self.purpose:
                return False
        ttl = timedelta(seconds=CHAIN_TTL_SECONDS[self.purpose])
        return all(
            observation.checked_at <= at <= observation.checked_at + ttl
            for observation in self.observations
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class GpuDevice:
    device_id: str
    name: str
    memory_bytes: int
    compute_capability: tuple[int, int] | None = None
    multiprocessor_count: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.device_id, name="device_id", maximum=128)
        _require_text(self.name, name="name", maximum=256)
        _require_int(self.memory_bytes, name="memory_bytes", minimum=1)
        if self.compute_capability is not None:
            if type(self.compute_capability) is not tuple or len(self.compute_capability) != 2:
                raise DomainValidationError("compute_capability must be a (major, minor) tuple")
            major, minor = self.compute_capability
            _require_int(major, name="compute_capability major", minimum=1)
            _require_int(minor, name="compute_capability minor", minimum=0)
        if self.multiprocessor_count is not None:
            _require_int(
                self.multiprocessor_count,
                name="multiprocessor_count",
                minimum=1,
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class HostCapabilities:
    architecture: str
    cpu_count: int
    memory_bytes: int
    disk_free_bytes: int | None = None
    gpus: tuple[GpuDevice, ...] = ()
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.architecture, name="architecture", maximum=64)
        _require_int(self.cpu_count, name="cpu_count", minimum=1)
        _require_int(self.memory_bytes, name="memory_bytes", minimum=1)
        if self.disk_free_bytes is not None:
            _require_int(self.disk_free_bytes, name="disk_free_bytes", minimum=0)
        if type(self.gpus) is not tuple:
            raise DomainValidationError("gpus must be a tuple")
        if any(type(gpu) is not GpuDevice for gpu in self.gpus):
            raise DomainValidationError("gpus must contain GpuDevice values")
        if len({gpu.device_id for gpu in self.gpus}) != len(self.gpus):
            raise DomainValidationError("GPU device ids must be unique")
        canonical_gpus = tuple(sorted(self.gpus, key=lambda gpu: gpu.device_id))
        object.__setattr__(self, "gpus", canonical_gpus)
        object.__setattr__(self, "fingerprint", self._derive_fingerprint())

    def _derive_fingerprint(self) -> str:
        return _canonical_digest(
            {
                "architecture": self.architecture,
                "cpu_count": self.cpu_count,
                "memory_bytes": self.memory_bytes,
                "disk_free_bytes": self.disk_free_bytes,
                "gpus": [
                    {
                        "device_id": gpu.device_id,
                        "name": gpu.name,
                        "memory_bytes": gpu.memory_bytes,
                        "compute_capability": gpu.compute_capability,
                        "multiprocessor_count": gpu.multiprocessor_count,
                    }
                    for gpu in self.gpus
                ],
            }
        )

    def gpu(self, device_id: str) -> GpuDevice | None:
        _require_text(device_id, name="device_id", maximum=128)
        return next((gpu for gpu in self.gpus if gpu.device_id == device_id), None)


__all__ = [
    "CHAIN_TTL_SECONDS",
    "ChainPurpose",
    "ChainSnapshot",
    "ChainState",
    "ChainUtxo",
    "DomainValidationError",
    "EngineName",
    "GpuDevice",
    "HostCapabilities",
    "KeyRange",
    "MAX_BITCOIN_SUPPLY_SATS",
    "ProviderAuthority",
    "ProviderObservation",
    "ProviderOutcome",
    "PuzzleTarget",
    "ResourceClass",
    "TargetMode",
]
