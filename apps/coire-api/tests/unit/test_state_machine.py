"""The registry state machine and listing rules (T025, T062)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from coire_api.db import EngineProcessRow, ModelRow
from coire_api.registry import service
from coire_core.models.engine import EngineState
from coire_core.models.registry import LoadState, ModelState, Visibility


def _model(**kw: object) -> ModelRow:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "repo_id": "mlx-community/tiny",
        "slug": "mlx-community--tiny",
        "display_name": "tiny",
        "state": ModelState.READY,
        "visibility": Visibility.PUBLISHED,
        "entitlement": [],
        "tags": ["general"],
        "placement_policy": "single:auto",
        "precision": "4bit-g64",
        "weight_bytes": 1,
        "total_bytes": 1,
        "file_count": 1,
        "memory_estimate_bytes": 1,
        "capability_profile": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    return ModelRow(**{**base, **kw})


def _engine(
    state: EngineState, *, node: uuid.UUID | None = None, load_seconds: float | None = None
) -> EngineProcessRow:
    return EngineProcessRow(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        node_id=node or uuid.uuid4(),
        port=9500,
        state=state,
        estimate_bytes=1,
        started_at=datetime.now(UTC),
        load_seconds=load_seconds,
    )


class TestVisibility:
    def test_an_admin_sees_everything(self) -> None:
        for state in ModelState:
            model = _model(state=state, visibility=Visibility.ADMIN_ONLY)
            assert service.visible_to(is_admin=True, model=model)

    def test_a_user_sees_only_published_and_ready(self) -> None:
        assert service.visible_to(is_admin=False, model=_model())
        assert not service.visible_to(
            is_admin=False, model=_model(visibility=Visibility.ADMIN_ONLY)
        )
        assert not service.visible_to(is_admin=False, model=_model(state=ModelState.DOWNLOADING))
        assert not service.visible_to(is_admin=False, model=_model(state=ModelState.RETIRED))

    def test_an_entitlement_list_hides_a_model_until_feature_007(self) -> None:
        """There is nobody who could be on the list yet, so a non-empty list means nobody."""
        assert not service.visible_to(is_admin=False, model=_model(entitlement=["someone"]))


class TestLoadState:
    def test_no_engines_is_cold(self) -> None:
        assert service.load_state_for([])[0] is LoadState.COLD

    def test_a_ready_engine_is_loaded(self) -> None:
        state, nodes = service.load_state_for([_engine(EngineState.READY)])
        assert state is LoadState.LOADED and len(nodes) == 1

    def test_a_starting_engine_is_loading(self) -> None:
        assert service.load_state_for([_engine(EngineState.STARTING)])[0] is LoadState.LOADING

    def test_stopped_and_failed_engines_do_not_count(self) -> None:
        engines = [_engine(EngineState.STOPPED), _engine(EngineState.FAILED)]
        assert service.load_state_for(engines)[0] is LoadState.COLD

    def test_ready_wins_over_starting(self) -> None:
        engines = [_engine(EngineState.STARTING), _engine(EngineState.READY)]
        assert service.load_state_for(engines)[0] is LoadState.LOADED

    def test_loaded_on_lists_each_node_once(self) -> None:
        node = uuid.uuid4()
        engines = [_engine(EngineState.READY, node=node), _engine(EngineState.READY, node=node)]
        assert len(service.load_state_for(engines)[1]) == 1


class TestListingShape:
    def test_a_listing_carries_nothing_internal(self) -> None:
        """No repo id, no paths, no copies, no failure reasons (spec US5 scenario 1)."""
        model = _model(state=ModelState.READY, description="small and fast")
        listing = service.to_listing(model, [], node_names={})
        body = listing.model_dump()
        for forbidden in ("repo_id", "slug", "state_reason", "copies", "path", "manifest_sha256"):
            assert forbidden not in body

    def test_warmup_comes_from_the_most_recent_measured_load(self) -> None:
        model = _model()
        engines = [_engine(EngineState.STOPPED, load_seconds=42.0)]
        assert service.to_listing(model, engines, node_names={}).estimated_warmup_seconds == 42.0

    def test_warmup_is_null_before_any_load_has_been_measured(self) -> None:
        assert service.to_listing(_model(), [], node_names={}).estimated_warmup_seconds is None

    def test_node_ids_are_resolved_to_names(self) -> None:
        node = uuid.uuid4()
        listing = service.to_listing(
            _model(), [_engine(EngineState.READY, node=node)], node_names={node: "coire-edge-a"}
        )
        assert listing.loaded_on == ["coire-edge-a"]
