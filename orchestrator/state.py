"""Per-(student, skill) state and the dict-backed store (student-model doc §1).

The state is deliberately small: θ, n, certification fields, misconception
evidence counters, and the graph version the state was earned under (§8).
Every field is reconstructible from the attempt log alone (§9, the design
principle) — the update rule that does the reconstructing (§3) is not yet
implemented in this repo; this module is state and status-machine only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from . import constants


@dataclass
class SkillState:
    """What the model knows about one (student, skill) pair (§1).

    `misc_counters` maps misconception_id -> event_ids of the attempts that
    evidenced it *within the last MISC_WINDOW attempts on the skill*. The
    counter for a misconception is len of its list; keeping the event ids is
    what makes the window (and the 2-in-last-5 trigger, §4) reconstructible
    from the log. `recent_event_ids` is the sliding window itself.
    """

    theta: float = 0.0
    n: float = 0.0
    certified_depth: Optional[float] = None
    review_due: Optional[datetime] = None
    review_interval_days: float = constants.REVIEW_INITIAL_INTERVAL_DAYS
    lapsed: bool = False
    misc_counters: Dict[str, List[str]] = field(default_factory=dict)
    recent_event_ids: List[str] = field(default_factory=list)
    graph_version: str = ""

    def misc_count(self, misconception_id: str) -> int:
        return len(self.misc_counters.get(misconception_id, []))

    def record_attempt(self, event_id: str, misconception_id: Optional[str]) -> None:
        """Slide the last-MISC_WINDOW window and refresh the counters (§4)."""
        self.recent_event_ids.append(event_id)
        del self.recent_event_ids[:-constants.MISC_WINDOW]
        if misconception_id is not None:
            self.misc_counters.setdefault(misconception_id, []).append(event_id)
        window = set(self.recent_event_ids)
        self.misc_counters = {
            mid: [eid for eid in eids if eid in window]
            for mid, eids in self.misc_counters.items()
        }
        self.misc_counters = {mid: eids for mid, eids in self.misc_counters.items() if eids}


# Certification status machine (§7): uncertified → certified(d) → due → lapsed.
STATUS_UNCERTIFIED = "uncertified"
STATUS_CERTIFIED = "certified"
STATUS_DUE = "due"
STATUS_LAPSED = "lapsed"


def certification_status(state: SkillState, now: datetime) -> str:
    """Read the §7 status machine.

    `due` is derived from the clock (certified but past review_due); `lapsed`
    is set explicitly by a failed review probe (§6) and cleared by re-earning
    the certification or passing a later probe.
    """
    if state.certified_depth is None:
        return STATUS_UNCERTIFIED
    if state.lapsed:
        return STATUS_LAPSED
    if state.review_due is not None and now > state.review_due:
        return STATUS_DUE
    return STATUS_CERTIFIED


def is_certified(state: SkillState, depth: float, now: datetime) -> bool:
    """The prerequisite-check read (§7, core doc §1.3): certified at ≥ depth.

    A `due` certification still counts (review pending is not yet a lapse);
    a `lapsed` one does not — lapsed prerequisites block dependents from
    being served (§6.3) but revoke nothing.
    """
    if state.certified_depth is None or state.certified_depth < depth:
        return False
    return certification_status(state, now) in (STATUS_CERTIFIED, STATUS_DUE)


class StudentModelStore:
    """Dict-backed repository for v1 (§1) — no database.

    Holds one SkillState per (student_id, skill_id) plus the append-only
    attempt log every state is reconstructible from (§9).
    """

    def __init__(self) -> None:
        self._states: Dict[Tuple[str, str], SkillState] = {}
        self.attempt_log: List[dict] = []

    def get(self, student_id: str, skill_id: str) -> SkillState:
        """Fetch-or-create the record for (student, skill)."""
        key = (student_id, skill_id)
        if key not in self._states:
            self._states[key] = SkillState()
        return self._states[key]

    def records_for(self, student_id: str) -> Dict[str, SkillState]:
        """All skill records for one student, keyed by skill_id."""
        return {
            skill: state
            for (sid, skill), state in self._states.items()
            if sid == student_id
        }

    def replace_records(self, student_id: str, records: Dict[str, SkillState]) -> None:
        """Swap in a migrated record set (§8 lazy migration)."""
        for sid, skill in list(self._states):
            if sid == student_id:
                del self._states[(sid, skill)]
        for skill, state in records.items():
            self._states[(student_id, skill)] = state

    def append_event(self, event: dict) -> None:
        self.attempt_log.append(event)

    def students(self) -> Iterable[str]:
        return sorted({sid for sid, _ in self._states})
