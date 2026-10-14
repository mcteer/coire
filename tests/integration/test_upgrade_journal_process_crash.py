"""SIGKILL preserves captured Core epochs and prevents uncertain redispatch.

These are actual process/file/flock boundaries with synthetic Docker identities;
they do not claim that the Docker host itself was rebooted.
"""

import hashlib
import os
import selectors
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from upgrade_fixtures import core_plan

from coire_core.errors import UpgradeConflict
from coire_core.models.upgrades import CoreExecutorEffect, UpgradeExecutionPlan
from coire_upgrade_executor.journal import ExecutorJournal

pytestmark = pytest.mark.integration

_CHILD = """
import sys,time
from datetime import datetime,UTC
from pathlib import Path
from coire_core.models.upgrades import UpgradeExecutionPlan,CoreExecutorEffect,DeploymentOwnership
from coire_core.deploy import host_boot_identity
from coire_upgrade_executor.journal import ExecutorJournal
from coire_upgrade_executor.ownership import executor_ownership
from test_executor_journal import activation
root=Path(sys.argv[1]);boundary=sys.argv[2]
plan=UpgradeExecutionPlan.model_validate_json((root/'plan.json').read_bytes())
now=datetime.now(UTC);journal=ExecutorJournal(root/'journal',plan)
if boundary!='initial':
 journal.activation(activation(journal,now),now=now)
if boundary in ('outage','intent','dispatch','recovery'):
 journal.capture_outage(now=now)
if boundary in ('intent','dispatch'):
 window=journal.service_window('coire-api',now=now)
 effect=CoreExecutorEffect(job_id=plan.operation.job_id,generation=plan.operation.generation,
  plan_sha256=plan.authority_sha256(),effect_id='stop-api',action='stop_source',
  service='coire-api',container_id='c'*64,image_id=plan.source.manifest.services[0].image_id,
  captured_at=now,deadline=window.deadline_at)
 journal.intent(effect,now=now)
if boundary=='dispatch':
 owner=DeploymentOwnership(project='coire',owner_kind='executor',owner_identity='crash-fixture',
  boot_identity=host_boot_identity(),generation=1,job_id=plan.operation.job_id,
  operation_generation=plan.operation.generation,source_sha256=plan.operation.source_sha256)
 with executor_ownership(root/'coordination',owner,plan) as authority:
  journal.dispatch_intent('stop-api',authority,now=now)
  print('durable',flush=True)
  while True:time.sleep(1)
if boundary=='recovery':journal.capture_recovery(now=now)
print('durable',flush=True)
while True:time.sleep(1)
"""


@pytest.mark.parametrize(
    "boundary", ["initial", "activation", "outage", "intent", "dispatch", "recovery"]
)
def test_sigkill_retains_original_epoch_and_uncertain_dispatch(
    tmp_path: Path, boundary: str
) -> None:
    plan = core_plan()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json())
    plan_path.chmod(0o600)
    repo = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(repo / "tests"), str(repo / "apps/coire-upgrade-executor/tests")]
        ),
    }
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD, str(tmp_path), boundary],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            assert poll.select(timeout=20), "crash fixture did not publish its durable boundary"
            assert process.stdout.readline() == b"durable\n"
        before = {
            str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (tmp_path / "journal").rglob("*.json")
        }
        process.kill()
        assert process.wait(timeout=10) == -signal.SIGKILL
        restarted = ExecutorJournal(tmp_path / "journal", plan)
        retained = restarted.state()
        if boundary == "recovery":
            assert (
                restarted.capture_recovery(now=datetime.now(UTC) + timedelta(hours=1)) == retained
            )
        if boundary in {"intent", "dispatch"}:
            effect = restarted.effects.read("stop-api.json", CoreExecutorEffect)
            assert effect is not None
            assert restarted.intent(effect, now=datetime.now(UTC) + timedelta(hours=1)) == effect
            assert effect.outcome == ("unknown" if boundary == "dispatch" else "intent")
            with pytest.raises(UpgradeConflict):
                restarted.intent(effect.model_copy(update={"container_id": "d" * 64}))
        assert {
            str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (tmp_path / "journal").rglob("*.json")
        } == before
        assert UpgradeExecutionPlan.model_validate_json(plan_path.read_bytes()) == plan
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
