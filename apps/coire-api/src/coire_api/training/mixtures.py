"""Traced core-side entry point for the shared, model-free mixture compiler."""

import uuid
from collections.abc import Mapping, Sequence

from opentelemetry import trace

from coire_core.models.datasets import DatasetMixture, SplitManifest
from coire_core.training_data import CompiledMixture as CompiledMixture
from coire_core.training_data import CompiledMixtureSource as CompiledMixtureSource
from coire_core.training_data import compile_mixture as _compile_mixture
from coire_core.training_data import detect_split_leakage as detect_split_leakage
from coire_core.training_data import largest_remainder_quotas as largest_remainder_quotas

tracer = trace.get_tracer("coire.api.training.mixtures")


def compile_mixture(
    mixture: DatasetMixture,
    manifests: Mapping[uuid.UUID, SplitManifest],
    *,
    validation_manifests: Sequence[SplitManifest] = (),
) -> CompiledMixture:
    with tracer.start_as_current_span("coire.api.training.mixture.compile"):
        return _compile_mixture(mixture, manifests, validation_manifests=validation_manifests)
