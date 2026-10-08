from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re

from .token_vault import EphemeralTokenVault


_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*[\"']?[^\s\"',;]{8,}"),
)
_GOVERNMENT_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PAYMENT_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_HARD_CLASSES = frozenset({"credential", "government_id", "payment"})


@dataclass(frozen=True, slots=True)
class SensitiveFinding:
    data_class: str
    start: int
    end: int
    hard_block: bool


@dataclass(frozen=True, slots=True)
class ScanSummary:
    counts: tuple[tuple[str, int], ...]
    hard_blocked_classes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SanitizationResult:
    outcome: str
    transformed_text: str | None
    summary: ScanSummary
    token_count: int


class SensitiveTextScanner:
    """Deterministic local scanner; it never calls a model or network service."""

    def scan(self, text: str) -> tuple[SensitiveFinding, ...]:
        findings: list[SensitiveFinding] = []
        for pattern in _CREDENTIAL_PATTERNS:
            findings.extend(_matches(pattern, text, "credential", hard=True))
        findings.extend(_matches(_GOVERNMENT_ID, text, "government_id", hard=True))
        for match in _PAYMENT_CANDIDATE.finditer(text):
            digits = "".join(character for character in match.group(0) if character.isdigit())
            if _luhn_valid(digits):
                findings.append(SensitiveFinding("payment", match.start(), match.end(), True))
        findings.extend(_matches(_EMAIL, text, "email", hard=False))
        findings.extend(_matches(_PHONE, text, "phone", hard=False))
        return tuple(_non_overlapping(findings))

    def sanitize_for_remote(
        self,
        text: str,
        *,
        vault: EphemeralTokenVault,
        turn_id: str,
        destination_id: str,
        ttl: timedelta = timedelta(minutes=10),
        now: datetime | None = None,
    ) -> SanitizationResult:
        findings = self.scan(text)
        summary = _summary(findings)
        if any(finding.data_class in _HARD_CLASSES for finding in findings):
            return SanitizationResult(
                outcome="blocked",
                transformed_text=None,
                summary=summary,
                token_count=0,
            )
        transformed = text
        token_count = 0
        for finding in reversed(findings):
            value = transformed[finding.start : finding.end]
            token = vault.tokenize(
                value,
                data_class=finding.data_class,
                turn_id=turn_id,
                destination_id=destination_id,
                ttl=ttl,
                now=now,
            )
            transformed = transformed[: finding.start] + token + transformed[finding.end :]
            token_count += 1
        return SanitizationResult(
            outcome="redacted" if token_count else "clean",
            transformed_text=transformed,
            summary=summary,
            token_count=token_count,
        )


def _matches(
    pattern: re.Pattern[str],
    text: str,
    data_class: str,
    *,
    hard: bool,
) -> list[SensitiveFinding]:
    return [
        SensitiveFinding(data_class, match.start(), match.end(), hard)
        for match in pattern.finditer(text)
    ]


def _non_overlapping(findings: list[SensitiveFinding]) -> list[SensitiveFinding]:
    ordered = sorted(
        findings,
        key=lambda item: (item.start, 0 if item.hard_block else 1, -(item.end - item.start)),
    )
    result: list[SensitiveFinding] = []
    for finding in ordered:
        if any(finding.start < existing.end and existing.start < finding.end for existing in result):
            continue
        result.append(finding)
    return sorted(result, key=lambda item: item.start)


def _summary(findings: tuple[SensitiveFinding, ...]) -> ScanSummary:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.data_class] = counts.get(finding.data_class, 0) + 1
    return ScanSummary(
        counts=tuple(sorted(counts.items())),
        hard_blocked_classes=tuple(sorted({item.data_class for item in findings if item.hard_block})),
    )


def _luhn_valid(value: str) -> bool:
    if len(value) < 13 or len(value) > 19 or len(set(value)) == 1:
        return False
    total = 0
    parity = len(value) % 2
    for index, character in enumerate(value):
        digit = int(character)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
