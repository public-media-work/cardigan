"""Deterministic field length / count limit checking.

Flags-only: values are never truncated, trimmed, or otherwise mutated here
(that's a model-fixable violation to hand back to a later generation pass,
not something the engine does silently). Compares each field present in
*both* the input mapping and ``StyleRules``' resolved limits (via
``limits_for``) and reports ``RuleViolation``s for anything out of bounds.
"""

from __future__ import annotations

from collections.abc import Mapping

from api.services.style_engine.rules import StyleRules
from api.services.style_engine.titles import split_title
from api.services.style_engine.types import RuleViolation


def check_field_limits(
    fields: Mapping[str, str | list[str] | None],
    rules: StyleRules,
    phase: str,
    program: str | None = None,
    content_type: str = "full",
) -> list[RuleViolation]:
    """Check ``fields`` against ``rules.limits_for(program, content_type)``.

    For each field present in both ``fields`` and the resolved limits:

    - String value with a ``"max"`` limit: ``len(value) > max`` ->
      severity ``"error"``, ``rule_id="limits.<field>.max"``, message
      names both the actual length and the limit. ``model_fixable=True``.
      The value is NEVER modified -- detection only. A ``title`` is
      measured without its program name (#459).
    - ``title`` with a ``"full_max"`` limit: the whole string, program name
      included, over ``full_max`` -> ``rule_id="limits.title.full_max"``.
    - List value with a ``"count"`` limit: length outside
      ``[min, max]`` -> severity ``"warning"``,
      ``rule_id="limits.<field>.count"``.
    - ``None`` values and fields with no applicable limit entry (absent,
      or present as an empty ``{}``) are skipped.
    """
    limits = rules.limits_for(program, content_type)
    violations: list[RuleViolation] = []

    for field_name, value in fields.items():
        if value is None:
            continue

        limit = limits.get(field_name)
        if not limit:
            continue

        if isinstance(value, str):
            measured, program = split_title(value) if field_name == "title" else (value, None)
            max_len = limit.get("max")
            if max_len is not None and len(measured) > max_len:
                what = " excluding the program name" if program else ""
                violations.append(
                    RuleViolation(
                        rule_id=f"limits.{field_name}.max",
                        phase=phase,
                        severity="error",
                        message=f"{field_name} is {len(measured)} chars{what} (limit {max_len})",
                        field=field_name,
                        model_fixable=True,
                    )
                )
            full_max = limit.get("full_max") if field_name == "title" else None
            if full_max is not None and len(value) > full_max:
                violations.append(
                    RuleViolation(
                        rule_id=f"limits.{field_name}.full_max",
                        phase=phase,
                        severity="error",
                        message=f"{field_name} is {len(value)} chars in full (limit {full_max})",
                        field=field_name,
                        model_fixable=True,
                    )
                )

        elif isinstance(value, list):
            count_limit = limit.get("count")
            if not count_limit:
                continue
            count = len(value)
            min_count = count_limit.get("min")
            max_count = count_limit.get("max")
            too_few = min_count is not None and count < min_count
            too_many = max_count is not None and count > max_count
            if too_few or too_many:
                violations.append(
                    RuleViolation(
                        rule_id=f"limits.{field_name}.count",
                        phase=phase,
                        severity="warning",
                        message=(f"{field_name} has {count} items " f"(expected {min_count}-{max_count})"),
                        field=field_name,
                        model_fixable=True,
                    )
                )

    return violations
