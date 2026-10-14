"""Physical cleanup validation with only exact retained logical probe exclusions."""

from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import (
    NODE_UPGRADE_OWNERS,
    NodeUpgradeCollectiveScope,
    NodeUpgradeInventory,
    UpgradeExecutionPlan,
)


def verify_retained_collective_inventory(
    plan: UpgradeExecutionPlan,
    expected_scope_sha256s: tuple[str, ...],
    scopes: tuple[NodeUpgradeCollectiveScope, ...],
    interrupted_scope_sha256s: tuple[str, ...],
    local: NodeUpgradeInventory | None,
    peer: NodeUpgradeInventory,
    *,
    defer_local: bool = False,
) -> None:
    hashes = tuple(scope.sha256() for scope in scopes)
    if (
        len(scopes) > 6
        or len(hashes) != len(set(hashes))
        or sorted(hashes) != sorted(expected_scope_sha256s)
        or len(interrupted_scope_sha256s) != len(set(interrupted_scope_sha256s))
        or not set(interrupted_scope_sha256s).issubset(hashes)
        or any(
            scope.plan.authority_sha256() != plan.authority_sha256()
            or scope.plan.operation.job_id != plan.operation.job_id
            or scope.request.generation != plan.operation.generation
            for scope in scopes
        )
    ):
        raise UpgradeUnavailable("Cold continuation retained dispatch authority differs")
    commands = {
        str(scope.command_id) for scope in scopes if scope.sha256() in interrupted_scope_sha256s
    }
    if local is None:
        if not defer_local or commands or not peer.idle(NODE_UPGRADE_OWNERS):
            raise UpgradeUnavailable("Cold continuation has no required physical inventory")
        return
    if len(commands) != len(interrupted_scope_sha256s):
        raise UpgradeUnavailable("Cold continuation repeats a logical command")
    matched: set[str] = set()
    owners = []
    for owner in local.owners:
        retained = []
        for entry in owner.entries:
            if (
                owner.owner == "probes"
                and entry.kind == "upgrade-collective"
                and entry.work_id in commands
                and entry.state == "cleaning"
                and entry.holds == ("unresolved-collective",)
                and entry.process_id is None
                and entry.process_created_at is None
            ):
                if entry.work_id in matched:
                    raise UpgradeUnavailable("Cold continuation inventory repeats an obligation")
                matched.add(entry.work_id)
            else:
                retained.append(entry)
        owners.append(owner.model_copy(update={"entries": tuple(retained)}))
    if (
        matched != commands
        or not local.model_copy(update={"owners": tuple(owners)}).idle(NODE_UPGRADE_OWNERS)
        or not peer.idle(NODE_UPGRADE_OWNERS)
    ):
        raise UpgradeUnavailable("Cold continuation retains physical or unknown ownership")
