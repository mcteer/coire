"""FastAPI dependencies for the node agent.

The agent's collaborators — store, jobs, engines, grants — are built once in `serve()` and
attached to `app.state`. These getters are async because a pure in-memory lookup
must not dispatch a worker thread for every authenticated poll. Routes reach them through these aliases so a test can build an app
with fakes without monkeypatching module globals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from coire_core.settings import Settings

if TYPE_CHECKING:
    from coire_node.engines import EngineManager
    from coire_node.grants import Grants
    from coire_node.image_dispatch import ImageNodeDispatcher
    from coire_node.image_runtime.supervisor import ImageProcessSupervisor
    from coire_node.jobs import JobSupervisor
    from coire_node.reservations import ReservationLedger
    from coire_node.store import Store


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def get_store(request: Request) -> Store:
    return request.app.state.store  # type: ignore[no-any-return]


async def get_jobs(request: Request) -> JobSupervisor:
    return request.app.state.jobs  # type: ignore[no-any-return]


async def get_grants(request: Request) -> Grants:
    return request.app.state.grants  # type: ignore[no-any-return]


async def get_engines(request: Request) -> EngineManager:
    return request.app.state.engines  # type: ignore[no-any-return]


async def get_image_workers(request: Request) -> ImageProcessSupervisor:
    return request.app.state.image_workers  # type: ignore[no-any-return]


async def get_image_dispatcher(request: Request) -> ImageNodeDispatcher:
    return request.app.state.image_dispatcher  # type: ignore[no-any-return]


async def get_reservations(request: Request) -> ReservationLedger:
    return request.app.state.reservations  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings)]
StoreDep = Annotated["Store", Depends(get_store)]
JobsDep = Annotated["JobSupervisor", Depends(get_jobs)]
GrantsDep = Annotated["Grants", Depends(get_grants)]
EngineDep = Annotated["EngineManager", Depends(get_engines)]
ImageWorkersDep = Annotated["ImageProcessSupervisor", Depends(get_image_workers)]
ImageDispatcherDep = Annotated["ImageNodeDispatcher", Depends(get_image_dispatcher)]
ReservationsDep = Annotated["ReservationLedger", Depends(get_reservations)]
