"""Node agent entry point.

Runs as a LaunchDaemon on each Studio: `RunAtLoad` plus `KeepAlive`, started at boot with no
login session, because SC-006 requires the agent to answer within two minutes of a reboot
unattended (research R6).
"""

from __future__ import annotations

import asyncio
import logging
import signal

from coire_core.settings import Settings
from coire_node import __version__
from coire_node.agent import resolve_egress_address, resolve_mesh_address, serve
from coire_node.keychain import load_node_secrets
from coire_node.metrics import MetricsCollector
from coire_node.otel import configure_node_telemetry
from coire_node.register import Registrar, build_registration, build_registration_v2

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)


async def _run() -> None:
    import socket as _socket

    settings = Settings()
    # The Studio's two secrets live in the System keychain, which is the only place they exist
    # (feature 000 research R6; spec FR-005). Anything already set by environment or a mounted
    # file wins, so containers and CI need no keychain.
    load_node_secrets(settings)
    configure_node_telemetry(__version__, settings.otlp_endpoint)
    hostname = settings.node_name or _socket.gethostname().split(".")[0]

    collector = MetricsCollector(
        node_name=hostname,
        agent_version=__version__,
        interval_s=settings.node_collection_interval_s,
        budget_cpu_pct=settings.node_collection_budget_cpu_pct,
        budget_rss_bytes=settings.node_collection_budget_rss_bytes,
        disk_path="/",
    )
    collector.start()

    mesh = resolve_mesh_address(hostname, settings.mesh_hosts_file)
    egress = resolve_egress_address()
    registrar: Registrar | None = None
    if settings.legacy_network_mode and mesh:
        # Egress is optional: it only carries the alerted Wi-Fi fallback listener. A node with
        # no route off the mesh is a legitimate configuration and must still join the cluster.
        if egress is None:
            logger.info("no egress interface; the fallback listener will not be started")
        registrar = Registrar(settings, build_registration(settings, mesh, egress))
        await registrar.start()
    elif settings.legacy_network_mode:
        logger.error(
            "not registering: no mesh address for this host in %s. Run "
            "scripts/apply-mesh-hosts.sh.",
            settings.mesh_hosts_file,
        )
    else:
        registrar = Registrar(settings, build_registration_v2(settings))
        await registrar.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    serve_task = asyncio.create_task(serve(settings, collector))
    try:
        await asyncio.wait(
            {serve_task, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        serve_task.cancel()
        if registrar is not None:
            await registrar.stop()
        collector.stop()
        logger.info("coire-node stopped")


def main() -> None:
    logger.info("coire-node %s starting", __version__)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
