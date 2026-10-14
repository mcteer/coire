"""Operate only explicitly provisioned, private native acceptance owners."""

import json
import os
import platform
import shlex
import subprocess
from pathlib import Path

from coire_core.models.upgrades import UpgradeRelease

_BLOCK_CANDIDATE = """
import json, os, sys, time
from pathlib import Path
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeNodeProcess
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_guard import _owned_node_process
root=Path(sys.argv[1]);target=sys.argv[2]
records=PrivateUpgradeStore(root/'fixture-records',create=False)
state=PrivateUpgradeStore(root/'state/upgrades',create=False)
limit=time.monotonic()+1800
while records.path('block-candidate').exists() and time.monotonic()<limit:
 try:
  node=state.read('node-process.json',NativeNodeProcess)
  if node is not None and node.selected.runtime.environment_sha256==target:
   python=root/'envs'/node.selected.runtime.environment_id/'bin/python3'
   process=_owned_node_process(node.process,python)
   if process is not None:
    process.kill()
    print(json.dumps({'candidate_pid':node.process.pid,'create_time':node.process.create_time_s}),flush=True)
 except UpgradeUnavailable:
  pass
 time.sleep(0.05)
"""

_REMOTE = """
import json, os, sys, subprocess
from pathlib import Path
import psutil
from coire_core.errors import UpgradeUnavailable
from coire_core.models.upgrades import NativeRecoveryIntent, NativeGuardStatus
from coire_core.upgrade_journal import PrivateUpgradeStore
from coire_node.upgrade_guard import _owned_native_process
body=json.load(sys.stdin)
root=Path(body['root'])
assert root.is_absolute() and root.resolve()==root and root.stat().st_uid==os.getuid()
assert root.name.startswith('native-upgrade-lab-') and root.parent.name=='coire-019-acceptance'
records=PrivateUpgradeStore(root/'fixture-records')
if body['action'] in ('block', 'unblock'):
 path=records.path('block-candidate')
 if body['action']=='block':
  fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
  # Exercise actual candidate process loss even with an adopted KeepAlive
  # owner. Never patch its sealed runtime or rely on a supervisor test hook.
  script=records.path('candidate-listener-fault.py')
  with script.open('w') as output:script.chmod(0o600);output.write(body['block_script'])
  log=records.path('candidate-listener-fault.log')
  with log.open('ab') as output:
   log.chmod(0o600)
   subprocess.Popen([sys.executable,'-I','-B',str(script),str(root),body['target_runtime_sha256']],stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
 else:path.unlink(missing_ok=True)
 print(json.dumps({'applied':True}))
elif body['action'] in ('restart_selected_guard', 'observe_selected_candidate'):
 guard=PrivateUpgradeStore(root/'upgrade-bootstrap')
 intent_path=guard.path('operations')/body['job_id']/'guard'
 if not intent_path.exists():print(json.dumps({'applied':False}));sys.exit(0)
 try:intent=PrivateUpgradeStore(intent_path).read('intent.json',NativeRecoveryIntent)
 except UpgradeUnavailable:print(json.dumps({'applied':False}));sys.exit(0)
 if intent is None:print(json.dumps({'applied':False}));sys.exit(0)
 assert intent.job_id==body['job_id']
 if intent.phase!='candidate_selected':print(json.dumps({'applied':False}));sys.exit(0)
 assert intent.candidate.environment_sha256==body['target_runtime_sha256']
 if body['action']=='observe_selected_candidate':
  print(json.dumps({'applied':True,'generation':intent.generation,'phase':intent.phase,'readiness_deadline_at':intent.readiness_deadline_at.isoformat()}));sys.exit(0)
 try:status=guard.read('status.json',NativeGuardStatus)
 except UpgradeUnavailable:print(json.dumps({'applied':False}));sys.exit(0)
 if status is None:print(json.dumps({'applied':False}));sys.exit(0)
 python=root/'envs'/status.bootstrap_runtime.environment_id/'bin/python3'
 process=_owned_native_process(status.process,python,'coire_node.upgrade_guard')
 assert process is not None and process.uids().real==os.getuid()
 process.terminate()
 print(json.dumps({'applied':True,'generation':intent.generation,'owned_guard_pid':process.pid,'create_time':status.process.create_time_s}))
else:raise RuntimeError('unknown native fixture operation')
"""


def _fault_command(root: Path, fixture: dict[str, object], python: str) -> list[str]:
    """CI operates only its ephemeral local runner, never an operator SSH host."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return [
            "ssh",
            "-oBatchMode=yes",
            "-oConnectTimeout=5",
            "coire-edge-a",
            shlex.join([python, "-c", _REMOTE]),
        ]
    if (
        os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("RUNNER_OS") != "macOS"
        or os.environ.get("RUNNER_ARCH") != "ARM64"
        or platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or fixture.get("native_transport") != "local"
    ):
        raise RuntimeError("Native CI faults require the isolated hosted Apple Silicon fixture")
    configured = os.environ.get("RUNNER_TEMP")
    native = fixture.get("native_root")
    if not configured or not isinstance(native, str):
        raise RuntimeError("Native CI fixture has no ephemeral owned root")
    temporary = Path(configured).resolve(strict=True)
    native_root = Path(native)
    if (
        not root.resolve(strict=True).is_relative_to(temporary)
        or not native_root.is_absolute()
        or native_root.resolve(strict=True) != native_root
        or not native_root.is_relative_to(temporary)
        or native_root.stat().st_uid != os.getuid()
        or native_root.stat().st_mode & 0o077
        or not Path(python).resolve(strict=True).is_relative_to(native_root)
    ):
        raise RuntimeError("Native CI cannot target operator roots or foreign interpreters")
    return [python, "-I", "-B", "-c", _REMOTE]


def native_fault(
    root: Path,
    fixture: dict[str, object],
    action: str,
    *,
    job_id: str | None = None,
    target_runtime_sha256: str | None = None,
) -> dict[str, object]:
    native = fixture.get("native_root")
    if not isinstance(native, str):
        raise RuntimeError("Native fault fixture has no private root")
    source = UpgradeRelease.model_validate_json((root / "source-coire-edge-a.json").read_bytes())
    if action == "block":
        target = UpgradeRelease.model_validate_json((root / "target.json").read_bytes())
        target_runtime_sha256 = target.manifest.environment_sha256
    python = native + "/envs/" + str(source.manifest.environment_id) + "/bin/python3"
    completed = subprocess.run(
        _fault_command(root, fixture, python),
        input=json.dumps(
            {
                "root": native,
                "action": action,
                "job_id": job_id,
                "target_runtime_sha256": target_runtime_sha256,
                "block_script": _BLOCK_CANDIDATE if action == "block" else None,
            }
        ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode:
        diagnostic = root / ("native-fault-" + action + "-failure.log")
        diagnostic.write_text(completed.stderr[:8192])
        diagnostic.chmod(0o600)
        raise RuntimeError("Private native fault operation failed: " + action)
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise RuntimeError("Private native fault result is malformed")
    return result
