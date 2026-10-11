"""Readiness from current-run records; no browser work or business defaults."""
import hashlib
import json
from core.contracts import AgentStatus
from services.tender_resolution import normalize_id
from services.specification_resolution import is_current

TECHNICAL_FIELDS = ('catalogue_group', 'bore', 'stroke', 'pressure', 'mounting', 'certification')


def fingerprint(value):
    """Stable input identity: renewed observation timestamps alone are not new work."""
    def clean(item):
        if hasattr(item, 'model_dump'):
            item = item.model_dump(mode='json')
        if isinstance(item, dict):
            result = {k: clean(v) for k, v in item.items() if k != 'observed_at'
                      and not (k == 'id' and 'source_site' in item)}
            if 'evidence' in result:
                unique = {json.dumps(e, sort_keys=True): e for e in result['evidence']}
                result['evidence'] = [unique[k] for k in sorted(unique)]
            return result
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True, default=str).encode()).hexdigest()


class Dependencies:
    def __init__(self, state):
        self.state = state

    def verified(self, record):
        return record is not None and (self.state.config.mode == 'test' or bool(record.evidence))

    def requirement(self, position):
        technical = self.state.technical
        matches = [r for r in (technical.requirements if technical else [])
                   if normalize_id(r.requirement_id) == normalize_id(position.requirement_id or '')
                   and (not r.position_ids or position.position_id in r.position_ids)]
        return matches[0] if len(matches) == 1 else None

    def technical_ready(self, requirement):
        technical = self.state.technical
        if not self.verified(technical) or technical.conflicts or not self.verified(requirement) or any(
                getattr(requirement, f) is None for f in TECHNICAL_FIELDS):
            return False
        if requirement.usable_travel is not None and requirement.stroke_reserve_per_end is None:
            return False
        if self.state.config.mode == 'production':
            if not requirement.revision or requirement.revision != technical.governing_revision:
                return False
            if not any(v.revision == requirement.revision and is_current(v) and self.verified(v) and
                       (not requirement.specification_id or v.specification_id == requirement.specification_id)
                       for v in technical.revisions):
                return False
        return True

    def erp_ready_positions(self):
        tender, technical = self.state.tender, self.state.technical
        if not self.verified(tender) or not self.verified(technical) or tender.conflicts or technical.conflicts:
            return []
        if self.state.config.mode == 'production' and tender.qa_complete is not True:
            return []  # quantities/amendments are provisional until the Q&A is investigated
        ready = []
        for p in tender.positions:
            r = self.requirement(p)
            if p.quantity is None or p.quantity <= 0 or not p.unit or not self.verified(p):
                continue
            if not self.technical_ready(r):
                continue
            conversion = next((c for c in technical.conversions if c.position_id == p.position_id), None)
            if self.state.config.mode == 'production' and (conversion is None or conversion.pieces is None or
                    conversion.pieces <= 0 or conversion.quantity != p.quantity or conversion.unit != p.unit):
                continue
            ready.append(p.position_id)
        return ready

    def ready(self, role):
        tender = self.state.tender
        if role == 'tender':
            return True
        if role == 'supplier':
            return self.verified(tender) and bool(tender.positions) and any(
                p.specification_reference and p.requirement_id for p in tender.positions)
        if role == 'mail':
            return self.verified(tender) and bool(tender.reference)
        if role == 'erp':
            return bool(self.erp_ready_positions())
        if role == 'pricing':
            return bool(self.erp_covered_positions()) and all(self.verified(r) for r in
                (self.state.tender, self.state.technical, self.state.erp, self.state.commercial))
        return False

    def erp_covered_positions(self):
        from agents.erp import ErpAgent, exact_match
        erp = self.state.erp
        if not self.state.tender or not self.verified(erp) or erp.conflicts or any(
                not m.specification or not m.specification.catalogue_group for m in erp.materials):
            return []
        ready = set(self.erp_ready_positions())
        covered = []
        for p in self.state.tender.positions:
            if p.position_id not in ready:
                continue
            r = self.requirement(p)
            if r.catalogue_group not in erp.searched_catalogue_groups:
                continue
            candidates = [m for m in erp.materials if m.specification and
                          m.specification.catalogue_group == r.catalogue_group]
            if any(m.active is None or (m.active is True and any(
                    getattr(m.specification, f) is None for f in TECHNICAL_FIELDS)) or
                    (exact_match(m, r) and (not self.verified(m) or m.records_complete is not True or
                                           not ErpAgent.material_complete(m))) for m in candidates):
                continue
            covered.append(p.position_id)
        return covered

    def input_key(self, role, task):
        # Own outputs are omitted: publishing progress must not schedule the same investigation again.
        tender, technical, erp, commercial = task.tender, task.technical, task.erp, task.commercial
        if role == 'tender':
            values = None
        elif role == 'supplier':
            releases = [r for r in (commercial.rules if commercial else [])
                        if any(word in f'{r.kind} {r.subject}'.casefold() for word in ('technik', 'technical'))]
            values = (tender, releases)
        elif role == 'erp':
            values = (tender, technical)
        elif role == 'mail':
            values = (tender, [(r.specification_id, r.requirement_id, r.revision)
                               for r in (technical.requirements if technical else [])],
                      [(r.specification_id, r.revision) for r in (technical.revisions if technical else [])],
                      sorted({m.metal_index_code for m in (erp.materials if erp else []) if m.metal_index_code}),
                      sorted({c.fx_key for m in (erp.materials if erp else []) for c in m.conditions if c.fx_key}))
        else:
            values = (tender, technical, erp, commercial)
        return fingerprint(values)

    def positions(self):
        """Expose dependency state per position without treating a partial portfolio bid as final."""
        ready = set(self.erp_ready_positions())
        covered = set(self.erp_covered_positions())
        result = {}
        for p in (self.state.tender.positions if self.state.tender else []):
            roles = {}
            for role in ('tender', 'supplier', 'erp', 'mail', 'pricing'):
                status = self.state.statuses.get(role, AgentStatus.pending)
                if role in ('erp', 'pricing') and p.position_id not in ready:
                    status = AgentStatus.blocked
                elif role == 'supplier':
                    r = self.requirement(p)
                    if self.technical_ready(r):
                        status = AgentStatus.complete
                    elif status == AgentStatus.complete:
                        status = AgentStatus.blocked
                elif role == 'erp':
                    if p.position_id in covered:
                        status = AgentStatus.complete
                    elif status == AgentStatus.complete:
                        status = AgentStatus.blocked
                elif role == 'pricing':
                    bid = self.state.bid
                    if bid and bid.complete and not bid.missing:
                        status = AgentStatus.complete
                    elif status == AgentStatus.complete:
                        status = AgentStatus.blocked
                roles[role] = status
                if any(m.agent_id == role and m.position_id in (None, p.position_id)
                       for m in self.state.missing) and status == AgentStatus.complete:
                    roles[role] = AgentStatus.blocked
            result[p.position_id] = roles
        return result
