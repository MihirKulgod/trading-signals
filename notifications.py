"""
Notification rules: decide *when* to notify, independent of *how*.

A rule watches one node id (any id in node_scores -- top-level, definition, or
nested) and turns it into a met/not-met boolean via ``kind``. NotificationEngine
tracks that boolean's transitions across ticks and fires on the configured
``edge``. Delivery is a single injected function, left as a log-only stub until
a real channel (Telegram/email/etc.) is chosen -- nothing above that function
needs to change when one is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from app_logging import get_logger

log = get_logger(__name__)


def _is_met(value: Any) -> bool:
    """The 'fires' convention used everywhere else: not missing/NaN, and >= 0."""
    return value is not None and not (isinstance(value, float) and math.isnan(value)) and value >= 0


def _state_met(target: str, scores: dict, children: dict, params: dict) -> bool:
    return _is_met(scores.get(target))


def _children_met(target: str, scores: dict, children: dict, params: dict) -> bool:
    kids = children.get(target, [])
    met_count = sum(1 for kid in kids if _is_met(scores.get(kid)))
    return met_count >= params.get("min_met", 1)


# Add a new trigger kind by writing one function here with this signature and
# registering it -- nothing else (storage, engine loop, edge handling) changes.
METRICS: dict[str, Callable[[str, dict, dict, dict], bool]] = {
    "state": _state_met,
    "children_met": _children_met,
}


def is_met(kind: str, target: str, scores: dict, children: dict, params: dict) -> bool:
    """
    Evaluate one (kind, target, params) trigger against live state -- the same
    computation a notification rule's edge detection is built on, reused
    directly wherever something just needs a continuous met/not-met read (e.g.
    dashboard panel visibility). False for an unknown kind, never a crash.
    """
    metric = METRICS.get(kind)
    return bool(metric(target, scores, children, params)) if metric else False


@dataclass
class NotificationRule:
    id: str
    label: str
    target: str
    kind: str = "state"           # "state" | "children_met"
    params: dict = field(default_factory=dict)
    edge: str = "rising"          # "rising" | "falling" | "level"
    enabled: bool = True


@dataclass
class NotificationEvent:
    rule: NotificationRule
    met: bool
    value: Any


def default_send(event: NotificationEvent) -> None:
    """Placeholder delivery until a real channel is wired in."""
    log.info("NOTIFY: %s (target=%s, value=%s)", event.rule.label, event.rule.target, event.value)


def load_rules(settings_doc: dict) -> list[NotificationRule]:
    raw = ((settings_doc or {}).get("notifications") or {}).get("rules") or []
    return [
        NotificationRule(
            id=str(r.get("id")),
            label=str(r.get("label") or r.get("id") or ""),
            target=str(r.get("target")),
            kind=str(r.get("kind", "state")),
            params=dict(r.get("params") or {}),
            edge=str(r.get("edge", "rising")),
            enabled=bool(r.get("enabled", True)),
        )
        for r in raw
    ]


class NotificationEngine:
    """
    Tracks each rule's last known met/not-met state so it can detect
    transitions across ticks. One instance per live-engine run: rules are
    loaded fresh at bootstrap, same as the strategy itself.
    """

    def __init__(self, send: Callable[[NotificationEvent], None] = default_send):
        self.send = send
        self._last_met: dict[str, bool] = {}

    def evaluate(self, rules: list[NotificationRule], scores: dict,
                children: dict) -> list[NotificationEvent]:
        events = []
        for rule in rules:
            if not rule.enabled:
                continue
            metric = METRICS.get(rule.kind)
            if metric is None:
                continue
            met = metric(rule.target, scores, children, rule.params)
            was_met = self._last_met.get(rule.id, False)
            fires = ((rule.edge == "rising" and met and not was_met)
                    or (rule.edge == "falling" and not met and was_met)
                    or (rule.edge == "level" and met))
            self._last_met[rule.id] = met
            if fires:
                events.append(NotificationEvent(rule, met, scores.get(rule.target)))
        for event in events:
            self.send(event)
        return events
