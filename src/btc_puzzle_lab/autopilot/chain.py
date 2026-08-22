"""Provider-bound collection of public chain evidence.

This module is intentionally an adapter boundary.  Remote payloads may describe
UTXOs and a tip, but they never choose their provider identity, authority, or
independence group.  Those values come from the local registry below before a
``ChainSnapshot`` is constructed.

Fixture adapters remain available for deterministic tests.  The production
registry binds two public Esplora-compatible services to locally fixed origins
and provenance, then verifies every reported UTXO against its original
transaction before admitting it to the shared snapshot model.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

import requests

from btc_puzzle_lab.autopilot._issuance import ProcessLocalIssuance
from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshotProvenance,
    CatalogTargetBinding,
    PracticeFixtureEvidence,
    is_catalog_target_binding_issued,
    is_practice_fixture_evidence_issued,
)
from btc_puzzle_lab.autopilot.facts import (
    CHAIN_TTL_SECONDS,
    MAX_BITCOIN_SUPPLY_SATS,
    ChainPurpose,
    ChainSnapshot,
    ChainState,
    ChainUtxo,
    DomainValidationError,
    ProviderAuthority,
    ProviderObservation,
    ProviderOutcome,
    PuzzleTarget,
    TargetMode,
)

if TYPE_CHECKING:
    from btc_puzzle_lab.autopilot.catalog_ranking import CatalogFastestRankingReceipt

_MAX_RESPONSE_BYTES = 1_048_576
_MAX_UTXOS = 10_000
_MAX_TRANSACTION_LOOKUPS_PER_PROVIDER = 120
_MAX_TRANSACTION_OUTPUTS = 100_000
_MAX_TIP_HEIGHT = 10_000_000
_MAX_VOUT = 0xFFFFFFFF
# One decoded byte per iterator yield makes the monotonic deadline effective
# even against a peer that drips data just inside Requests' inactivity timeout.
_HTTP_READ_CHUNK_BYTES = 1
_DEFAULT_HTTP_TIMEOUT_SECONDS = (3.05, 10.0)
_PRODUCTION_HTTP_DEADLINE_SECONDS = 60.0
_PRODUCTION_HTTP_REQUEST_LIMIT = 256
_PRODUCTION_HTTP_TOTAL_BYTES_LIMIT = 16 * 1_048_576

# Catalog selection has one larger envelope shared by both fixed providers,
# plus a strict independent sub-envelope for each provider.  The two layers
# are charged atomically for every actual HTTP request and decoded byte.
_CATALOG_HTTP_DEADLINE_SECONDS = 120.0
_CATALOG_HTTP_REQUEST_LIMIT = 400
_CATALOG_HTTP_TOTAL_BYTES_LIMIT = 64 * 1_048_576
_CATALOG_PROVIDER_REQUEST_LIMIT = 200
_CATALOG_PROVIDER_TOTAL_BYTES_LIMIT = 32 * 1_048_576
_CATALOG_PROVIDER_MAX_UNIQUE_TRANSACTIONS = 120
_CATALOG_FASTEST_OBJECTIVE_V1 = "fastest_full_solution_eta_baseline_v1"

# Versioned anti-ancient-cache checkpoint only.  It does not establish
# minute-level freshness; the purpose-specific local observation TTL still
# applies independently after collection.
_PRODUCTION_MAINNET_MIN_TIP_V1 = 963_000


class ChainAcquisitionError(RuntimeError):
    """Trusted local acquisition configuration cannot produce chain evidence."""


class ProviderPayloadError(ValueError):
    """A provider payload is not an exact supported public schema."""


class HttpTransportError(ChainAcquisitionError):
    """A bounded production HTTP request or response failed."""


class ChainEvidenceProvenance(StrEnum):
    """Locally assigned acquisition path; never accepted from a provider."""

    CATALOG_PRACTICE_V1 = "catalog_practice_v1"
    FIXTURE_V1 = "fixture_v1"
    INJECTED_V1 = "injected_v1"
    PRODUCTION_HTTP_V1 = "production_http_v1"
    PRODUCTION_CATALOG_HTTP_V1 = "production_catalog_http_v1"


class ProviderResource(StrEnum):
    UTXOS = "utxos"
    TIP = "tip"
    TRANSACTION = "transaction"


@dataclass(frozen=True, slots=True)
class RawHttpResponse:
    """Transport result before any provider-specific interpretation."""

    status_code: int
    body: bytes

    def __post_init__(self) -> None:
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be an HTTP status integer")
        if type(self.body) is not bytes:
            raise ValueError("body must be bytes")


class ChainTransport(Protocol):
    """Injected I/O boundary; implementations own timeouts and HTTP details."""

    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None = None,
    ) -> RawHttpResponse: ...


class ProviderPayloadAdapter(Protocol):
    """Parse one locally registered provider's raw response schemas."""

    def collect_utxos(
        self,
        payload: bytes,
        *,
        tip_height: int,
        transaction_payload: Callable[[str], bytes],
    ) -> tuple[ChainUtxo, ...]: ...

    def parse_tip_height(self, payload: bytes) -> int: ...


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_json(payload: bytes) -> object:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_RESPONSE_BYTES:
        raise ProviderPayloadError("payload must be bounded non-empty bytes")
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProviderPayloadError("payload must be strict UTF-8 JSON") from exc
    return value


def _load_json_object(payload: bytes) -> dict[str, Any]:
    value = _load_json(payload)
    if type(value) is not dict:
        raise ProviderPayloadError("payload root must be an object")
    return value


def _load_json_array(payload: bytes) -> list[Any]:
    value = _load_json(payload)
    if type(value) is not list:
        raise ProviderPayloadError("payload root must be an array")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise ProviderPayloadError(f"{label} has an unsupported schema")


