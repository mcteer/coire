"""Signed placement/rank contracts reject incomplete or ambiguous residency claims."""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from coire_core.failover_crypto import sign_ed25519
from coire_core.failover_residency import process_generation, rank_observation_current
from coire_core.models.adapters import InferenceTarget
from coire_core.models.failover import (
    FailoverModel,
    FailoverRankIdentity,
    FailoverRankObservation,
    FailoverRankProbe,
    FailoverShardedPlacement,
)


def placement() -> FailoverShardedPlacement:
    return FailoverShardedPlacement(
        mode="tp",
        instance_id=uuid4(),
        engine_id=uuid4(),
        group_id=uuid4(),
        generation="a" * 64,
        target=InferenceTarget(model_id=uuid4(), variant_id=uuid4(), base_manifest_sha256="a" * 64),
        ranks=[
            FailoverRankIdentity(rank=0, node="coire-edge-a", generation="b" * 64),
            FailoverRankIdentity(rank=1, node="coire-edge-b", generation="c" * 64),
        ],
    )


@pytest.mark.parametrize(
    "mutation",
    ["duplicate_rank", "duplicate_node", "duplicate_generation", "missing_rank", "adapter"],
)
def test_sharded_snapshot_requires_exact_two_rank_base_identity(mutation: str) -> None:
    value = placement().model_dump(mode="json")
    if mutation == "missing_rank":
        value["ranks"].pop()
    elif mutation == "adapter":
        value["target"].update(adapter_id=str(uuid4()), adapter_manifest_sha256="d" * 64)
    else:
        field = {
            "duplicate_rank": "rank",
            "duplicate_node": "node",
            "duplicate_generation": "generation",
        }[mutation]
        value["ranks"][1][field] = value["ranks"][0][field]
    with pytest.raises(ValidationError):
        FailoverShardedPlacement.model_validate(value)


@pytest.mark.parametrize(
    "field",
    [
        None,
        "challenge",
        "epoch",
        "instance_id",
        "group_id",
        "group_generation",
        "target",
        "rank",
        "healthy",
        "link_healthy",
        "expires_at",
        "signature",
    ],
)
def test_live_rank_evidence_binds_every_identity_and_lifetime(field: str | None) -> None:
    group = placement()
    now = datetime.now(UTC)
    key = Ed25519PrivateKey.generate()
    private = base64.b64encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    ).decode()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    probe = FailoverRankProbe(
        challenge=uuid4(),
        member="coire-edge-a",
        epoch=1,
        placement=group,
        issued_at=now,
        expires_at=now + timedelta(seconds=3),
        signature="isolated-probe-fixture",
    )
    evidence = FailoverRankObservation(
        challenge=probe.challenge,
        host="coire-edge-a",
        epoch=1,
        instance_id=group.instance_id,
        group_id=group.group_id,
        group_generation=group.generation,
        target=group.target,
        rank=group.ranks[0],
        healthy=True,
        link_healthy=True,
        issued_at=now,
        expires_at=probe.expires_at,
        signature="unsigned",
    )
    changes = {
        "challenge": uuid4(),
        "epoch": 2,
        "instance_id": uuid4(),
        "group_id": uuid4(),
        "group_generation": "d" * 64,
        "target": group.target.model_copy(update={"variant_id": uuid4()}),
        "rank": group.ranks[0].model_copy(update={"generation": "e" * 64}),
        "healthy": False,
        "link_healthy": False,
        "expires_at": now + timedelta(seconds=4),
    }
    if field is not None and field != "signature":
        evidence = evidence.model_copy(update={field: changes[field]})
    evidence = evidence.model_copy(
        update={"signature": sign_ed25519(evidence.canonical_bytes(), private)}
    )
    if field == "signature":
        evidence = evidence.model_copy(update={"signature": "invalid"})
    assert rank_observation_current(probe, evidence, public, now=now) is (field is None)
    assert not rank_observation_current(probe, evidence, public, now=now + timedelta(seconds=3))


def test_process_reuse_target_replacement_and_instance_replacement_change_generation() -> None:
    group = placement()
    fields: dict[str, Any] = {
        "instance_id": group.instance_id,
        "engine_id": group.engine_id,
        "node": "coire-edge-a",
        "pid": 10,
        "process_create_time": 100.0,
        "target": group.target,
        "group_id": group.group_id,
        "rank": 0,
    }
    original = process_generation(**fields)
    for field, value in {
        "process_create_time": 101.0,
        "instance_id": uuid4(),
        "group_id": uuid4(),
        "target": group.target.model_copy(update={"base_manifest_sha256": "f" * 64}),
    }.items():
        assert process_generation(**{**fields, field: value}) != original
    with pytest.raises(ValueError):
        process_generation(**{**fields, "process_create_time": float("nan")})


def test_published_capability_requires_exact_matching_model_placement() -> None:
    group = placement()
    with pytest.raises(ValidationError):
        FailoverModel(
            id=uuid4(),
            slug="fixture",
            display_name="Fixture",
            context_window=2048,
            capabilities=frozenset({"text_chat"}),
            placement=group,
        )
    with pytest.raises(ValidationError):
        FailoverModel(
            id=group.target.model_id,
            slug="fixture",
            display_name="Fixture",
            context_window=2048,
            capabilities=frozenset({"text_chat"}),
        )


@pytest.mark.parametrize("mutation", ["wrong_host", "long_lifetime", "zero_lifetime", "naive_time"])
def test_rank_observation_is_host_bound_and_short_lived(mutation: str) -> None:
    group = placement()
    now = datetime.now(UTC)
    fields = {
        "challenge": uuid4(),
        "host": "coire-edge-a",
        "epoch": 1,
        "instance_id": group.instance_id,
        "group_id": group.group_id,
        "group_generation": group.generation,
        "rank": group.ranks[0],
        "target": group.target,
        "healthy": True,
        "link_healthy": True,
        "issued_at": now,
        "expires_at": now + timedelta(seconds=3),
        "signature": "unsigned",
    }
    assert FailoverRankObservation.model_validate(fields)
    if mutation == "wrong_host":
        fields["host"] = "coire-edge-b"
    elif mutation == "naive_time":
        fields["issued_at"] = now.replace(tzinfo=None)
    else:
        fields["expires_at"] = now + timedelta(seconds=4 if mutation == "long_lifetime" else 0)
    with pytest.raises(ValidationError):
        FailoverRankObservation.model_validate(fields)
