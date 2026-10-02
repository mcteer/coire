"""Admin image inspection rejects unpinned, unlicensed and executable sources."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coire_api.db import DownloadJobRow, ModelRow, NodeRow
from coire_api.registry import image_acquisition
from coire_api.registry.inspection import classify_image_inspection
from coire_api.registry.placement import NodeView
from coire_api.registry.service import RegistryError
from coire_core.models.jobs import (
    ChecksumManifest,
    JobKind,
    JobStage,
    JobStatus,
    ManifestFile,
    RepoFile,
    RepoInspection,
)
from coire_core.models.node import Reachability
from coire_core.models.registry import (
    EngineBackend,
    ImageAssetAcquireRequest,
    ModelKind,
    ModelState,
)
from coire_core.settings import Settings


def _repo(**changes: object) -> RepoInspection:
    values: dict[str, object] = {
        "repo_id": "org/image-base",
        "revision": "a" * 40,
        "files": [
            RepoFile(path="config.json", bytes=100),
            RepoFile(path="model.safetensors", bytes=1024, upstream_sha256="b" * 64),
        ],
        "total_bytes": 1124,
        "weight_bytes": 1024,
        "is_mlx_format": False,
        "license_id": "apache-2.0",
    }
    values.update(changes)
    return RepoInspection.model_validate(values)


def test_image_base_inspection_has_explicit_kind_backend_and_licence() -> None:
    result = classify_image_inspection(_repo(), ModelKind.IMAGE_MODEL)
    assert result.supported
    assert result.kind is ModelKind.IMAGE_MODEL
    assert result.backend is EngineBackend.MFLUX
    assert result.license_id == "apache-2.0"
    assert result.revision == "a" * 40


def test_image_inspection_rejects_missing_licence_or_commit() -> None:
    assert (
        classify_image_inspection(_repo(license_id=None), ModelKind.IMAGE_MODEL).rejection_code
        == "licence_unreviewed"
    )
    assert (
        classify_image_inspection(_repo(revision="main"), ModelKind.IMAGE_MODEL).rejection_code
        == "unresolved_revision"
    )


def test_image_inspection_rejects_pickle_only_and_unverified_weights() -> None:
    pickle = _repo(
        files=[RepoFile(path="pytorch_model.bin", bytes=1024)],
        total_bytes=1024,
        weight_bytes=1024,
    )
    assert classify_image_inspection(pickle, ModelKind.IMAGE_MODEL).rejection_code == (
        "missing_safetensors"
    )
    unverified = _repo(
        files=[
            RepoFile(path="config.json", bytes=100),
            RepoFile(path="model.safetensors", bytes=1024),
        ]
    )
    assert classify_image_inspection(unverified, ModelKind.IMAGE_MODEL).rejection_code == (
        "unverified_weight"
    )


def test_auxiliary_inspection_never_claims_a_generation_backend() -> None:
    result = classify_image_inspection(_repo(), ModelKind.CONTROL_MODEL)
    assert result.supported
    assert result.backend is EngineBackend.AUXILIARY


def test_seedvr2_upscale_inspection_requires_exact_configless_layout() -> None:
    files = [
        RepoFile(path=name, bytes=1024, upstream_sha256="b" * 64)
        for name in ("seedvr2_ema_3b_fp16.safetensors", "ema_vae_fp16.safetensors")
    ]
    files.append(
        RepoFile(path="seedvr2_ema_7b_fp16.safetensors", bytes=2048, upstream_sha256="b" * 64)
    )
    repo = _repo(
        repo_id="numz/SeedVR2_comfyUI",
        files=files,
        total_bytes=4096,
        weight_bytes=4096,
    )
    result = classify_image_inspection(repo, ModelKind.UPSCALE_MODEL)
    assert result.supported and result.backend is EngineBackend.AUXILIARY
    assert (
        classify_image_inspection(
            _repo(repo_id="other/upscaler", files=files), ModelKind.UPSCALE_MODEL
        ).rejection_code
        == "missing_local_config"
    )
    assert (
        classify_image_inspection(
            _repo(repo_id="numz/SeedVR2_comfyUI", files=files[:1]), ModelKind.UPSCALE_MODEL
        ).rejection_code
        == "missing_local_config"
    )


def test_image_acquisition_contract_requires_human_licence_review() -> None:
    request = ImageAssetAcquireRequest(
        repo_id="org/image-base",
        kind=ModelKind.IMAGE_MODEL,
        accepted_license_id="apache-2.0",
    )
    assert request.accepted_license_id == "apache-2.0"
    base_id = uuid.uuid4()
    assert (
        ImageAssetAcquireRequest(
            repo_id="org/adapter",
            kind=ModelKind.IMAGE_LORA,
            accepted_license_id="apache-2.0",
            compatible_base_model_id=base_id,
        ).compatible_base_model_id
        == base_id
    )
    for payload in (
        {"repo_id": "org/image-base", "kind": "image_model"},
        {"repo_id": "org/image-base", "kind": "language_model", "accepted_license_id": "MIT"},
        {
            "repo_id": "org/image-base",
            "kind": "image_model",
            "accepted_license_id": "unknown",
        },
        {
            "repo_id": "org/image-base",
            "kind": "image_model",
            "accepted_license_id": "MIT",
            "trust_remote_code": True,
        },
        {"repo_id": "org/adapter", "kind": "image_lora", "accepted_license_id": "MIT"},
        {"repo_id": "org/control", "kind": "control_model", "accepted_license_id": "MIT"},
        {
            "repo_id": "org/image-base",
            "kind": "image_model",
            "accepted_license_id": "MIT",
            "compatible_base_model_id": str(base_id),
        },
    ):
        with pytest.raises(ValidationError):
            ImageAssetAcquireRequest.model_validate(payload)


async def test_auxiliary_intake_refuses_an_unready_base_before_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, query: object) -> Result:
            return Result()

        async def get(self, model: type[object], identity: object) -> ModelRow:
            assert model is ModelRow
            return ModelRow(id=identity, kind=ModelKind.IMAGE_MODEL, state=ModelState.DOWNLOADING)

    class Client:
        async def inspect(self, node: str, repo_id: str) -> RepoInspection:
            pytest.fail("unready base reached upstream inspection")

    audits: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(image_acquisition, "write_audit", audit)
    request = ImageAssetAcquireRequest(
        repo_id="org/adapter",
        kind=ModelKind.IMAGE_LORA,
        accepted_license_id="apache-2.0",
        compatible_base_model_id=uuid.uuid4(),
    )
    with pytest.raises(RegistryError, match="compatible_base_unavailable"):
        await image_acquisition.submit_image_asset(
            Session(),  # type: ignore[arg-type]
            request,
            client=Client(),  # type: ignore[arg-type]
            settings=Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            views=[],
            actor="admin:test",
        )
    assert audits[-1]["detail"] == {"reason": "compatible_base_unavailable"}


async def test_admin_image_intake_pins_revision_and_licence_before_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [NodeRow(id=uuid.uuid4(), name=name) for name in ("coire-edge-a", "coire-edge-b")]

    class Result:
        def __init__(self, items: list[object]) -> None:
            self.items = items

        def scalar_one_or_none(self) -> object | None:
            return self.items[0] if self.items else None

        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return self.items

    class Session:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.queries = 0

        async def execute(self, query: object) -> Result:
            self.queries += 1
            return Result([] if self.queries == 1 else list[object](nodes))

        async def flush(self) -> None:
            pass

        def add(self, row: object) -> None:
            self.added.append(row)

    class Client:
        async def inspect(self, node: str, repo_id: str) -> RepoInspection:
            assert node == "coire-edge-a" and repo_id == "org/image-base"
            return _repo()

    audits: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(image_acquisition, "write_audit", audit)
    views = [
        NodeView(
            name=node.name,
            reachability=Reachability.HEALTHY,
            store_free_bytes=10**12,
            memory_budget_bytes=10**12,
        )
        for node in nodes
    ]
    session = Session()
    model, job = await image_acquisition.submit_image_asset(
        session,  # type: ignore[arg-type]
        ImageAssetAcquireRequest(
            repo_id="org/image-base",
            kind=ModelKind.IMAGE_MODEL,
            accepted_license_id="apache-2.0",
        ),
        client=Client(),  # type: ignore[arg-type]
        settings=Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        views=views,
        actor="admin:test",
    )
    assert isinstance(model, ModelRow) and isinstance(job, DownloadJobRow)
    assert model.source_revision == "a" * 40
    assert model.license_id == "apache-2.0"
    assert model.kind is ModelKind.IMAGE_MODEL
    assert model.backend == EngineBackend.MFLUX.value
    assert model.total_bytes == 1124
    assert model.memory_estimate_bytes == 16 * 1024**3 + 3072
    assert job.origin_node_id == nodes[0].id
    assert job.replica_node_id == nodes[1].id
    assert job.expected_files == {
        "config.json": {"bytes": 100, "upstream_sha256": None},
        "model.safetensors": {"bytes": 1024, "upstream_sha256": "b" * 64},
    }
    assert audits[0]["detail"]["license_id"] == "apache-2.0"  # type: ignore[index]

    class SessionWithBase(Session):
        async def get(self, model_type: type[object], identity: object) -> ModelRow:
            assert model_type is ModelRow and identity == model.id
            model.state = ModelState.READY
            model.image_capability_profile = {"modes": ["txt2img"]}
            model.manifest_sha256 = "c" * 64
            return model

    class AdapterClient:
        async def inspect(self, node: str, repo_id: str) -> RepoInspection:
            assert node == "coire-edge-a" and repo_id == "org/adapter"
            return _repo(repo_id=repo_id)

    adapter, _adapter_job = await image_acquisition.submit_image_asset(
        SessionWithBase(),  # type: ignore[arg-type]
        ImageAssetAcquireRequest(
            repo_id="org/adapter",
            kind=ModelKind.IMAGE_LORA,
            accepted_license_id="apache-2.0",
            compatible_base_model_id=model.id,
        ),
        client=AdapterClient(),  # type: ignore[arg-type]
        settings=Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        views=views,
        actor="admin:test",
    )
    assert adapter.capability_profile == {"compatible_base_model_id": str(model.id)}
    assert adapter.memory_estimate_bytes == model.memory_estimate_bytes + 3072

    one_low_node = [
        NodeView(
            name=node.name,
            reachability=Reachability.HEALTHY,
            store_free_bytes=10**12,
            memory_budget_bytes=16 * 1024**3 if index == 0 else 10**12,
        )
        for index, node in enumerate(nodes)
    ]
    with pytest.raises(RegistryError, match="insufficient_memory"):
        await image_acquisition.submit_image_asset(
            Session(),  # type: ignore[arg-type]
            ImageAssetAcquireRequest(
                repo_id="org/image-base",
                kind=ModelKind.IMAGE_MODEL,
                accepted_license_id="apache-2.0",
            ),
            client=Client(),  # type: ignore[arg-type]
            settings=Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            views=one_low_node,
            actor="admin:test",
        )
    assert audits[-1]["detail"] == {"reason": "insufficient_memory"}


async def test_seedvr2_intake_excludes_unselected_7b_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [NodeRow(id=uuid.uuid4(), name=name) for name in ("coire-edge-a", "coire-edge-b")]
    files = [
        RepoFile(path=name, bytes=size, upstream_sha256="b" * 64)
        for name, size in (
            ("seedvr2_ema_3b_fp16.safetensors", 1024),
            ("ema_vae_fp16.safetensors", 1024),
            ("seedvr2_ema_7b_fp16.safetensors", 2048),
        )
    ]
    repo = _repo(
        repo_id="numz/SeedVR2_comfyUI",
        files=files,
        total_bytes=4096,
        weight_bytes=4096,
    )

    class Result:
        def __init__(self, items: list[object]) -> None:
            self.items = items

        def scalar_one_or_none(self) -> object | None:
            return None

        def scalars(self) -> Result:
            return self

        def all(self) -> list[object]:
            return self.items

    class Session:
        def __init__(self) -> None:
            self.queries = 0

        async def execute(self, query: object) -> Result:
            self.queries += 1
            return Result([] if self.queries == 1 else list[object](nodes))

        async def flush(self) -> None:
            pass

        def add(self, row: object) -> None:
            pass

    class Client:
        async def inspect(self, node: str, repo_id: str) -> RepoInspection:
            assert node == "coire-edge-a" and repo_id == repo.repo_id
            return repo

    async def audit(session: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(image_acquisition, "write_audit", audit)
    views = [
        NodeView(
            name=node.name,
            reachability=Reachability.HEALTHY,
            store_free_bytes=10**12,
            memory_budget_bytes=10**12,
        )
        for node in nodes
    ]
    model, job = await image_acquisition.submit_image_asset(
        Session(),  # type: ignore[arg-type]
        ImageAssetAcquireRequest(
            repo_id=repo.repo_id,
            kind=ModelKind.UPSCALE_MODEL,
            accepted_license_id="apache-2.0",
        ),
        client=Client(),  # type: ignore[arg-type]
        settings=Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
        views=views,
        actor="admin:test",
    )
    assert model.weight_bytes == model.total_bytes == 2048
    assert model.memory_estimate_bytes == 6144
    assert job.expected_files is not None
    assert set(job.expected_files) == {
        "seedvr2_ema_3b_fp16.safetensors",
        "ema_vae_fp16.safetensors",
    }


async def test_admin_licence_mismatch_is_audited_before_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, query: object) -> Result:
            return Result()

    class Client:
        async def inspect(self, node: str, repo_id: str) -> RepoInspection:
            return _repo()

    audits: list[dict[str, object]] = []

    async def audit(session: object, **kwargs: object) -> None:
        audits.append(kwargs)

    monkeypatch.setattr(image_acquisition, "write_audit", audit)
    views = [
        NodeView(name=name, reachability=Reachability.HEALTHY, store_free_bytes=10**12)
        for name in ("coire-edge-a", "coire-edge-b")
    ]
    with pytest.raises(RegistryError, match="licence_review_mismatch"):
        await image_acquisition.submit_image_asset(
            Session(),  # type: ignore[arg-type]
            ImageAssetAcquireRequest(
                repo_id="org/image-base",
                kind=ModelKind.IMAGE_MODEL,
                accepted_license_id="mit",
            ),
            client=Client(),  # type: ignore[arg-type]
            settings=Settings(_secrets_dir="/nonexistent"),  # type: ignore[call-arg]
            views=views,
            actor="admin:test",
        )
    assert len(audits) == 1 and audits[0]["outcome"] == "refused"


def test_image_copy_manifest_must_match_inspection_and_origin() -> None:
    model = ModelRow(
        id=uuid.uuid4(),
        slug="org--image-base",
        repo_id="org/image-base",
        source_revision="a" * 40,
        kind=ModelKind.IMAGE_MODEL,
    )
    job = DownloadJobRow(
        id=uuid.uuid4(),
        expected_files={
            "config.json": {"bytes": 2, "upstream_sha256": None},
            "model.safetensors": {"bytes": 4, "upstream_sha256": "b" * 64},
        },
    )
    manifest = ChecksumManifest(
        slug=model.slug,
        repo_id=model.repo_id,
        revision=model.source_revision or "",
        files=[
            ManifestFile(path="config.json", bytes=2, sha256="a" * 64),
            ManifestFile(
                path="model.safetensors",
                bytes=4,
                sha256="b" * 64,
                upstream_sha256="b" * 64,
            ),
        ],
        total_bytes=6,
        created_at=datetime.now(UTC),
    )
    status = JobStatus(
        job_id=job.id,
        kind=JobKind.PULL,
        slug=model.slug,
        stage=JobStage.DONE,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
    )
    assert image_acquisition.copy_manifest_valid(model, job, status)
    replica = status.model_copy(update={"kind": JobKind.IMPORT})
    assert image_acquisition.copy_manifest_valid(
        model, job, replica, origin_digest=manifest.sha256()
    )
    for changed in (
        status.model_copy(update={"manifest_sha256": "0" * 64}),
        status.model_copy(update={"manifest": manifest.model_copy(update={"revision": "c" * 40})}),
        status.model_copy(
            update={
                "manifest": manifest.model_copy(
                    update={
                        "files": [
                            *manifest.files,
                            ManifestFile(path="evil.pkl", bytes=0, sha256="c" * 64),
                        ]
                    }
                )
            }
        ),
    ):
        assert not image_acquisition.copy_manifest_valid(model, job, changed)
    assert not image_acquisition.copy_manifest_valid(model, job, replica, origin_digest="0" * 64)
