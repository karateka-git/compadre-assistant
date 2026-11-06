"""Проверяет exact CI evidence настоящим Linux verifier в новом private каталоге.

Не запускает deploy/runtime/migration/Docker containers и не останавливает ботов.
Metadata читаются публичным GitHub API. Единственный фиксированный artifact
получается локальным gh и передаётся как bytes с SHA256; token на VPS не попадает.
"""

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys


SHA = "cabc0e24d28947123868db8a60f6b55095feb63e"
DIGEST = "578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca"
POLICY = "eb1dad4fee8a176ec4870db12da5e1051a3d1c6e"
ARTIFACT_ID = 13519278785
ARTIFACT_DIGEST = "sha256:87a5ab53bcd8b7f57a2012c279872e9252c74ebfc33ddc0f3fbd0099b2e77c6c"
PARENT = "/var/lib/compadre-assistant-stage7-20261106"

PREFLIGHT = r'''
import datetime, hashlib, json, pathlib, subprocess
def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()
def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
checkout='/opt/obs-chat-bot'
status=run('git','-c','safe.directory='+checkout,'-C',checkout,'status','--porcelain')
head=run('git','-c','safe.directory='+checkout,'-C',checkout,'rev-parse','HEAD')
assert not status and head=='dd71c2ba42a0744b5b654cedcf73009ce8879058'
ids=run('docker','ps','--all','--no-trunc','-q').splitlines()
assert set(ids)=={'2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42','31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00'}
containers=[]
for container in json.loads(run('docker','inspect',*ids)):
    item={'id':container['Id'],'image':container['Image'],'running':container['State']['Running'],
          'health':container['State']['Health']['Status'],'restarts':container['RestartCount'],
          'policy':container['HostConfig']['RestartPolicy']['Name']}
    assert item['image']=='sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8'
    assert item['running'] and item['health']=='healthy' and item['restarts']==0 and item['policy']=='unless-stopped'
    containers.append(item)
units={unit:run('systemctl','show','--property=ActiveState','--value',unit)
       for unit in ('obs-chat-bot.service','obs-chat-bot-backup.timer','obs-chat-bot-backup.service')}
assert units=={'obs-chat-bot.service':'active','obs-chat-bot-backup.timer':'active','obs-chat-bot-backup.service':'inactive'}
state=pathlib.Path('/var/lib/compadre-assistant-deploy')
assert all(not (state/name).exists() for name in ('deploy-in-progress','active-db.json','controller.sha','recovery-blocked.json'))
assert sha(state/'state.json')=='fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317'
fixture=pathlib.Path('/var/lib/compadre-assistant-stage7-20261106/fixtures/source.db')
assert sha(fixture)=='5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c'
print(json.dumps({'phase':'readonly-preflight','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'checkout_sha':head,'clean':True,'containers':containers,'units':units,'marker':False,
                 'state_sha256':sha(state/'state.json'),'fixture_sha256':sha(fixture)}),flush=True)
'''

