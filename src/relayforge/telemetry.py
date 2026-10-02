from __future__ import annotations

import json
import math
import threading
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .production_models import HistogramSnapshot
from .redaction import SENSITIVE_HEADERS, redact_url, sanitize_headers
from .timeutil import to_iso


@dataclass(frozen=True, slots=True)
class MetricKey:
    name: str
    labels: tuple[tuple[str, str], ...]


class BoundedMetrics:
    """In-process metrics with declared label names and cardinality bounds."""

    def __init__(self, *, max_series: int = 500) -> None:
        if max_series <= 0:
            raise ValueError("max_series must be positive")
        self.max_series = max_series
        self._definitions: dict[str, tuple[str, ...]] = {}
        self._counters: Counter[MetricKey] = Counter()
        self._histograms: dict[MetricKey, _Histogram] = {}
        self._lock = threading.Lock()

    def declare(self, name: str, *, labels: Iterable[str] = ()) -> None:
        normalized = _metric_name(name)
        label_names = tuple(labels)
        if len(set(label_names)) != len(label_names):
            raise ValueError("metric label names must be unique")
        with self._lock:
            existing = self._definitions.get(normalized)
            if existing is not None and existing != label_names:
                raise ValueError("metric was declared with different labels")
            self._definitions[normalized] = label_names

    def increment(
        self,
        name: str,
        amount: int = 1,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if amount < 0:
            raise ValueError("counter amount cannot be negative")
        key = self._key(name, labels)
        with self._lock:
            self._ensure_capacity(key)
            self._counters[key] += amount

    def observe(
        self,
        name: str,
        value: float,
        *,
        buckets: Iterable[float],
        labels: Mapping[str, str] | None = None,
    ) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("histogram observation must be finite and non-negative")
        boundaries = tuple(sorted(set(float(item) for item in buckets)))
        if not boundaries or any(not math.isfinite(item) or item <= 0 for item in boundaries):
            raise ValueError("histogram buckets must be finite and positive")
        key = self._key(name, labels)
        with self._lock:
            self._ensure_capacity(key)
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = _Histogram(boundaries)
                self._histograms[key] = histogram
            elif histogram.boundaries != boundaries:
                raise ValueError("histogram buckets cannot change after first use")
            histogram.observe(value)

    def counter_value(
        self, name: str, *, labels: Mapping[str, str] | None = None
    ) -> int:
        key = self._key(name, labels)
        with self._lock:
            return self._counters[key]

    def histogram(
        self, name: str, *, labels: Mapping[str, str] | None = None
    ) -> HistogramSnapshot:
        key = self._key(name, labels)
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                return HistogramSnapshot(0, 0.0, None, None, ())
            return histogram.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counters = [
                {
                    "name": key.name,
                    "labels": dict(key.labels),
                    "value": value,
                }
                for key, value in sorted(
                    self._counters.items(), key=lambda item: (item[0].name, item[0].labels)
                )
            ]
            histograms = [
                {
                    "name": key.name,
                    "labels": dict(key.labels),
                    "value": histogram.snapshot(),
                }
                for key, histogram in sorted(
                    self._histograms.items(), key=lambda item: (item[0].name, item[0].labels)
                )
            ]
        return {"counters": counters, "histograms": histograms}

    def _key(
        self, name: str, labels: Mapping[str, str] | None
    ) -> MetricKey:
        normalized = _metric_name(name)
        expected = self._definitions.get(normalized)
        if expected is None:
            raise ValueError(f"metric {normalized} was not declared")
        supplied = dict(labels or {})
        if set(supplied) != set(expected):
            raise ValueError(f"metric {normalized} requires labels {expected}")
        pairs = tuple((label, _label_value(supplied[label])) for label in expected)
        return MetricKey(normalized, pairs)

    def _ensure_capacity(self, key: MetricKey) -> None:
        known = key in self._counters or key in self._histograms
        if not known and len(set(self._counters) | set(self._histograms)) > self.max_series:
            raise ValueError("metric series cardinality limit reached")


class _Histogram:
    def __init__(self, boundaries: tuple[float, ...]) -> None:
        self.boundaries = boundaries
        self.count = 0
        self.total = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.counts = [0 for _ in boundaries]

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        for index, boundary in enumerate(self.boundaries):
            if value <= boundary:
                self.counts[index] += 1

    def snapshot(self) -> HistogramSnapshot:
        return HistogramSnapshot(
            self.count,
            round(self.total, 6),
            self.minimum,
            self.maximum,
            tuple(zip(self.boundaries, self.counts, strict=True)),
        )


class StructuredLogger:
    SENSITIVE_FIELDS = frozenset(
        {
            "authorization",
            "token",
            "secret",
            "password",
            "cookie",
            "payload",
            "body",
        }
    )

    def event(
        self,
        name: str,
        *,
        now: datetime,
        correlation_id: str,
        fields: Mapping[str, Any] | None = None,
    ) -> str:
        safe = {
            key: self._safe_value(key, value)
            for key, value in sorted((fields or {}).items())
        }
        record = {
            "event": name,
            "occurred_at": to_iso(now),
            "correlation_id": correlation_id,
            **safe,
        }
        return json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _safe_value(self, key: str, value: Any) -> Any:
        folded = key.casefold()
        if folded in self.SENSITIVE_FIELDS or folded in SENSITIVE_HEADERS:
            return "[REDACTED]"
        if folded.endswith("_url") and isinstance(value, str):
            return redact_url(value)
        if folded == "headers" and isinstance(value, Mapping):
            return sanitize_headers({str(k): str(v) for k, v in value.items()})
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)[:300]


def production_metrics(*, max_series: int = 500) -> BoundedMetrics:
    registry = BoundedMetrics(max_series=max_series)
    registry.declare("relayforge_dispatch_total", labels=("outcome",))
    registry.declare("relayforge_retry_total", labels=("reason",))
    registry.declare("relayforge_circuit_total", labels=("decision",))
    registry.declare("relayforge_sqlite_contention_total", labels=("operation",))
    registry.declare("relayforge_queue_age_seconds", labels=("priority",))
    registry.declare("relayforge_dispatch_latency_seconds", labels=("outcome",))
    return registry


def _metric_name(value: str) -> str:
    if not value or not all(ch.isalnum() or ch in "_:" for ch in value):
        raise ValueError("metric name contains unsupported characters")
    return value


def _label_value(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("metric label value must be 1..64 characters")
    return value