def _bounded_integer(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProviderPayloadError(f"{label} must be an integer in the supported range")
    return value


def _require_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProviderPayloadError(f"{label} must be bounded non-empty text")
    return value


def _require_txid(value: object, *, label: str = "txid") -> str:
    text = _require_text(value, label=label, maximum=64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ProviderPayloadError(f"{label} must be 32-byte lower-case hexadecimal")
    return text


def _make_utxo(
    *,
    txid: object,
    vout: object,
    value_sats: object,
    script_pubkey_hex: object,
    confirmed: object,
) -> ChainUtxo:
    if type(confirmed) is not bool:
        raise ProviderPayloadError("confirmed must be a boolean")
    try:
        return ChainUtxo(
            txid=_require_text(txid, label="txid", maximum=64),
            vout=_bounded_integer(vout, label="vout", minimum=0, maximum=_MAX_VOUT),
            value_sats=_bounded_integer(
                value_sats,
                label="value_sats",
                minimum=1,
                maximum=MAX_BITCOIN_SUPPLY_SATS,
            ),
            script_pubkey_hex=_require_text(
                script_pubkey_hex,
                label="script_pubkey_hex",
                maximum=20_000,
            ),
            confirmed=confirmed,
        )
    except DomainValidationError as exc:
        raise ProviderPayloadError("UTXO fields are not canonical") from exc


def _canonical_utxos(
    items: object, *, parser: Callable[[dict[str, Any]], ChainUtxo]
) -> tuple[ChainUtxo, ...]:
    if type(items) is not list or len(items) > _MAX_UTXOS:
        raise ProviderPayloadError("UTXO collection must be a bounded array")
    parsed: list[ChainUtxo] = []
    for item in items:
        if type(item) is not dict:
            raise ProviderPayloadError("each UTXO must be an object")
        parsed.append(parser(item))
    canonical = tuple(sorted(parsed, key=lambda utxo: utxo.outpoint))
    if len({utxo.outpoint for utxo in canonical}) != len(canonical):
        raise ProviderPayloadError("UTXO collection contains duplicate outpoints")
    if sum(utxo.value_sats for utxo in canonical) > MAX_BITCOIN_SUPPLY_SATS:
        raise ProviderPayloadError("UTXO collection exceeds Bitcoin's maximum supply")
    return canonical


class FixtureAlphaAdapter:
    """Strict parser for fixture schema A."""

    @staticmethod
    def parse_utxos(payload: bytes) -> tuple[ChainUtxo, ...]:
        root = _load_json_object(payload)
        _require_exact_keys(root, {"utxos"}, label="alpha UTXO response")

        def parse(item: dict[str, Any]) -> ChainUtxo:
            _require_exact_keys(
                item,
                {"confirmed", "script_pubkey_hex", "txid", "value_sats", "vout"},
                label="alpha UTXO",
            )
            return _make_utxo(
                txid=item["txid"],
                vout=item["vout"],
                value_sats=item["value_sats"],
                script_pubkey_hex=item["script_pubkey_hex"],
                confirmed=item["confirmed"],
            )

        return _canonical_utxos(root["utxos"], parser=parse)

    @staticmethod
    def collect_utxos(
        payload: bytes,
        *,
        tip_height: int,
        transaction_payload: Callable[[str], bytes],
    ) -> tuple[ChainUtxo, ...]:
        del tip_height
        del transaction_payload
        return FixtureAlphaAdapter.parse_utxos(payload)

    @staticmethod
    def parse_tip_height(payload: bytes) -> int:
        root = _load_json_object(payload)
        _require_exact_keys(root, {"tip_height"}, label="alpha tip response")
        return _bounded_integer(
            root["tip_height"],
            label="tip_height",
            minimum=0,
            maximum=_MAX_TIP_HEIGHT,
        )


class FixtureBetaAdapter:
    """Strict parser for independent fixture schema B."""

    @staticmethod
    def parse_utxos(payload: bytes) -> tuple[ChainUtxo, ...]:
        root = _load_json_object(payload)
        _require_exact_keys(root, {"outputs"}, label="beta UTXO response")

        def parse(item: dict[str, Any]) -> ChainUtxo:
            _require_exact_keys(
                item,
                {"is_confirmed", "locking_script", "output_index", "satoshis", "transaction_id"},
                label="beta UTXO",
            )
            return _make_utxo(
                txid=item["transaction_id"],
                vout=item["output_index"],
                value_sats=item["satoshis"],
                script_pubkey_hex=item["locking_script"],
                confirmed=item["is_confirmed"],
            )

        return _canonical_utxos(root["outputs"], parser=parse)

    @staticmethod
    def collect_utxos(
        payload: bytes,
        *,
        tip_height: int,
        transaction_payload: Callable[[str], bytes],
    ) -> tuple[ChainUtxo, ...]:
        del tip_height
        del transaction_payload
        return FixtureBetaAdapter.parse_utxos(payload)

    @staticmethod
    def parse_tip_height(payload: bytes) -> int:
        root = _load_json_object(payload)
        _require_exact_keys(root, {"height"}, label="beta tip response")
        return _bounded_integer(
            root["height"],
            label="height",
            minimum=0,
            maximum=_MAX_TIP_HEIGHT,
        )


@dataclass(frozen=True, slots=True)
class _EsploraStatus:
    confirmed: bool
    block_height: int | None = None
    block_hash: str | None = None
    block_time: int | None = None


@dataclass(frozen=True, slots=True)
class _EsploraUtxoClaim:
    txid: str
    vout: int
    value_sats: int
    status: _EsploraStatus


class _TransactionPayloadError(ProviderPayloadError):
    """An original transaction cannot substantiate an address UTXO claim."""


def _parse_esplora_status(value: object, *, label: str) -> _EsploraStatus:
    if type(value) is not dict:
        raise ProviderPayloadError(f"{label} must be an object")
    confirmed = value.get("confirmed")
    if type(confirmed) is not bool:
        raise ProviderPayloadError(f"{label}.confirmed must be a boolean")
    expected = (
        {"confirmed", "block_height", "block_hash", "block_time"} if confirmed else {"confirmed"}
    )
    _require_exact_keys(value, expected, label=label)
    if not confirmed:
        return _EsploraStatus(confirmed=False)
    block_height = _bounded_integer(
        value["block_height"],
        label=f"{label}.block_height",
        minimum=0,
        maximum=_MAX_TIP_HEIGHT,
    )
    block_hash = _require_text(
        value["block_hash"],
        label=f"{label}.block_hash",
        maximum=64,
    )
    if len(block_hash) != 64 or any(
        character not in "0123456789abcdef" for character in block_hash
    ):
        raise ProviderPayloadError(f"{label}.block_hash must be lower-case hexadecimal")
    block_time = _bounded_integer(
        value["block_time"],
        label=f"{label}.block_time",
        minimum=0,
        maximum=0x7FFFFFFFFFFFFFFF,
    )
    return _EsploraStatus(
        confirmed=True,
        block_height=block_height,
        block_hash=block_hash,
        block_time=block_time,
    )


def _canonical_esplora_claims(payload: bytes) -> tuple[_EsploraUtxoClaim, ...]:
    items = _load_json_array(payload)
    if len(items) > _MAX_UTXOS:
        raise ProviderPayloadError("UTXO collection must be a bounded array")
    claims: list[_EsploraUtxoClaim] = []
    for item in items:
        if type(item) is not dict:
            raise ProviderPayloadError("each UTXO must be an object")
        _require_exact_keys(item, {"status", "txid", "value", "vout"}, label="Esplora UTXO")
        claims.append(
            _EsploraUtxoClaim(
                txid=_require_txid(item["txid"]),
                vout=_bounded_integer(
                    item["vout"],
                    label="vout",
                    minimum=0,
                    maximum=_MAX_VOUT,
                ),
                value_sats=_bounded_integer(
                    item["value"],
                    label="value",
                    minimum=1,
                    maximum=MAX_BITCOIN_SUPPLY_SATS,
                ),
                status=_parse_esplora_status(item["status"], label="UTXO status"),
            )
        )
    canonical = tuple(sorted(claims, key=lambda claim: (claim.txid, claim.vout)))
    if len({(claim.txid, claim.vout) for claim in canonical}) != len(canonical):
        raise ProviderPayloadError("UTXO collection contains duplicate outpoints")
    if sum(claim.value_sats for claim in canonical) > MAX_BITCOIN_SUPPLY_SATS:
        raise ProviderPayloadError("UTXO collection exceeds Bitcoin's maximum supply")
    return canonical


def _utxo_from_esplora_transaction(payload: bytes, claim: _EsploraUtxoClaim) -> ChainUtxo:
    try:
        root = _load_json_object(payload)
        required = {"status", "txid", "vout"}
        if not required.issubset(root):
            raise ProviderPayloadError("Esplora transaction has an unsupported schema")
        if _require_txid(root["txid"], label="transaction txid") != claim.txid:
            raise ProviderPayloadError("transaction txid does not match the UTXO")
        status = _parse_esplora_status(root["status"], label="transaction status")
        if status != claim.status:
            raise ProviderPayloadError("transaction confirmation disagrees with the UTXO")
        outputs = root["vout"]
        if type(outputs) is not list or len(outputs) > _MAX_TRANSACTION_OUTPUTS:
            raise ProviderPayloadError("transaction vout must be a bounded array")
        if claim.vout >= len(outputs) or type(outputs[claim.vout]) is not dict:
            raise ProviderPayloadError("UTXO output index is absent from the transaction")
        output = outputs[claim.vout]
        if not {"scriptpubkey", "value"}.issubset(output):
            raise ProviderPayloadError("transaction output has an unsupported schema")
        transaction_value = _bounded_integer(
            output["value"],
            label="transaction output value",
            minimum=0,
            maximum=MAX_BITCOIN_SUPPLY_SATS,
        )
        if transaction_value != claim.value_sats:
            raise ProviderPayloadError("transaction output value disagrees with the UTXO")
        return _make_utxo(
            txid=claim.txid,
            vout=claim.vout,
            value_sats=claim.value_sats,
            script_pubkey_hex=output["scriptpubkey"],
            confirmed=claim.status.confirmed,
        )
    except ProviderPayloadError as exc:
        raise _TransactionPayloadError("original transaction does not substantiate UTXO") from exc


class EsploraAdapter:
    """Strict Bitcoin-mainnet parser for the documented Esplora REST schema."""

    @staticmethod
    def collect_utxos(
        payload: bytes,
        *,
        tip_height: int,
        transaction_payload: Callable[[str], bytes],
    ) -> tuple[ChainUtxo, ...]:
        claims = _canonical_esplora_claims(payload)
        if any(
            claim.status.block_height is not None and claim.status.block_height > tip_height
            for claim in claims
        ):
            raise ProviderPayloadError("confirmed UTXO height exceeds the provider tip")
        if len({claim.txid for claim in claims}) > _MAX_TRANSACTION_LOOKUPS_PER_PROVIDER:
            raise ProviderPayloadError("UTXO collection requires too many transaction lookups")
        transactions: dict[str, bytes] = {}
        materialized: list[ChainUtxo] = []
        for claim in claims:
            if claim.txid not in transactions:
                transactions[claim.txid] = transaction_payload(claim.txid)
            materialized.append(_utxo_from_esplora_transaction(transactions[claim.txid], claim))
        return tuple(materialized)

    @staticmethod
    def parse_tip_height(payload: bytes) -> int:
        if type(payload) is not bytes or not payload or len(payload) > 32:
            raise ProviderPayloadError("tip height must be bounded non-empty bytes")
        wire = payload.removesuffix(b"\n").removesuffix(b"\r")
        if not wire or not wire.isdigit():
            raise ProviderPayloadError("tip height must be canonical decimal ASCII")
        if len(wire) > 1 and wire.startswith(b"0"):
            raise ProviderPayloadError("tip height must be canonical decimal ASCII")
        return _bounded_integer(
            int(wire),
            label="tip_height",
            minimum=0,
            maximum=_MAX_TIP_HEIGHT,
        )


class FixtureProvider(StrEnum):
    ALPHA = "fixture-alpha"
    BETA = "fixture-beta"


class ProductionProvider(StrEnum):
    """Locally approved public mainnet identities; never parsed from HTTP."""

    MEMPOOL_SPACE = "mempool-space"
    BLOCKSTREAM_INFO = "blockstream-info"


@dataclass(frozen=True, slots=True)
class _RegisteredProvider:
    provider_id: str
    authority: ProviderAuthority
    independence_group: str
    adapter: ProviderPayloadAdapter


_FIXTURE_PROVIDERS = MappingProxyType(
    {
        FixtureProvider.ALPHA: _RegisteredProvider(
            provider_id="fixture-alpha",
            authority=ProviderAuthority.PUBLIC,
            independence_group="fixture-backend-alpha",
            adapter=FixtureAlphaAdapter(),
        ),
        FixtureProvider.BETA: _RegisteredProvider(
            provider_id="fixture-beta",
            authority=ProviderAuthority.PUBLIC,
            independence_group="fixture-backend-beta",
            adapter=FixtureBetaAdapter(),
        ),
    }
)

_PRODUCTION_PROVIDERS = MappingProxyType(
    {
        ProductionProvider.MEMPOOL_SPACE: _RegisteredProvider(
            provider_id="mempool-space",
            authority=ProviderAuthority.PUBLIC,
            independence_group="mempool-space",
            adapter=EsploraAdapter(),
        ),
        ProductionProvider.BLOCKSTREAM_INFO: _RegisteredProvider(
            provider_id="blockstream-info",
            authority=ProviderAuthority.PUBLIC,
            independence_group="blockstream-info",
            adapter=EsploraAdapter(),
        ),
    }
)

_PRODUCTION_HTTP_BASE_URLS = MappingProxyType(
    {
        "mempool-space": "https://mempool.space/api",
        "blockstream-info": "https://blockstream.info/api",
    }
)


def _registered_provider(
    provider_id: FixtureProvider | ProductionProvider,
) -> _RegisteredProvider:
    if type(provider_id) is FixtureProvider:
        return _FIXTURE_PROVIDERS[provider_id]
    if type(provider_id) is ProductionProvider:
        return _PRODUCTION_PROVIDERS[provider_id]
    raise KeyError(provider_id)


def _validated_http_timeout(
    value: float | tuple[float, float],
) -> float | tuple[float, float]:
    if type(value) in (int, float):
        if value <= 0:
            raise HttpTransportError("HTTP timeout must be positive")
        return float(value)
    if type(value) is not tuple or len(value) != 2:
        raise HttpTransportError("HTTP timeout must be seconds or a connect/read pair")
    connect, read = value
    if type(connect) not in (int, float) or type(read) not in (int, float):
        raise HttpTransportError("HTTP timeout values must be numeric")
    if connect <= 0 or read <= 0:
        raise HttpTransportError("HTTP timeout values must be positive")
    return float(connect), float(read)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _monotonic_seconds() -> float:
    return time.monotonic()


def _new_requests_session() -> requests.Session:
    return requests.Session()


def _read_monotonic() -> float:
    try:
        value = _monotonic_seconds()
    except Exception as exc:  # noqa: BLE001 - production clock failure must be typed
        raise HttpTransportError("production monotonic clock failed") from exc
    if type(value) not in (int, float) or not math.isfinite(value):
        raise HttpTransportError("production monotonic clock returned an invalid value")
    return float(value)


@dataclass(slots=True)
class _HttpCollectionBudget:
    deadline_at: float
    request_limit: int
    decompressed_bytes_limit: int
    request_count: int = 0
    decompressed_bytes: int = 0

    @classmethod
    def production(cls) -> _HttpCollectionBudget:
        return cls(
            deadline_at=_read_monotonic() + _PRODUCTION_HTTP_DEADLINE_SECONDS,
            request_limit=_PRODUCTION_HTTP_REQUEST_LIMIT,
            decompressed_bytes_limit=_PRODUCTION_HTTP_TOTAL_BYTES_LIMIT,
        )

    def _remaining_seconds(self) -> float:
        remaining = self.deadline_at - _read_monotonic()
        if remaining <= 0:
            raise HttpTransportError("HTTP collection deadline exceeded")
        return remaining

    def begin_request(
        self,
        configured_timeout: float | tuple[float, float],
    ) -> float | tuple[float, float]:
        timeout = self._request_timeout(configured_timeout)
        self.request_count += 1
        return timeout

    def _request_timeout(
        self,
        configured_timeout: float | tuple[float, float],
    ) -> float | tuple[float, float]:
        remaining = self._remaining_seconds()
        if self.request_count >= self.request_limit:
            raise HttpTransportError("HTTP collection request limit exceeded")
        if type(configured_timeout) is tuple:
            configured_connect, configured_read = configured_timeout
            connect = min(configured_connect, remaining / 2)
            read = min(configured_read, remaining - connect)
            if connect <= 0 or read <= 0:
                raise HttpTransportError("HTTP collection deadline exceeded")
            return connect, read
        timeout = min(configured_timeout, remaining / 2)
        if timeout <= 0:
            raise HttpTransportError("HTTP collection deadline exceeded")
        return timeout

    def check_deadline(self) -> None:
        self._remaining_seconds()

    def check_announced_bytes(self, size: int) -> None:
        self._check_bytes(size)

    def consume_bytes(self, size: int) -> None:
        self._check_bytes(size)
        self.decompressed_bytes += size

    def _check_bytes(self, size: int) -> None:
        self.check_deadline()
        if type(size) is not int or size < 0:
            raise HttpTransportError("HTTP collection byte charge must be non-negative")
        if self.decompressed_bytes + size > self.decompressed_bytes_limit:
            raise HttpTransportError("HTTP collection decompressed-byte limit exceeded")


@dataclass(slots=True)
class _LayeredHttpCollectionBudget:
    """Atomically charge one request to a shared and provider-local budget."""

    shared: _HttpCollectionBudget
    provider: _HttpCollectionBudget

    def begin_request(
        self,
        configured_timeout: float | tuple[float, float],
    ) -> float | tuple[float, float]:
        shared_timeout = self.shared._request_timeout(configured_timeout)
        provider_timeout = self.provider._request_timeout(configured_timeout)
        self.shared.request_count += 1
        self.provider.request_count += 1
        if type(shared_timeout) is tuple and type(provider_timeout) is tuple:
            return (
                min(shared_timeout[0], provider_timeout[0]),
                min(shared_timeout[1], provider_timeout[1]),
            )
        if type(shared_timeout) is float and type(provider_timeout) is float:
            return min(shared_timeout, provider_timeout)
        raise HttpTransportError("HTTP collection budget timeout types disagree")

    def check_deadline(self) -> None:
        self.shared.check_deadline()
        self.provider.check_deadline()

    def check_announced_bytes(self, size: int) -> None:
        self.shared._check_bytes(size)
        self.provider._check_bytes(size)

    def consume_bytes(self, size: int) -> None:
        self.shared._check_bytes(size)
        self.provider._check_bytes(size)
        self.shared.decompressed_bytes += size
        self.provider.decompressed_bytes += size


class HttpChainTransport:
    """Bounded read-only HTTP transport for the local production registry."""

    def __init__(
        self,
        *,
        session: object | None = None,
        request_get: Callable[..., object] | None = None,
        timeout_seconds: float | tuple[float, float] = _DEFAULT_HTTP_TIMEOUT_SECONDS,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        _collection_budget: _HttpCollectionBudget | _LayeredHttpCollectionBudget | None = None,
    ) -> None:
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise HttpTransportError(
                f"HTTP response limit must be between 1 and {_MAX_RESPONSE_BYTES} bytes"
            )
        if session is not None and request_get is not None:
            raise HttpTransportError("HTTP transport accepts a session or request_get, not both")
        if session is not None:
            session_get = getattr(session, "get", None)
            if not callable(session_get):
                raise HttpTransportError("HTTP session must provide get")
            self._request_get = session_get
        else:
            self._request_get = requests.get if request_get is None else request_get
        self._timeout_seconds = _validated_http_timeout(timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._collection_budget = _collection_budget

    @staticmethod
    def _url(
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None,
    ) -> str:
        try:
            base_url = _PRODUCTION_HTTP_BASE_URLS[provider_id]
        except (KeyError, TypeError):
            raise HttpTransportError(
                "HTTP transport requires a local production provider"
            ) from None
        if not isinstance(resource, ProviderResource):
            raise HttpTransportError("HTTP resource must be a ProviderResource")
        if not isinstance(address, str) or not address or len(address) > 90:
            raise HttpTransportError("HTTP address locator must be bounded text")
        if resource is ProviderResource.UTXOS:
            if txid is not None:
                raise HttpTransportError("UTXO request cannot carry a transaction id")
            return f"{base_url}/address/{quote(address, safe='')}/utxo"
        if resource is ProviderResource.TIP:
            if txid is not None:
                raise HttpTransportError("tip request cannot carry a transaction id")
            return f"{base_url}/blocks/tip/height"
        if resource is ProviderResource.TRANSACTION:
            try:
                transaction_id = _require_txid(txid, label="transaction txid")
            except ProviderPayloadError as exc:
                raise HttpTransportError("transaction request requires a canonical txid") from exc
            return f"{base_url}/tx/{transaction_id}"
        raise HttpTransportError("unsupported HTTP resource")

    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None = None,
    ) -> RawHttpResponse:
        url = self._url(
            provider_id=provider_id,
            resource=resource,
            address=address,
            txid=txid,
        )
        request_timeout = (
            self._collection_budget.begin_request(self._timeout_seconds)
            if self._collection_budget is not None
            else self._timeout_seconds
        )
        try:
            response = self._request_get(
                url,
                allow_redirects=False,
                headers={
                    "Accept": "application/json, text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                    "User-Agent": "btc-puzzle-lab-chain-evidence/1",
                },
                stream=True,
                timeout=request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - normalize the injected requests boundary
            if self._collection_budget is not None:
                self._collection_budget.check_deadline()
            raise HttpTransportError("HTTP GET failed") from exc
        try:
            try:
                if self._collection_budget is not None:
                    self._collection_budget.check_deadline()
                status_code = getattr(response, "status_code")
                headers = getattr(response, "headers", {})
                content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
                if isinstance(content_length, str) and content_length.isdecimal():
                    if int(content_length) > self._max_response_bytes:
                        raise HttpTransportError("HTTP response exceeds the configured byte limit")
                    if self._collection_budget is not None:
                        self._collection_budget.check_announced_bytes(int(content_length))
                body = bytearray()
                size = 0
                iterator = getattr(response, "iter_content")
                stream = iter(iterator(chunk_size=_HTTP_READ_CHUNK_BYTES))
                while True:
                    if self._collection_budget is not None:
                        self._collection_budget.check_deadline()
                    try:
                        chunk = next(stream)
                    except StopIteration:
                        if self._collection_budget is not None:
                            self._collection_budget.check_deadline()
                        break
                    if self._collection_budget is not None:
                        self._collection_budget.check_deadline()
                    if type(chunk) is not bytes:
                        raise HttpTransportError("HTTP response yielded a non-bytes chunk")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise HttpTransportError("HTTP response exceeds the configured byte limit")
                    if self._collection_budget is not None:
                        self._collection_budget.consume_bytes(len(chunk))
                    body.extend(chunk)
                return RawHttpResponse(status_code=status_code, body=bytes(body))
            except HttpTransportError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize untrusted response objects
                raise HttpTransportError("HTTP response could not be read") from exc
        finally:
            try:
                close = getattr(response, "close", None)
            except Exception:
                close = None
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


class _CatalogProviderTransport:
    """Provider-local tip/transaction cache around one isolated HTTP session."""

    def __init__(self, *, provider_id: str, transport: HttpChainTransport) -> None:
        self._provider_id = provider_id
        self._transport = transport
        self._tip: RawHttpResponse | None = None
        self._transactions: dict[str, RawHttpResponse] = {}
        self._unique_transaction_ids: set[str] = set()

    @property
    def unique_transaction_count(self) -> int:
        return len(self._unique_transaction_ids)

    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None = None,
    ) -> RawHttpResponse:
        if provider_id != self._provider_id:
            raise HttpTransportError("catalog transport cannot cross provider boundaries")
        if resource is ProviderResource.TIP:
            if txid is not None:
                raise HttpTransportError("tip request cannot carry a transaction id")
            if self._tip is None:
                self._tip = self._transport.get(
                    provider_id=provider_id,
                    resource=resource,
                    address=address,
                )
            return self._tip
        if resource is ProviderResource.TRANSACTION:
            try:
                transaction_id = _require_txid(txid, label="transaction txid")
            except ProviderPayloadError as exc:
                raise HttpTransportError("transaction request requires a canonical txid") from exc
            cached = self._transactions.get(transaction_id)
            if cached is not None:
                return cached
            if transaction_id not in self._unique_transaction_ids:
                if len(self._unique_transaction_ids) >= _CATALOG_PROVIDER_MAX_UNIQUE_TRANSACTIONS:
                    raise HttpTransportError("catalog provider unique-transaction limit exceeded")
                self._unique_transaction_ids.add(transaction_id)
            response = self._transport.get(
                provider_id=provider_id,
                resource=resource,
                address=address,
                txid=transaction_id,
            )
            self._transactions[transaction_id] = response
            return response
        return self._transport.get(
            provider_id=provider_id,
            resource=resource,
            address=address,
            txid=txid,
        )


_REGISTRY_FACTORY_TOKEN = object()
_CHAIN_RECEIPT_FACTORY_TOKEN = object()
_PRACTICE_BYPASS_FACTORY_TOKEN = object()
_PRODUCTION_COLLECTION_FACTORY_TOKEN = object()
_CATALOG_CHAIN_BATCH_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderRegistry:
    """Immutable local provenance registry for provider adapters.

    Callers select typed local identities; they cannot supply authority,
    independence metadata, adapters, or HTTP origins themselves.
    """

    _provider_ids: tuple[FixtureProvider | ProductionProvider, ...]

    def __init__(
        self,
        provider_ids: tuple[FixtureProvider | ProductionProvider, ...],
        *,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _REGISTRY_FACTORY_TOKEN:
            raise ChainAcquisitionError("provider provenance must come from a registry factory")
        object.__setattr__(self, "_provider_ids", provider_ids)

    @staticmethod
    def fixture(
        providers: tuple[FixtureProvider, ...] = (
            FixtureProvider.ALPHA,
            FixtureProvider.BETA,
        ),
    ) -> ProviderRegistry:
        if type(providers) is not tuple or not providers:
            raise ChainAcquisitionError("provider registry must not be empty")
        if any(type(provider) is not FixtureProvider for provider in providers):
            raise ChainAcquisitionError("fixture provider ids must be typed registry ids")
        if len(set(providers)) != len(providers):
            raise ChainAcquisitionError("provider registry contains duplicate ids")
        registry = ProviderRegistry(
            providers,
            _factory_token=_REGISTRY_FACTORY_TOKEN,
        )
        return _PROVIDER_REGISTRY_ISSUANCE.issue(registry)

    @staticmethod
    def production() -> ProviderRegistry:
        """Issue the fixed local two-provider public-mainnet registry."""

        providers = (
            ProductionProvider.MEMPOOL_SPACE,
            ProductionProvider.BLOCKSTREAM_INFO,
        )
        registry = ProviderRegistry(
            providers,
            _factory_token=_REGISTRY_FACTORY_TOKEN,
        )
        return _PROVIDER_REGISTRY_ISSUANCE.issue(registry)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(provider.value for provider in self._provider_ids)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ChainAdmissionReceipt:
    """Opaque proof that a snapshot came through the local provider registry."""

    target: PuzzleTarget
    snapshot: ChainSnapshot
    provenance: ChainEvidenceProvenance
    receipt_fingerprint: str

    def __init__(
        self,
        *,
        target: PuzzleTarget,
        snapshot: ChainSnapshot,
        provider_ids: tuple[str, ...],
        provenance: ChainEvidenceProvenance,
        _factory_token: object | None = None,
        _production_token: object | None = None,
    ) -> None:
        if _factory_token is not _CHAIN_RECEIPT_FACTORY_TOKEN:
            raise ChainAcquisitionError("chain receipt must come from registered collection")
        if type(target) is not PuzzleTarget or target.mode is not TargetMode.LIVE:
            raise ChainAcquisitionError("chain receipt requires the exact live target collected")
        if snapshot.target_id != target.puzzle_id or snapshot.address != target.address:
            raise ChainAcquisitionError("chain receipt snapshot does not match its target")
        if (
            type(provenance) is not ChainEvidenceProvenance
            or provenance is ChainEvidenceProvenance.CATALOG_PRACTICE_V1
        ):
            raise ChainAcquisitionError("live chain receipt requires typed live provenance")
        if (
            provenance is ChainEvidenceProvenance.PRODUCTION_HTTP_V1
            and _production_token is not _PRODUCTION_COLLECTION_FACTORY_TOKEN
        ):
            raise ChainAcquisitionError("production provenance requires sealed collection")
        if {item.provider_id for item in snapshot.observations} != set(provider_ids):
            raise ChainAcquisitionError("chain receipt providers do not match the registry")
        material = (
            "chain-admission-v3\0"
            + provenance.value
            + "\0"
            + str(target.puzzle_id)
            + "\0"
            + format(target.key_range.start, "x")
            + "\0"
            + format(target.key_range.end, "x")
            + "\0"
            + target.address
            + "\0"
            + target.mode.value
            + "\0"
            + (str(target.bits_label) if target.bits_label is not None else "")
            + "\0"
            + (target.public_key_hex or "")
            + "\0"
            + (target.practice_fixture_id or "")
            + "\0"
            + snapshot.evidence_fingerprint
            + "\0"
            + "\0".join(provider_ids)
        ).encode("ascii")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "receipt_fingerprint", hashlib.sha256(material).hexdigest())


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class PracticeLookupBypass:
    """Explicit non-chain path for a public practice fixture.

    It intentionally has no chain state, UTXOs, balance, or quorum property.
    """

    target: PuzzleTarget
    purpose: ChainPurpose
    fixture: PracticeFixtureEvidence
    provenance: ChainEvidenceProvenance
    receipt_fingerprint: str

    def __init__(
        self,
        *,
        target: PuzzleTarget,
        purpose: ChainPurpose,
        fixture: PracticeFixtureEvidence,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _PRACTICE_BYPASS_FACTORY_TOKEN:
            raise ChainAcquisitionError("practice bypass must come from registered collection")
        if type(target) is not PuzzleTarget or target.mode is not TargetMode.PRACTICE:
            raise ChainAcquisitionError("only a practice target may bypass live lookup")
        if not isinstance(purpose, ChainPurpose):
            raise ChainAcquisitionError("purpose must be a ChainPurpose")
        if (
            not is_practice_fixture_evidence_issued(fixture)
            or type(target.address) is not str
            or type(target.practice_fixture_id) is not str
            or fixture.target != target
        ):
            raise ChainAcquisitionError(
                "practice lookup requires catalog-verified evidence for this exact target"
            )
        provenance = ChainEvidenceProvenance.CATALOG_PRACTICE_V1
        material = (
            f"practice-bypass-v2\0{provenance.value}\0{purpose.value}\0"
            f"{fixture.fixture_fingerprint}"
        ).encode("ascii")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "fixture", fixture)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "receipt_fingerprint", hashlib.sha256(material).hexdigest())


class CatalogChainCandidateStatus(StrEnum):
    """Exact chain disposition for one statically ranked candidate."""

    NOT_CHECKED = "NOT_CHECKED"
    EMPTY = "EMPTY"
    FUNDED_UNCONFIRMED = "FUNDED_UNCONFIRMED"
    FUNDED_CONFIRMED = "FUNDED_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class CatalogChainBatchOutcome(StrEnum):
    """Terminal result of bounded ranked-prefix collection."""

    SELECTED = "SELECTED"
    INDETERMINATE = "INDETERMINATE"
    NO_FEASIBLE = "NO_FEASIBLE"


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogChainProviderCounts:
    """Non-sensitive resource accounting for one fixed production provider."""

    provider_id: str
    request_count: int
    decompressed_bytes: int
    unique_transaction_count: int

    def __post_init__(self) -> None:
        if self.provider_id not in {provider.value for provider in ProductionProvider}:
            raise ChainAcquisitionError("catalog counts require a fixed production provider")
        integer_values = (
            self.request_count,
            self.decompressed_bytes,
            self.unique_transaction_count,
        )
        if any(type(value) is not int or value < 0 for value in integer_values):
            raise ChainAcquisitionError("catalog provider counts must be non-negative integers")
        if self.request_count > _CATALOG_PROVIDER_REQUEST_LIMIT:
            raise ChainAcquisitionError("catalog provider request count exceeds its limit")
        if self.decompressed_bytes > _CATALOG_PROVIDER_TOTAL_BYTES_LIMIT:
            raise ChainAcquisitionError("catalog provider byte count exceeds its limit")
        if self.unique_transaction_count > _CATALOG_PROVIDER_MAX_UNIQUE_TRANSACTIONS:
            raise ChainAcquisitionError(
                "catalog provider unique-transaction count exceeds its limit"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogChainCandidateResult:
    """One ranked candidate's issued evidence or exact untouched suffix marker."""

    binding: CatalogTargetBinding
    status: CatalogChainCandidateStatus
    receipt: ChainAdmissionReceipt | None

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not CatalogTargetBinding
            or not is_catalog_target_binding_issued(self.binding)
            or self.binding.target.mode is not TargetMode.LIVE
        ):
            raise ChainAcquisitionError(
                "catalog chain result requires an unchanged live catalog binding"
            )
        if type(self.status) is not CatalogChainCandidateStatus:
            raise ChainAcquisitionError("catalog chain result requires a typed status")
        if self.status is CatalogChainCandidateStatus.NOT_CHECKED:
            if self.receipt is not None:
                raise ChainAcquisitionError("NOT_CHECKED candidate cannot carry chain evidence")
            return
        if (
            type(self.receipt) is not ChainAdmissionReceipt
            or not is_production_chain_admission_receipt_issued(self.receipt)
            or self.receipt.target is not self.binding.target
            or self.receipt.snapshot.purpose is not ChainPurpose.SELECTION
        ):
            raise ChainAcquisitionError(
                "checked catalog candidate requires exact production selection evidence"
            )
        expected_status = CatalogChainCandidateStatus(self.receipt.snapshot.state.value)
        if self.status is not expected_status:
            raise ChainAcquisitionError("catalog candidate status disagrees with its evidence")

    @property
    def puzzle_id(self) -> int:
        return self.binding.target.puzzle_id


def _catalog_chain_batch_fingerprint(
    *,
    ranking_fingerprint: str,
    catalog_fingerprint: str,
    catalog_provenance: CatalogSnapshotProvenance,
    host_fingerprint: str,
    policy_fingerprint: str,
    objective: str,
    purpose: ChainPurpose,
    outcome: CatalogChainBatchOutcome,
    candidates: tuple[CatalogChainCandidateResult, ...],
    provider_counts: tuple[CatalogChainProviderCounts, ...],
    batch_started_at: datetime,
    batch_completed_at: datetime,
) -> str:
    payload = {
        "contract_version": 1,
        "provenance": ChainEvidenceProvenance.PRODUCTION_CATALOG_HTTP_V1.value,
        "ranking_fingerprint": ranking_fingerprint,
        "catalog_fingerprint": catalog_fingerprint,
        "catalog_provenance": catalog_provenance.value,
        "host_fingerprint": host_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "objective": objective,
        "purpose": purpose.value,
        "outcome": outcome.value,
        "candidates": [
            {
                "puzzle_id": candidate.puzzle_id,
                "status": candidate.status.value,
                "receipt_fingerprint": (
                    candidate.receipt.receipt_fingerprint if candidate.receipt else None
                ),
            }
            for candidate in candidates
        ],
        "provider_counts": [
            {
                "provider_id": counts.provider_id,
                "request_count": counts.request_count,
                "decompressed_bytes": counts.decompressed_bytes,
                "unique_transaction_count": counts.unique_transaction_count,
            }
            for counts in provider_counts
        ],
        "batch_started_at": batch_started_at.astimezone(UTC).isoformat(),
        "batch_completed_at": batch_completed_at.astimezone(UTC).isoformat(),
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class CatalogChainBatchReceipt:
    """Opaque proof of one sealed, ranked-prefix production chain batch."""

    ranking: CatalogFastestRankingReceipt
    catalog_fingerprint: str
    catalog_provenance: CatalogSnapshotProvenance
    host_fingerprint: str
    policy_fingerprint: str
    ranking_fingerprint: str
    objective: str
    purpose: ChainPurpose
    provenance: ChainEvidenceProvenance
    outcome: CatalogChainBatchOutcome
    candidates: tuple[CatalogChainCandidateResult, ...]
    provider_counts: tuple[CatalogChainProviderCounts, ...]
    checked_count: int
    request_count: int
    decompressed_bytes: int
    unique_transaction_count: int
    selected_target_id: int | None
    batch_started_at: datetime
    batch_completed_at: datetime
    receipt_fingerprint: str

    def __init__(
        self,
        *,
        ranking: CatalogFastestRankingReceipt,
        outcome: CatalogChainBatchOutcome,
        candidates: tuple[CatalogChainCandidateResult, ...],
        provider_counts: tuple[CatalogChainProviderCounts, ...],
        batch_started_at: datetime,
        batch_completed_at: datetime,
        _factory_token: object | None = None,
    ) -> None:
        from btc_puzzle_lab.autopilot.catalog_ranking import (
            CatalogFastestRankingReceipt as RuntimeCatalogFastestRankingReceipt,
        )
        from btc_puzzle_lab.autopilot.catalog_ranking import (
            is_catalog_fastest_ranking_receipt_issued,
        )

        if _factory_token is not _CATALOG_CHAIN_BATCH_FACTORY_TOKEN:
            raise ChainAcquisitionError(
                "catalog chain batch receipts require sealed production collection"
            )
        if (
            type(ranking) is not RuntimeCatalogFastestRankingReceipt
            or not is_catalog_fastest_ranking_receipt_issued(ranking)
            or ranking.catalog_provenance is not CatalogSnapshotProvenance.PACKAGE_V1
            or ranking.objective != _CATALOG_FASTEST_OBJECTIVE_V1
            or ranking.purpose is not ChainPurpose.SELECTION
            or ranking.executable is not False
        ):
            raise ChainAcquisitionError("catalog chain batch requires an exact issued ranking")
        if type(outcome) is not CatalogChainBatchOutcome:
            raise ChainAcquisitionError("catalog chain batch requires a typed outcome")
        if type(candidates) is not tuple or any(
            type(candidate) is not CatalogChainCandidateResult for candidate in candidates
        ):
            raise ChainAcquisitionError("catalog chain candidates must be an exact tuple")
        ranked = ranking.algorithmically_selectable
        if len(candidates) != len(ranked) or any(
            candidate.binding is not ranked_candidate.binding
            for candidate, ranked_candidate in zip(candidates, ranked, strict=True)
        ):
            raise ChainAcquisitionError(
                "catalog chain candidates do not preserve the exact ranking order"
            )

        checked: list[CatalogChainCandidateResult] = []
        suffix_started = False
        for candidate in candidates:
            if candidate.status is CatalogChainCandidateStatus.NOT_CHECKED:
                suffix_started = True
            elif suffix_started:
                raise ChainAcquisitionError("checked catalog candidate follows NOT_CHECKED suffix")
            else:
                checked.append(candidate)
        terminal = checked[-1].status if checked else None
        if outcome is CatalogChainBatchOutcome.SELECTED:
            valid_terminal = terminal is CatalogChainCandidateStatus.FUNDED_CONFIRMED
        elif outcome is CatalogChainBatchOutcome.INDETERMINATE:
            valid_terminal = terminal is CatalogChainCandidateStatus.UNKNOWN
        else:
            valid_terminal = len(checked) == len(candidates) and all(
                candidate.status
                in {
                    CatalogChainCandidateStatus.EMPTY,
                    CatalogChainCandidateStatus.FUNDED_UNCONFIRMED,
                }
                for candidate in checked
            )
        if not valid_terminal:
            raise ChainAcquisitionError("catalog chain outcome disagrees with its checked prefix")
        if any(
            candidate.status
            in {
                CatalogChainCandidateStatus.FUNDED_CONFIRMED,
                CatalogChainCandidateStatus.UNKNOWN,
            }
            for candidate in checked[:-1]
        ):
            raise ChainAcquisitionError("catalog chain batch continued after a terminal state")

        expected_provider_ids = tuple(provider.value for provider in ProductionProvider)
        if (
            type(provider_counts) is not tuple
            or any(type(item) is not CatalogChainProviderCounts for item in provider_counts)
            or tuple(item.provider_id for item in provider_counts) != expected_provider_ids
        ):
            raise ChainAcquisitionError(
                "catalog chain counts must cover fixed providers in registry order"
            )
        if not isinstance(batch_started_at, datetime) or not isinstance(
            batch_completed_at, datetime
        ):
            raise ChainAcquisitionError("catalog chain batch times must be datetimes")
        if (
            batch_started_at.tzinfo is None
            or batch_started_at.utcoffset() is None
            or batch_completed_at.tzinfo is None
            or batch_completed_at.utcoffset() is None
            or batch_completed_at < batch_started_at
            or batch_completed_at
            > batch_started_at + timedelta(seconds=CHAIN_TTL_SECONDS[ChainPurpose.SELECTION])
        ):
            raise ChainAcquisitionError("catalog chain batch time window is invalid")
        if any(
            observation.checked_at != batch_started_at
            for candidate in checked
            for observation in candidate.receipt.snapshot.observations  # type: ignore[union-attr]
        ):
            raise ChainAcquisitionError(
                "catalog observations must use the conservative batch start time"
            )

        request_count = sum(item.request_count for item in provider_counts)
        decompressed_bytes = sum(item.decompressed_bytes for item in provider_counts)
        unique_transaction_count = sum(item.unique_transaction_count for item in provider_counts)
        if request_count > _CATALOG_HTTP_REQUEST_LIMIT:
            raise ChainAcquisitionError("catalog batch request count exceeds its limit")
        if decompressed_bytes > _CATALOG_HTTP_TOTAL_BYTES_LIMIT:
            raise ChainAcquisitionError("catalog batch byte count exceeds its limit")
        selected_target_id = (
            checked[-1].puzzle_id if outcome is CatalogChainBatchOutcome.SELECTED else None
        )
        receipt_fingerprint = _catalog_chain_batch_fingerprint(
            ranking_fingerprint=ranking.ranking_fingerprint,
            catalog_fingerprint=ranking.catalog_fingerprint,
            catalog_provenance=ranking.catalog_provenance,
            host_fingerprint=ranking.host_fingerprint,
            policy_fingerprint=ranking.policy_fingerprint,
            objective=ranking.objective,
            purpose=ranking.purpose,
            outcome=outcome,
            candidates=candidates,
            provider_counts=provider_counts,
            batch_started_at=batch_started_at,
            batch_completed_at=batch_completed_at,
        )
        object.__setattr__(self, "ranking", ranking)
        object.__setattr__(self, "catalog_fingerprint", ranking.catalog_fingerprint)
        object.__setattr__(self, "catalog_provenance", ranking.catalog_provenance)
        object.__setattr__(self, "host_fingerprint", ranking.host_fingerprint)
        object.__setattr__(self, "policy_fingerprint", ranking.policy_fingerprint)
        object.__setattr__(self, "ranking_fingerprint", ranking.ranking_fingerprint)
        object.__setattr__(self, "objective", ranking.objective)
        object.__setattr__(self, "purpose", ranking.purpose)
        object.__setattr__(
            self,
            "provenance",
            ChainEvidenceProvenance.PRODUCTION_CATALOG_HTTP_V1,
        )
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "provider_counts", provider_counts)
        object.__setattr__(self, "checked_count", len(checked))
        object.__setattr__(self, "request_count", request_count)
        object.__setattr__(self, "decompressed_bytes", decompressed_bytes)
        object.__setattr__(self, "unique_transaction_count", unique_transaction_count)
        object.__setattr__(self, "selected_target_id", selected_target_id)
        object.__setattr__(self, "batch_started_at", batch_started_at)
        object.__setattr__(self, "batch_completed_at", batch_completed_at)
        object.__setattr__(self, "receipt_fingerprint", receipt_fingerprint)

    @property
    def prefix_receipts(self) -> tuple[ChainAdmissionReceipt, ...]:
        return tuple(
            candidate.receipt
            for candidate in self.candidates[: self.checked_count]
            if candidate.receipt is not None
        )

    @property
    def selected_receipt(self) -> ChainAdmissionReceipt | None:
        return (
            self.prefix_receipts[-1] if self.outcome is CatalogChainBatchOutcome.SELECTED else None
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("CatalogChainBatchReceipt is final and cannot be subclassed")


_PROVIDER_REGISTRY_ISSUANCE = ProcessLocalIssuance(ProviderRegistry)
_CHAIN_ADMISSION_RECEIPT_ISSUANCE = ProcessLocalIssuance(ChainAdmissionReceipt)
_PRACTICE_LOOKUP_BYPASS_ISSUANCE = ProcessLocalIssuance(PracticeLookupBypass)
_CATALOG_CHAIN_BATCH_RECEIPT_ISSUANCE = ProcessLocalIssuance(CatalogChainBatchReceipt)


def is_provider_registry_issued(value: object) -> bool:
    """Return whether this exact registry was issued here and remains unchanged."""

    return _PROVIDER_REGISTRY_ISSUANCE.is_valid(value)


def is_chain_admission_receipt_issued(value: object) -> bool:
    """Return whether this exact live receipt was issued here and remains unchanged."""

    return _CHAIN_ADMISSION_RECEIPT_ISSUANCE.is_valid(value)


def is_production_chain_admission_receipt_issued(value: object) -> bool:
    """Require an unchanged exact receipt from the sealed production HTTP path."""

    return (
        is_chain_admission_receipt_issued(value)
        and type(value) is ChainAdmissionReceipt
        and value.provenance is ChainEvidenceProvenance.PRODUCTION_HTTP_V1
    )


def is_practice_lookup_bypass_issued(value: object) -> bool:
    """Return whether this exact practice bypass was issued here and remains unchanged."""

    return _PRACTICE_LOOKUP_BYPASS_ISSUANCE.is_valid(value)


def is_catalog_chain_batch_receipt_issued(value: object) -> bool:
    """Require an unchanged exact receipt and all of its nested authorities."""

    from btc_puzzle_lab.autopilot.catalog_ranking import (
        is_catalog_fastest_ranking_receipt_issued,
    )

    return (
        _CATALOG_CHAIN_BATCH_RECEIPT_ISSUANCE.is_valid(value)
        and type(value) is CatalogChainBatchReceipt
        and is_catalog_fastest_ranking_receipt_issued(value.ranking)
        and all(
            is_catalog_target_binding_issued(candidate.binding)
            and (
                candidate.status is CatalogChainCandidateStatus.NOT_CHECKED
                or is_production_chain_admission_receipt_issued(candidate.receipt)
            )
            for candidate in value.candidates
        )
    )


type ChainEvidence = ChainAdmissionReceipt | PracticeLookupBypass
type Clock = Callable[[], datetime]


class _ProviderFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_clock(clock: Clock) -> datetime:
    try:
        value = clock()
    except Exception as exc:  # noqa: BLE001 - a broken trusted clock must fail closed
        raise ChainAcquisitionError("chain evidence clock failed") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ChainAcquisitionError("chain evidence clock must return an aware datetime")
    return value


def _response_payload(
    transport: ChainTransport,
    *,
    provider_id: str,
    resource: ProviderResource,
    address: str,
    txid: str | None = None,
) -> bytes:
    try:
        if txid is None:
            response = transport.get(
                provider_id=provider_id,
                resource=resource,
                address=address,
            )
        else:
            response = transport.get(
                provider_id=provider_id,
                resource=resource,
                address=address,
                txid=txid,
            )
    except Exception as exc:  # noqa: BLE001 - remote transport failures are evidence failures
        raise _ProviderFailure(f"{resource.value}_transport_error") from exc
    if not isinstance(response, RawHttpResponse):
        raise _ProviderFailure(f"{resource.value}_invalid_response")
    if response.status_code != 200:
        raise _ProviderFailure(f"{resource.value}_http_{response.status_code}")
    if not response.body or len(response.body) > _MAX_RESPONSE_BYTES:
        raise _ProviderFailure(f"{resource.value}_invalid_payload")
    return response.body


def _collect_observation(
    provider: _RegisteredProvider,
    *,
    address: str,
    transport: ChainTransport,
    clock: Clock,
    minimum_tip_height: int | None = None,
) -> ProviderObservation:
    # Timestamp before either resource request.  A slow or stuck provider must
    # age the evidence rather than stamping old UTXOs as fresh after it returns.
    checked_at = _read_clock(clock)
    try:
        utxo_payload = _response_payload(
            transport,
            provider_id=provider.provider_id,
            resource=ProviderResource.UTXOS,
            address=address,
        )
        tip_payload = _response_payload(
            transport,
            provider_id=provider.provider_id,
            resource=ProviderResource.TIP,
            address=address,
        )
        try:
            tip_height = provider.adapter.parse_tip_height(tip_payload)
        except Exception as exc:  # noqa: BLE001 - adapter output is untrusted evidence
            raise _ProviderFailure("tip_invalid_payload") from exc
        if minimum_tip_height is not None and tip_height < minimum_tip_height:
            raise _ProviderFailure("tip_below_mainnet_checkpoint_v1")
        try:
            utxos = provider.adapter.collect_utxos(
                utxo_payload,
                tip_height=tip_height,
                transaction_payload=lambda txid: _response_payload(
                    transport,
                    provider_id=provider.provider_id,
                    resource=ProviderResource.TRANSACTION,
                    address=address,
                    txid=txid,
                ),
            )
        except _ProviderFailure:
            raise
        except _TransactionPayloadError as exc:
            raise _ProviderFailure("transaction_invalid_payload") from exc
        except Exception as exc:  # noqa: BLE001 - adapter output is untrusted evidence
            raise _ProviderFailure("utxos_invalid_payload") from exc
    except _ProviderFailure as failure:
        return ProviderObservation(
            provider_id=provider.provider_id,
            authority=provider.authority,
            independence_group=provider.independence_group,
            outcome=ProviderOutcome.ERROR,
            address=address,
            checked_at=checked_at,
            error_code=failure.code,
        )
    return ProviderObservation(
        provider_id=provider.provider_id,
        authority=provider.authority,
        independence_group=provider.independence_group,
        outcome=ProviderOutcome.OK,
        address=address,
        checked_at=checked_at,
        tip_height=tip_height,
        utxos=utxos,
    )


def _apply_local_freshness(
    observation: ProviderObservation,
    *,
    evaluated_at: datetime,
    purpose: ChainPurpose,
) -> ProviderObservation:
    error_code: str | None = None
    if observation.checked_at > evaluated_at:
        error_code = "future_observation"
    elif evaluated_at > observation.checked_at + timedelta(seconds=CHAIN_TTL_SECONDS[purpose]):
        error_code = "stale_observation"
    if error_code is None:
        return observation
    return ProviderObservation(
        provider_id=observation.provider_id,
        authority=observation.authority,
        independence_group=observation.independence_group,
        outcome=ProviderOutcome.ERROR,
        address=observation.address,
        checked_at=observation.checked_at,
        error_code=error_code,
    )


def _apply_target_binding(
    observation: ProviderObservation,
    *,
    target: PuzzleTarget,
    purpose: ChainPurpose,
) -> ProviderObservation:
    if observation.outcome is ProviderOutcome.ERROR:
        return observation
    try:
        ChainSnapshot(
            target_id=target.puzzle_id,
            address=target.address,
            purpose=purpose,
            observations=(observation,),
        )
    except DomainValidationError:
        return ProviderObservation(
            provider_id=observation.provider_id,
            authority=observation.authority,
            independence_group=observation.independence_group,
            outcome=ProviderOutcome.ERROR,
            address=observation.address,
            checked_at=observation.checked_at,
            error_code="utxo_address_mismatch",
        )
    return observation


def _validate_collection_request(
    *,
    target: PuzzleTarget,
    purpose: ChainPurpose,
    registry: ProviderRegistry,
    practice_fixture: PracticeFixtureEvidence | None,
) -> PracticeLookupBypass | None:
    if type(target) is not PuzzleTarget:
        raise ChainAcquisitionError("target must be a PuzzleTarget")
    if not isinstance(purpose, ChainPurpose):
        raise ChainAcquisitionError("purpose must be a ChainPurpose")
    if not is_provider_registry_issued(registry):
        raise ChainAcquisitionError("registry must be an issued, unchanged ProviderRegistry")
    if target.mode is TargetMode.PRACTICE:
        if not is_practice_fixture_evidence_issued(practice_fixture):
            raise ChainAcquisitionError(
                "practice lookup requires catalog-verified evidence for this exact target"
            )
        bypass = PracticeLookupBypass(
            target=target,
            purpose=purpose,
            fixture=practice_fixture,
            _factory_token=_PRACTICE_BYPASS_FACTORY_TOKEN,
        )
        return _PRACTICE_LOOKUP_BYPASS_ISSUANCE.issue(bypass)
    if practice_fixture is not None:
        raise ChainAcquisitionError("live target cannot carry practice fixture evidence")
    return None


def _collect_live_registered_evidence(
    *,
    target: PuzzleTarget,
    purpose: ChainPurpose,
    registry: ProviderRegistry,
    transport: ChainTransport,
    clock: Clock,
    provenance: ChainEvidenceProvenance,
    minimum_tip_height: int | None = None,
    _production_token: object | None = None,
) -> ChainAdmissionReceipt:
    if (
        type(provenance) is not ChainEvidenceProvenance
        or provenance is ChainEvidenceProvenance.CATALOG_PRACTICE_V1
    ):
        raise ChainAcquisitionError("live collection requires typed live provenance")
    if (
        provenance is ChainEvidenceProvenance.PRODUCTION_HTTP_V1
        and _production_token is not _PRODUCTION_COLLECTION_FACTORY_TOKEN
    ):
        raise ChainAcquisitionError("production provenance requires sealed collection")

    try:
        providers = tuple(
            _registered_provider(provider_id) for provider_id in registry._provider_ids
        )
    except (KeyError, TypeError):
        raise ChainAcquisitionError("provider registry contains invalid local identities") from None

    observations = tuple(
        _collect_observation(
            provider,
            address=target.address,
            transport=transport,
            clock=clock,
            minimum_tip_height=minimum_tip_height,
        )
        for provider in providers
    )
    evaluated_at = _read_clock(clock)
    observations = tuple(
        _apply_target_binding(
            observation,
            target=target,
            purpose=purpose,
        )
        for observation in observations
    )
    observations = tuple(
        _apply_local_freshness(
            observation,
            evaluated_at=evaluated_at,
            purpose=purpose,
        )
        for observation in observations
    )
    snapshot = ChainSnapshot(
        target_id=target.puzzle_id,
        address=target.address,
        purpose=purpose,
        observations=observations,
    )
    receipt = ChainAdmissionReceipt(
        target=target,
        snapshot=snapshot,
        provider_ids=registry.provider_ids,
        provenance=provenance,
        _factory_token=_CHAIN_RECEIPT_FACTORY_TOKEN,
        _production_token=_production_token,
    )
    return _CHAIN_ADMISSION_RECEIPT_ISSUANCE.issue(receipt)


def collect_chain_evidence(
    *,
    target: PuzzleTarget,
    purpose: ChainPurpose,
    registry: ProviderRegistry,
    transport: ChainTransport,
    clock: Clock,
    practice_fixture: PracticeFixtureEvidence | None = None,
) -> ChainEvidence:
    """Collect fixture/injected evidence; never issue production provenance."""

    practice = _validate_collection_request(
        target=target,
        purpose=purpose,
        registry=registry,
        practice_fixture=practice_fixture,
    )
    if practice is not None:
        return practice
    provenance = (
        ChainEvidenceProvenance.FIXTURE_V1
        if all(type(provider_id) is FixtureProvider for provider_id in registry._provider_ids)
        else ChainEvidenceProvenance.INJECTED_V1
    )
    return _collect_live_registered_evidence(
        target=target,
        purpose=purpose,
        registry=registry,
        transport=transport,
        clock=clock,
        provenance=provenance,
    )


def collect_production_chain_evidence(
    *,
    target: PuzzleTarget,
    purpose: ChainPurpose,
    practice_fixture: PracticeFixtureEvidence | None = None,
) -> ChainEvidence:
    """Collect through the sealed, bounded public-mainnet HTTP path.

    No caller-supplied transport, wall clock, monotonic clock, session, or
    resource budget is accepted.  Tests replace the private module factories;
    ordinary injected collection remains explicitly non-production provenance.
    """

    registry = ProviderRegistry.production()
    practice = _validate_collection_request(
        target=target,
        purpose=purpose,
        registry=registry,
        practice_fixture=practice_fixture,
    )
    if practice is not None:
        return practice

    budget = _HttpCollectionBudget.production()
    try:
        session = _new_requests_session()
    except Exception as exc:  # noqa: BLE001 - normalize production session creation
        raise HttpTransportError("production HTTP session creation failed") from exc
    try:
        try:
            session.trust_env = False
            if session.trust_env is not False:
                raise AttributeError("trust_env was not disabled")
            if not callable(getattr(session, "close", None)):
                raise AttributeError("session has no close method")
            transport = HttpChainTransport(
                session=session,
                _collection_budget=budget,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on session isolation
            raise HttpTransportError("production HTTP session isolation failed") from exc
        return _collect_live_registered_evidence(
            target=target,
            purpose=purpose,
            registry=registry,
            transport=transport,
            clock=_utc_now,
            provenance=ChainEvidenceProvenance.PRODUCTION_HTTP_V1,
            minimum_tip_height=_PRODUCTION_MAINNET_MIN_TIP_V1,
            _production_token=_PRODUCTION_COLLECTION_FACTORY_TOKEN,
        )
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - collection did not close cleanly
                raise HttpTransportError("production HTTP session close failed") from exc


@dataclass(slots=True)
class _CatalogProviderRuntime:
    provider: _RegisteredProvider
    session: object
    budget: _HttpCollectionBudget
    transport: _CatalogProviderTransport


def _close_catalog_sessions(sessions: list[object]) -> None:
    failures: list[Exception] = []
    closed_ids: set[int] = set()
    for session in sessions:
        if id(session) in closed_ids:
            continue
        closed_ids.add(id(session))
        try:
            close = getattr(session, "close", None)
        except Exception as exc:  # noqa: BLE001 - continue closing the other provider
            failures.append(exc)
            continue
        if not callable(close):
            continue
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - all sessions still need a close attempt
            failures.append(exc)
    if failures:
        raise HttpTransportError("catalog production HTTP session close failed") from failures[0]


def _empty_catalog_provider_counts() -> tuple[CatalogChainProviderCounts, ...]:
    return tuple(
        CatalogChainProviderCounts(
            provider_id=provider.value,
            request_count=0,
            decompressed_bytes=0,
            unique_transaction_count=0,
        )
        for provider in ProductionProvider
    )


def _catalog_provider_counts(
    runtimes: tuple[_CatalogProviderRuntime, ...],
) -> tuple[CatalogChainProviderCounts, ...]:
    return tuple(
        CatalogChainProviderCounts(
            provider_id=runtime.provider.provider_id,
            request_count=runtime.budget.request_count,
            decompressed_bytes=runtime.budget.decompressed_bytes,
            unique_transaction_count=runtime.transport.unique_transaction_count,
        )
        for runtime in runtimes
    )


def _issue_catalog_chain_receipt(
    *,
    binding: CatalogTargetBinding,
    observations: tuple[ProviderObservation, ...],
    completed_at: datetime,
    provider_ids: tuple[str, ...],
) -> ChainAdmissionReceipt:
    target = binding.target
    refreshed = tuple(
        _apply_local_freshness(
            observation,
            evaluated_at=completed_at,
            purpose=ChainPurpose.SELECTION,
        )
        for observation in observations
    )
    snapshot = ChainSnapshot(
        target_id=target.puzzle_id,
        address=target.address,
        purpose=ChainPurpose.SELECTION,
        observations=refreshed,
    )
    receipt = ChainAdmissionReceipt(
        target=target,
        snapshot=snapshot,
        provider_ids=provider_ids,
        provenance=ChainEvidenceProvenance.PRODUCTION_HTTP_V1,
        _factory_token=_CHAIN_RECEIPT_FACTORY_TOKEN,
        _production_token=_PRODUCTION_COLLECTION_FACTORY_TOKEN,
    )
    return _CHAIN_ADMISSION_RECEIPT_ISSUANCE.issue(receipt)


def _issue_catalog_batch(
    *,
    ranking: CatalogFastestRankingReceipt,
    raw_prefix: tuple[tuple[CatalogTargetBinding, tuple[ProviderObservation, ...]], ...],
    provider_counts: tuple[CatalogChainProviderCounts, ...],
    batch_started_at: datetime,
    batch_completed_at: datetime,
) -> CatalogChainBatchReceipt:
    provider_ids = tuple(provider.value for provider in ProductionProvider)
    checked: list[CatalogChainCandidateResult] = []
    for binding, observations in raw_prefix:
        receipt = _issue_catalog_chain_receipt(
            binding=binding,
            observations=observations,
            completed_at=batch_completed_at,
            provider_ids=provider_ids,
        )
        checked.append(
            CatalogChainCandidateResult(
                binding=binding,
                status=CatalogChainCandidateStatus(receipt.snapshot.state.value),
                receipt=receipt,
            )
        )
    checked_count = len(checked)
    suffix = tuple(
        CatalogChainCandidateResult(
            binding=candidate.binding,
            status=CatalogChainCandidateStatus.NOT_CHECKED,
            receipt=None,
        )
        for candidate in ranking.algorithmically_selectable[checked_count:]
    )
    candidates = tuple(checked) + suffix
    terminal = checked[-1].status if checked else None
    if terminal is CatalogChainCandidateStatus.FUNDED_CONFIRMED:
        outcome = CatalogChainBatchOutcome.SELECTED
    elif terminal is CatalogChainCandidateStatus.UNKNOWN:
        outcome = CatalogChainBatchOutcome.INDETERMINATE
    else:
        outcome = CatalogChainBatchOutcome.NO_FEASIBLE
    receipt = CatalogChainBatchReceipt(
        ranking=ranking,
        outcome=outcome,
        candidates=candidates,
        provider_counts=provider_counts,
        batch_started_at=batch_started_at,
        batch_completed_at=batch_completed_at,
        _factory_token=_CATALOG_CHAIN_BATCH_FACTORY_TOKEN,
    )
    return _CATALOG_CHAIN_BATCH_RECEIPT_ISSUANCE.issue(receipt)


def collect_production_catalog_prefix(
    ranking: CatalogFastestRankingReceipt,
) -> CatalogChainBatchReceipt:
    """Check only the issued static ranking's bounded continuous live prefix.

    This production entry accepts no caller transport, session, clock, budget,
    provider, purpose, or target.  The first confirmed funded candidate is
    selected; the first unknown candidate makes the batch indeterminate; empty
    and unconfirmed candidates allow the exact ranked prefix to continue.
    """

    from btc_puzzle_lab.autopilot.catalog_ranking import (
        CatalogFastestRankingReceipt as RuntimeCatalogFastestRankingReceipt,
    )
    from btc_puzzle_lab.autopilot.catalog_ranking import (
        is_catalog_fastest_ranking_receipt_issued,
    )

    if (
        type(ranking) is not RuntimeCatalogFastestRankingReceipt
        or not is_catalog_fastest_ranking_receipt_issued(ranking)
        or ranking.catalog_provenance is not CatalogSnapshotProvenance.PACKAGE_V1
        or ranking.objective != _CATALOG_FASTEST_OBJECTIVE_V1
        or ranking.purpose is not ChainPurpose.SELECTION
        or ranking.executable is not False
    ):
        raise ChainAcquisitionError(
            "catalog production collection requires an exact issued fastest ranking"
        )
    if any(
        not is_catalog_target_binding_issued(candidate.binding)
        or candidate.binding.catalog_fingerprint != ranking.catalog_fingerprint
        or candidate.binding.target.mode is not TargetMode.LIVE
        for candidate in ranking.algorithmically_selectable
    ):
        raise ChainAcquisitionError("catalog ranking contains an invalid live binding")

    batch_started_at = _read_clock(_utc_now)
    if not ranking.algorithmically_selectable:
        batch_completed_at = _read_clock(_utc_now)
        return _issue_catalog_batch(
            ranking=ranking,
            raw_prefix=(),
            provider_counts=_empty_catalog_provider_counts(),
            batch_started_at=batch_started_at,
            batch_completed_at=batch_completed_at,
        )

    monotonic_started_at = _read_monotonic()
    deadline_at = monotonic_started_at + _CATALOG_HTTP_DEADLINE_SECONDS
    shared_budget = _HttpCollectionBudget(
        deadline_at=deadline_at,
        request_limit=_CATALOG_HTTP_REQUEST_LIMIT,
        decompressed_bytes_limit=_CATALOG_HTTP_TOTAL_BYTES_LIMIT,
    )
    sessions: list[object] = []
    runtimes: list[_CatalogProviderRuntime] = []
    raw_prefix: list[tuple[CatalogTargetBinding, tuple[ProviderObservation, ...]]] = []
    terminal_state: ChainState | None = None
    try:
        for provider_id in (
            ProductionProvider.MEMPOOL_SPACE,
            ProductionProvider.BLOCKSTREAM_INFO,
        ):
            provider = _registered_provider(provider_id)
            try:
                session = _new_requests_session()
            except Exception as exc:  # noqa: BLE001 - normalize sealed factory failure
                raise HttpTransportError("catalog production HTTP session creation failed") from exc
            sessions.append(session)
            if any(session is prior for prior in sessions[:-1]):
                raise HttpTransportError(
                    "catalog production providers require distinct HTTP sessions"
                )
            try:
                session.trust_env = False
                if session.trust_env is not False:
                    raise AttributeError("trust_env was not disabled")
                if not callable(getattr(session, "close", None)):
                    raise AttributeError("session has no close method")
                provider_budget = _HttpCollectionBudget(
                    deadline_at=deadline_at,
                    request_limit=_CATALOG_PROVIDER_REQUEST_LIMIT,
                    decompressed_bytes_limit=_CATALOG_PROVIDER_TOTAL_BYTES_LIMIT,
                )
                http_transport = HttpChainTransport(
                    session=session,
                    _collection_budget=_LayeredHttpCollectionBudget(
                        shared=shared_budget,
                        provider=provider_budget,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - fail closed on provider isolation
                raise HttpTransportError(
                    "catalog production HTTP session isolation failed"
                ) from exc
            runtimes.append(
                _CatalogProviderRuntime(
                    provider=provider,
                    session=session,
                    budget=provider_budget,
                    transport=_CatalogProviderTransport(
                        provider_id=provider.provider_id,
                        transport=http_transport,
                    ),
                )
            )

        for candidate in ranking.algorithmically_selectable:
            binding = candidate.binding
            target = binding.target
            observations = tuple(
                _apply_target_binding(
                    _collect_observation(
                        runtime.provider,
                        address=target.address,
                        transport=runtime.transport,
                        clock=lambda: batch_started_at,
                        minimum_tip_height=_PRODUCTION_MAINNET_MIN_TIP_V1,
                    ),
                    target=target,
                    purpose=ChainPurpose.SELECTION,
                )
                for runtime in runtimes
            )
            snapshot = ChainSnapshot(
                target_id=target.puzzle_id,
                address=target.address,
                purpose=ChainPurpose.SELECTION,
                observations=observations,
            )
            raw_prefix.append((binding, observations))
            terminal_state = snapshot.state
            try:
                shared_budget.check_deadline()
            except HttpTransportError:
                if snapshot.state is not ChainState.UNKNOWN:
                    raise
            if snapshot.state in {
                ChainState.FUNDED_CONFIRMED,
                ChainState.UNKNOWN,
            }:
                break
    finally:
        _close_catalog_sessions(sessions)

    try:
        shared_budget.check_deadline()
    except HttpTransportError:
        if terminal_state is not ChainState.UNKNOWN:
            raise

    batch_completed_at = _read_clock(_utc_now)
    if batch_completed_at < batch_started_at:
        raise ChainAcquisitionError("catalog chain clock moved backwards during collection")
    if batch_completed_at > batch_started_at + timedelta(
        seconds=CHAIN_TTL_SECONDS[ChainPurpose.SELECTION]
    ):
        raise ChainAcquisitionError("catalog chain batch exceeded selection freshness")
    return _issue_catalog_batch(
        ranking=ranking,
        raw_prefix=tuple(raw_prefix),
        provider_counts=_catalog_provider_counts(tuple(runtimes)),
        batch_started_at=batch_started_at,
        batch_completed_at=batch_completed_at,
    )


__all__ = [
    "ChainAcquisitionError",
    "ChainAdmissionReceipt",
    "ChainEvidence",
    "ChainEvidenceProvenance",
    "ChainTransport",
    "CatalogChainBatchOutcome",
    "CatalogChainBatchReceipt",
    "CatalogChainCandidateResult",
    "CatalogChainCandidateStatus",
    "CatalogChainProviderCounts",
    "EsploraAdapter",
    "FixtureAlphaAdapter",
    "FixtureBetaAdapter",
    "FixtureProvider",
    "HttpChainTransport",
    "HttpTransportError",
    "is_chain_admission_receipt_issued",
    "is_catalog_chain_batch_receipt_issued",
    "is_practice_lookup_bypass_issued",
    "is_production_chain_admission_receipt_issued",
    "is_provider_registry_issued",
    "PracticeLookupBypass",
    "ProviderPayloadAdapter",
    "ProviderPayloadError",
    "ProviderRegistry",
    "ProviderResource",
    "ProductionProvider",
    "RawHttpResponse",
    "collect_chain_evidence",
    "collect_production_catalog_prefix",
    "collect_production_chain_evidence",
]