VERIFY = r'''
import base64, datetime, hashlib, io, json, os, pathlib, stat, sys, tarfile, tempfile, types
root=pathlib.Path(config['root'])
assert root.parent==pathlib.Path('/var/lib/compadre-assistant-stage7-20261106')
assert root.name.startswith('admission-cabc0e2-') and root.parent.resolve()==root.parent
assert root.parent.stat().st_uid==0 and root.parent.stat().st_mode & 0o077==0
payload=base64.b64decode(config['archive'])
assert hashlib.sha256(payload).hexdigest()==config['digest']
artifact=base64.b64decode(config['artifact'])
assert len(artifact)<=64*1024*1024 and 'sha256:'+hashlib.sha256(artifact).hexdigest()==config['artifact_digest']
os.umask(0o077)
root.mkdir(mode=0o700)
assert root.resolve()==root
(root/'tmp').mkdir(mode=0o700)
tempfile.tempdir=str(root/'tmp')
protected=tuple(pathlib.Path(p) for p in ('/opt/obs-chat-bot','/var/lib/compadre-assistant-deploy',
               '/usr/local/lib/compadre-assistant-deploy','/etc/systemd','/run/docker.sock','/var/run/docker.sock'))
def check(value, writing=False):
    if isinstance(value,int):
        raise PermissionError('ADMISSION_DESCRIPTOR')
    path=pathlib.Path(value).absolute().resolve()
    if any(path==p or path.is_relative_to(p) for p in protected):
        raise PermissionError('ADMISSION_PRODUCTION_PATH')
    if writing and not path.is_relative_to(root):
        raise PermissionError('ADMISSION_EXTERNAL_WRITE')
def audit(event,args):
    if event.startswith('subprocess.') or event in ('os.system','os.exec','os.spawn','os.fork','os.posix_spawn','sqlite3.connect'):
        raise PermissionError('ADMISSION_EXTERNAL_OPERATION')
    if event=='open':
        check(args[0], bool(args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)))
    elif event in ('os.mkdir','os.remove','os.rmdir','os.chmod','os.chown','os.utime','os.truncate'):
        check(args[0],True)
    elif event in ('os.rename','os.link','os.symlink'):
        check(args[0],True); check(args[1],True)
sys.addaudithook(audit)
with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
    member=archive.getmember('scripts/controller_snapshot.py')
    module=types.ModuleType('snapshot_bootstrap')
    exec(compile(archive.extractfile(member).read(),'verified-snapshot-builder','exec'),module.__dict__)
(root/'candidate.tar').write_bytes(payload)
(root/'compatibility.zip').write_bytes(artifact)
manifest=module.unpack(root/'candidate.tar',config['sha'],config['digest'],root/'control')
module.verify_directory(root/'control',manifest,require_root=True)
sys.path.insert(0,str(root/'control'))
from scripts.predeploy_compatibility import EvidenceAPI,verify_evidence
from scripts.select_deploy_release import SelectedRelease
selected=SelectedRelease('karateka-git/compadre-assistant','stage7-model','main',config['sha'],0,'main-full.yml',42249037867,1)
class FixedArtifactAPI(EvidenceAPI):
    def download(self,artifact_id):
        if artifact_id != 13519278785:
            raise ValueError('ADMISSION_UNEXPECTED_ARTIFACT')
        return artifact
api=FixedArtifactAPI(selected.repository,'',allow_anonymous=True)
evidence=verify_evidence(api,selected,config['policy'],'dd71c2ba42a0744b5b654cedcf73009ce8879058')
run=api.get('/actions/runs/42249037867/attempts/1')
assert run['status']=='completed' and run['conclusion']=='success' and run['head_sha']==config['sha']
result={'phase':'linux-native-admission','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'candidate_sha':config['sha'],'previous_sha':'dd71c2ba42a0744b5b654cedcf73009ce8879058',
        'run_id':42249037867,'run_attempt':1,'full_ci_success':True,'linux_native_verifier':True,
        'controller_sha':manifest['controller_sha'],'snapshot_digest':config['digest'],'snapshot_files':len(manifest['files']),
        'image_verified':False,'release_verified':False,'root':str(root),**evidence}
result['artifact_transport']='fixed ZIP bytes from local gh; digest verified against live public metadata'
(root/'ci-receipt.json').write_text(json.dumps(result,sort_keys=True)+'\n',encoding='utf-8')
os.chmod(root/'ci-receipt.json',0o600)
print(json.dumps(result,sort_keys=True),flush=True)
'''


def remote(code):
    """Передаёт только проверочный код/архив, возвращает безопасный JSON output."""
    result = subprocess.run(["ssh", "-i", str(Path.home() / "Downloads/SSH/H3LLO_CLOUD/karateka"),
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "user@91.188.213.142",
        "sudo /usr/bin/python3 -I -B -"], input=code.encode("utf-8"), capture_output=True, timeout=360)
    print(result.stdout.decode("utf-8"), end="", flush=True)
    if result.returncode:
        # Внешние HTTP responses и traceback не печатаются: может встретиться
        # credential-bearing URL redirect. Operator получает только этап/rc.
        last = result.stderr.decode("utf-8", errors="replace").splitlines()[-1:]
        category = last[0].split(":", 1)[0] if last else "unknown"
        if re.fullmatch(r"[A-Za-z.]+(?:Error|Exception)", category) is None:
            category = "unknown"
        raise RuntimeError(f"Private admission remote rc={result.returncode}, category={category}")


def main():
    """Повторяет readonly baseline до/после одного native evidence verifier."""
    archive = Path.home() / "AppData/Local/Temp/stage7-3-cabc0e2.tar"
    payload = archive.read_bytes()
    if hashlib.sha256(payload).hexdigest() != DIGEST:
        raise ValueError("Локальный snapshot изменился")
    artifact = subprocess.run(["gh", "api", f"repos/karateka-git/compadre-assistant/actions/artifacts/{ARTIFACT_ID}/zip"],
                              check=True, capture_output=True).stdout
    if len(artifact) > 64 * 1024 * 1024 or "sha256:" + hashlib.sha256(artifact).hexdigest() != ARTIFACT_DIGEST:
        raise ValueError("Фиксированный artifact digest не совпадает")
    remote(PREFLIGHT)
    config = {"sha": SHA, "digest": DIGEST, "policy": POLICY,
              "root": PARENT + "/admission-cabc0e2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "archive": base64.b64encode(payload).decode("ascii")}
    config.update(artifact=base64.b64encode(artifact).decode("ascii"),artifact_digest=ARTIFACT_DIGEST)
    try:
        remote("config=" + repr(config) + "\n" + VERIFY)
    finally:
        remote(PREFLIGHT)


if __name__ == "__main__":
    main()
