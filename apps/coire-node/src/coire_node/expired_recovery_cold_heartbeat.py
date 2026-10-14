"""Source readiness publication while cold serving checks keep maintenance held."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import copy_context
from datetime import UTC, datetime

import httpx
import psutil

from coire_core.errors import CoireError, UpgradeUnavailable
from coire_core.models.upgrade_recovery import UpgradeExpiredRecoveryColdSourceReceipt
from coire_core.models.upgrades import NativeGuardStatus
from coire_core.settings import Settings
from coire_node.expired_recovery_cold_source import stores
from coire_node.recovery_lease import require_recovery_lease
from coire_node.upgrade_guard import NativeGuardTransport


@contextmanager
def cold_readiness_heartbeat(
    settings: Settings, source: UpgradeExpiredRecoveryColdSourceReceipt
) -> Iterator[None]:
    """Metadata only, no serving qualification or native workflow progress.

    Stage producers independently verify full immutable graphs and physical
    Source ownership. The heartbeat reauthorizes the actual support lifetime
    before publishing its fixed selected Source; it never renews a deadline.
    Stop and join before preparing handback so it cannot overwrite Source's
    later publisher. A failed publisher prevents further pipeline effects.
    """
    effect = source.projection
    owner = effect.owner
    bootstrap, run, _ = stores(settings, owner)
    stopped = threading.Event()
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=5.0) as client:
                control = NativeGuardTransport(settings, client)
                while not stopped.is_set():
                    require_recovery_lease(bootstrap, owner.sha256())
                    if (
                        os.getpid() != owner.process.pid
                        or psutil.Process(os.getpid()).create_time() != owner.process.create_time_s
                        or run.read(
                            "cold.source.receipt.json", UpgradeExpiredRecoveryColdSourceReceipt
                        )
                        != source
                        or control.verify_cold_recovery_owner(owner) != owner
                    ):
                        raise UpgradeUnavailable(
                            "Cold heartbeat actual owner or live authority differs"
                        )
                    if stopped.is_set():
                        return
                    if datetime.now(UTC) >= owner.intent.deadline_at:
                        raise UpgradeUnavailable("Cold heartbeat captured root expired")
                    require_recovery_lease(bootstrap, owner.sha256())
                    bootstrap.write(
                        "status.json",
                        NativeGuardStatus(
                            node=owner.original_claim.node,
                            bootstrap_runtime=owner.runtime,
                            process=owner.process,
                            selected=effect.source_selection,
                            ready=True,
                            observed_at=datetime.now(UTC),
                        ),
                    )
                    stopped.wait(1.0)
        except (CoireError, OSError, ValueError, psutil.Error) as error:
            errors.append(error)
            stopped.set()

    context = copy_context()
    thread = threading.Thread(
        target=lambda: context.run(publish), name="coire-cold-readiness", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=6.0)
        if thread.is_alive():
            raise UpgradeUnavailable("Cold heartbeat did not retire before handback")
        if errors:
            raise UpgradeUnavailable(
                "Cold heartbeat live authority or publication failed"
            ) from None
