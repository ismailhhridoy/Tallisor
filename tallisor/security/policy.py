"""Permission tiers.

    T0 READ       read inputs, compute, explain             agent, reviewer, owner, viewer
    T1 PROPOSE    draft journal entries, tasks, packs       agent, reviewer, owner
    T2 APPROVE    approve a drafted fix for export          reviewer, owner (humans only)
    T3 FORBIDDEN  move money, contact customers,            nobody: no implementation exists
                  submit financing applications

Two properties are enforced in code and covered by tests:
  1. The agent's ceiling is T1. It cannot approve its own work (OWASP ASI09).
  2. T3 actions are capability-absent: the system contains no code that moves
     money or sends messages, so no prompt can trigger one (OWASP ASI02/ASI03).
     Requests are refused and logged.
"""
from __future__ import annotations

from enum import IntEnum


class Tier(IntEnum):
    READ = 0
    PROPOSE = 1
    APPROVE = 2
    FORBIDDEN = 3


ROLE_CEILING = {
    "viewer": Tier.READ,
    "agent": Tier.PROPOSE,
    "reviewer": Tier.APPROVE,
    "owner": Tier.APPROVE,
}

HUMAN_ROLES = {"reviewer", "owner"}

ACTIONS = {
    # T0
    "ingest": Tier.READ,
    "reconcile": Tier.READ,
    "read_cases": Tier.READ,
    "forecast": Tier.READ,
    "alerts": Tier.READ,
    "match_financing": Tier.READ,
    "export_journal": Tier.READ,
    "verify_log": Tier.READ,
    # T1
    "investigate": Tier.PROPOSE,
    "draft_fix": Tier.PROPOSE,
    "prepare_financing_pack": Tier.PROPOSE,
    # T2
    "approve_fix": Tier.APPROVE,
    "reject_fix": Tier.APPROVE,
    "escalate_fix": Tier.PROPOSE,
    "halt": Tier.READ,              # anyone may press the kill switch
    # T3: listed so requests can be recognised and refused; never implemented
    "move_money": Tier.FORBIDDEN,
    "issue_refund": Tier.FORBIDDEN,
    "contact_customer": Tier.FORBIDDEN,
    "submit_financing_application": Tier.FORBIDDEN,
    "change_bank_details": Tier.FORBIDDEN,
}


class PermissionDenied(PermissionError):
    def __init__(self, role: str, action: str, reason: str):
        super().__init__(f"{role} may not {action}: {reason}")
        self.role, self.action, self.reason = role, action, reason


class Policy:
    def __init__(self):
        self.halted = False

    def tier_of(self, action: str) -> Tier:
        if action not in ACTIONS:
            # Unknown actions are treated as forbidden (default deny).
            return Tier.FORBIDDEN
        return ACTIONS[action]

    def authorize(self, role: str, action: str) -> Tier:
        tier = self.tier_of(action)
        if tier == Tier.FORBIDDEN:
            raise PermissionDenied(role, action, "this action is not available in Tallisor")
        if self.halted and action != "halt" and role == "agent":
            raise PermissionDenied(role, action, "the session was stopped with the kill switch")
        ceiling = ROLE_CEILING.get(role)
        if ceiling is None:
            raise PermissionDenied(role, action, "unknown role")
        if tier > ceiling:
            raise PermissionDenied(role, action, f"requires tier T{int(tier)}, role ceiling is T{int(ceiling)}")
        return tier
