"""Current-admin analysis status includes pending identities without fabricated statistics."""

import uuid

from fastapi import APIRouter, Request

from coire_api.db import TrainingDatasetAnalysisRow, session_scope
from coire_api.training.authorization import CurrentTrainingAdmin, authorize_live_training_action
from coire_core.errors import TrainingNotFound, TrainingUnavailable
from coire_core.models.datasets import DatasetAnalysis

router = APIRouter(prefix="/api/v1/admin/dataset-analyses", tags=["admin:datasets"])


@router.get("/{analysis_id}", response_model=DatasetAnalysis)
async def analysis_detail(
    request: Request, analysis_id: uuid.UUID, principal: CurrentTrainingAdmin
) -> DatasetAnalysis:
    if not request.app.state.settings.training_enabled:
        raise TrainingUnavailable("Training datasets are disabled")
    async with session_scope() as session:
        await authorize_live_training_action(session, principal)
        row = await session.get(TrainingDatasetAnalysisRow, analysis_id)
        if row is None:
            raise TrainingNotFound()
        if row.result is not None and "dispatch" not in row.result:
            return DatasetAnalysis.model_validate(row.result)
        return DatasetAnalysis.model_validate(
            {
                "id": str(row.id),
                "dataset_id": str(row.dataset_id),
                "model_id": str(row.model_id),
                "variant_id": str(row.variant_id),
                "state": row.state,
                "created_at": row.created_at.isoformat(),
            }
        )
