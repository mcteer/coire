"""Authorize only fixed registry generation through the normal node engine proxy."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from coire_core.errors import UpgradeConflict, UpgradeUnavailable
from coire_core.models.engine import EngineStartRequest, EngineState
from coire_core.models.gateway import ChatMessage, EngineChatRequest
from coire_core.models.registry import EngineBackend
from coire_core.models.upgrades import (
    CoreNodeSmokeCleanupReceipt,
    CoreNodeSmokeLifecycle,
    CoreNodeSmokeScope,
)
from coire_core.settings import Settings
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_runtime import running_runtime_sha256

if TYPE_CHECKING:
    from coire_node.engines import EngineManager
    from coire_node.maintenance import NodeMaintenance
    from coire_node.store import Store

SMOKE_PROMPT = "Count from one to sixty-four, one number per line."
_admission: ContextVar[tuple[NodeMaintenance, Callable[[], None]] | None] = ContextVar(
    "core_smoke_node_admission", default=None
)


def authorize_current_smoke(gate: NodeMaintenance) -> bool:
    context = _admission.get()
    if context is None:
        return False
    if context[0] is not gate:
        raise UpgradeConflict("Core smoke admission belongs to another node owner")
    context[1]()
    return True


@contextmanager
def core_smoke_admission(
    body: EngineChatRequest | EngineStartRequest,
    path: str,
    settings: Settings,
    gate: NodeMaintenance,
    engines: EngineManager,
    store: Store,
) -> Iterator[None]:
    scope = body.coire_upgrade_scope
    if scope is None:
        raise UpgradeConflict("Core smoke admission has no fixed scope")

    def lifecycle() -> CoreNodeSmokeLifecycle | None:
        return core_smoke_store(settings, scope).read("lifecycle.json", CoreNodeSmokeLifecycle)

    def verify() -> None:
        authorize_core_smoke(body, path, settings, gate, engines, store)
        current = lifecycle()
        if current is None or current.scope != scope or current.state != "active":
            raise UpgradeConflict("Core smoke lifecycle is absent, revoked or changed")

    with gate.lock:
        authorize_core_smoke(body, path, settings, gate, engines, store)
        private = core_smoke_store(settings, scope)
        with private.ownership("lifecycle.lock"):
            current = lifecycle()
            if current is not None and (current.scope != scope or current.state != "active"):
                raise UpgradeConflict("Core smoke lifecycle is revoked or changed")
            if isinstance(body, EngineStartRequest):
                manifest = store.read_manifest(scope.plan.model_slug)
                if manifest is None or store.verify_against(scope.plan.model_slug, manifest):
                    raise UpgradeConflict(
                        "Core smoke model content differs from its verified manifest"
                    )
            if current is None:
                if (
                    not isinstance(body, EngineStartRequest)
                    or engines.get(scope.engine_id) is not None
                ):
                    raise UpgradeConflict("Core smoke cannot acquire an existing unowned engine")
                private.write(
                    "lifecycle.json",
                    CoreNodeSmokeLifecycle(scope=scope, updated_at=datetime.now(UTC)),
                    replace=False,
                )

    with gate.transfer_continuation(verify):
        token = _admission.set((gate, verify))
        try:
            yield
        finally:
            _admission.reset(token)


def core_smoke_store(settings: Settings, scope: CoreNodeSmokeScope) -> PrivateUpgradeStore:
    root = PrivateUpgradeStore(Path(settings.node_state_dir) / "upgrades/core-smokes")
    return PrivateUpgradeStore(root.path(str(scope.engine_id)))


def fixed_smoke_request(scope: CoreNodeSmokeScope) -> EngineChatRequest:
    return EngineChatRequest(
        model=scope.plan.model_slug,
        messages=[ChatMessage(role="user", content=SMOKE_PROMPT)],
        stream=False,
        max_tokens=64,
        temperature=0,
        coire_upgrade_scope=scope,
    )


def authorize_core_smoke(
    body: EngineChatRequest | EngineStartRequest,
    path: str,
    settings: Settings,
    gate: NodeMaintenance,
    engines: EngineManager,
    store: Store,
) -> None:
    scope = body.coire_upgrade_scope
    if scope is None or not settings.upgrades_enabled:
        raise UpgradeUnavailable("Core serving smoke has no enabled accepted scope")
    loading = isinstance(body, EngineStartRequest)
    fixed = (
        EngineStartRequest(
            engine_id=scope.engine_id,
            slug=scope.plan.model_slug,
            target=body.target,
            estimate_bytes=body.estimate_bytes,
            coire_upgrade_scope=scope,
        )
        if isinstance(body, EngineStartRequest)
        else fixed_smoke_request(scope)
    )
    if (
        path
        != (
            "/node/engines"
            if loading
            else f"/node/engines/{scope.engine_id}/proxy/v1/chat/completions"
        )
        or scope.node != settings.node_name
        or scope.plan.source.manifest.project != settings.upgrade_project
        or body != fixed
        or scope.runtime_sha256 != running_runtime_sha256(settings)
        or not 0 < (scope.deadline_at - datetime.now(UTC)).total_seconds() <= 300
    ):
        raise UpgradeConflict("Core serving smoke differs from fixed engine, runtime or generation")
    fence = gate.current()
    if scope.node in scope.plan.maintenance.targets or "cluster" in scope.plan.maintenance.targets:
        if fence is None or not fence.matches_plan(scope.plan):
            raise UpgradeConflict("Core serving smoke lacks its exact node maintenance owner")
    elif gate.blocked():
        raise UpgradeConflict("Another operation retains node maintenance")
    engine = engines.get(scope.engine_id)
    if isinstance(body, EngineStartRequest):
        target = body.target
        if (
            target is None
            or (target.model_id, target.variant_id, target.base_manifest_sha256, target.adapter_id)
            != (
                scope.plan.model_id,
                scope.plan.model_variant_id,
                scope.plan.model_manifest_sha256,
                None,
            )
            or not 0 < body.estimate_bytes <= 2 * 1024**3
        ):
            raise UpgradeConflict("Core serving smoke load differs from its fixed tiny target")
    if (engine is None and not loading) or (
        engine is not None
        and (
            engine.state
            not in ({EngineState.STARTING, EngineState.READY} if loading else {EngineState.READY})
            or engine.slug != scope.plan.model_slug
            or engine.backend is not EngineBackend.MLX_LM
            or engine.target is None
            or engine.target.model_id != scope.plan.model_id
            or engine.target.variant_id != scope.plan.model_variant_id
            or engine.target.base_manifest_sha256 != scope.plan.model_manifest_sha256
            or engine.target.adapter_id is not None
        )
    ):
        raise UpgradeConflict("Core serving smoke engine differs from its exact registry target")
    manifest = store.read_manifest(scope.plan.model_slug)
    if (
        manifest is None
        or manifest.sha256() != scope.plan.model_manifest_sha256
        or not 0 < manifest.total_bytes <= 1024**3
    ):
        raise UpgradeConflict("Core serving smoke registry manifest differs")


def cleanup_core_smoke(
    settings: Settings, scope: CoreNodeSmokeScope, gate: NodeMaintenance, engines: EngineManager
) -> CoreNodeSmokeCleanupReceipt:
    """Revoke queued starts before stopping only this positively owned engine."""
    if (
        scope.node != settings.node_name
        or scope.runtime_sha256 != running_runtime_sha256(settings)
        or scope.plan.source.manifest.project != settings.upgrade_project
    ):
        raise UpgradeConflict("Core smoke cleanup differs from its exact native runtime")
    if engines._lock is not gate.lock:
        raise UpgradeConflict("Core smoke cleanup requires shared engine admission ownership")
    private = core_smoke_store(settings, scope)
    with gate.lock, private.ownership("lifecycle.lock"):
        current = private.read("lifecycle.json", CoreNodeSmokeLifecycle)
        if current is None:
            if engines.get(scope.engine_id) is not None:
                raise UpgradeConflict(
                    "Core smoke cleanup cannot acquire an unowned existing engine"
                )
            engines.verify_no_unowned_native_processes()
            current = CoreNodeSmokeLifecycle(
                scope=scope, state="revoked", updated_at=datetime.now(UTC)
            )
            private.write("lifecycle.json", current, replace=False)
        if current.scope != scope:
            raise UpgradeConflict("Core smoke cleanup has no exact retained process intent")
        if current.state == "active":
            current = current.model_copy(
                update={"state": "revoked", "updated_at": datetime.now(UTC)}
            )
            private.write("lifecycle.json", current)
        engine = engines.get(scope.engine_id)
        if engine is None:
            engines.verify_no_unowned_native_processes()
        if engine is not None and (
            engine.slug != scope.plan.model_slug
            or engine.target is None
            or (
                engine.target.model_id,
                engine.target.variant_id,
                engine.target.base_manifest_sha256,
                engine.target.adapter_id,
            )
            != (
                scope.plan.model_id,
                scope.plan.model_variant_id,
                scope.plan.model_manifest_sha256,
                None,
            )
        ):
            raise UpgradeConflict("Core smoke cleanup found a different engine target")
        if engine is not None and engine.state is not EngineState.STOPPED:
            if engine.state is not EngineState.STOPPING:
                engines.stop(scope.engine_id)
            raise UpgradeUnavailable("Core smoke process stop is not yet positively observed")
        if current.state != "released":
            private.write(
                "lifecycle.json",
                current.model_copy(
                    update={
                        "state": "released",
                        "updated_at": datetime.now(UTC),
                    }
                ),
            )
        return CoreNodeSmokeCleanupReceipt(
            scope_sha256=scope.sha256(),
            engine_id=scope.engine_id,
            runtime_sha256=scope.runtime_sha256,
            observed_at=datetime.now(UTC),
        )
