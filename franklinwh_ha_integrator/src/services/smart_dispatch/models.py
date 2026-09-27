"""SmartDispatch data models.

Extracted from `smart_dispatch/__init__.py` in v0.2.3 (Phase 1 Stage A,
2026-08-05) as the smallest-risk split. `RuleResult` and `EvalDecision`
are pure dataclasses with no collaborators — moving them out is a
one-way import (models has no siblings). They're re-exported from
`__init__.py` so `from src.services.smart_dispatch import EvalDecision`
keeps working at all 15 external import sites."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RuleResult:
    """The output of a single engine evaluation cycle."""
    action: str                         # GRID_CHARGE | GRID_EXPORT | STANDBY | HOLD (no-op — native mode continues)
    preset_name: Optional[str] = None   # set when action == APPLY_PRESET
    rule_id: str = ""
    rule_name: str = ""
    priority: int = 999
    reason: str = ""
    conditions_met: list[str] = field(default_factory=list)
    confidence: float = 1.0
    can_execute: bool = False           # True only when baseline preset exists
    baseline_missing: bool = False      # warn when AMBER_BASELINE_PRESET absent
    evaluated_at: float = field(default_factory=time.time)
    action_payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action":           self.action,
            "preset_name":      self.preset_name,
            "rule_id":          self.rule_id,
            "rule_name":        self.rule_name,
            "priority":         self.priority,
            "reason":           self.reason,
            "conditions_met":   self.conditions_met,
            "confidence":       self.confidence,
            "can_execute":      self.can_execute,
            "baseline_missing": self.baseline_missing,
            "evaluated_at":     self.evaluated_at,
        }


@dataclass
class EvalDecision(RuleResult):
    """Extended result produced by the 6-rule evaluation chain.
    Adds transparency and dispatch-plan fields for the Live Engine Panel."""
    trigger_category: str = "fallback"      # demand_charge | negative_export | price_spike |
                                            #  export_bonus  | earnings_target | force_charge |
                                            #  fallback      | paused
    dispatch_summary: str = ""              # human-readable plan shown in UI panel
    requires_approval: bool = False         # True → actionable notification sent before execute
    ha_entity_action: Optional[str] = None  # HA entity to call (e.g. solar disable switch)
    ha_entity_state: Optional[str] = None   # desired state: "on" | "off"
    evaluated_params: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update({
            "trigger_category":  self.trigger_category,
            "dispatch_summary":  self.dispatch_summary,
            "requires_approval": self.requires_approval,
            "ha_entity_action":  self.ha_entity_action,
            "ha_entity_state":   self.ha_entity_state,
            "evaluated_params":  self.evaluated_params,
        })
        return d
