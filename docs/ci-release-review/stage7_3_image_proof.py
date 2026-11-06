"""Собирает exact candidate image без runtime и проверяет synthetic Linux boundary."""

import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("admission", HERE / "stage7_3_linux_admission.py")
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)
ROOT = "/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z"

BUILD = r'''
import base64,datetime,hashlib,io,json,os,pathlib,subprocess,tarfile
root=pathlib.Path(config['root'])
assert root==pathlib.Path('/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z')
assert root.resolve()==root and root.stat().st_uid==0 and root.stat().st_mode & 0o077==0
receipt=json.loads((root/'ci-receipt.json').read_text())
assert receipt['candidate_sha']==config['sha'] and receipt['full_ci_success'] and receipt['linux_native_verifier']
assert receipt['snapshot_digest']==config['snapshot_digest']
assert __import__('shutil').disk_usage(root).free > 2*1024**3
payload=base64.b64decode(config['context'])
assert hashlib.sha256(payload).hexdigest()==config['context_digest']
os.umask(0o077)
context=root/'build-context'
context.mkdir(mode=0o700,exist_ok=True)
assert context.resolve()==context and not (root/'candidate.image-id').exists()
(root/'build-context.tar').write_bytes(payload)
with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
    members=archive.getmembers()
    assert len(members)<2000 and sum(m.size for m in members)<32*1024*1024
    for member in members:
        path=pathlib.PurePosixPath(member.name)
        assert not path.is_absolute() and '..' not in path.parts
        assert member.isdir() or member.isfile()
        assert member.name in ('Dockerfile','requirements.txt','compadre_assistant','document_chunker') or member.name.startswith(('compadre_assistant/','document_chunker/'))
        destination=context/member.name
        if member.isdir():
            destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        else:
            destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            content=archive.extractfile(member).read()
            assert destination.resolve()==destination
            if destination.exists():
                assert destination.read_bytes()==content
            else:
                destination.write_bytes(content)
env={'PATH':'/usr/bin:/bin','HOME':str(root),'DOCKER_HOST':'unix:///var/run/docker.sock','DOCKER_CONFIG':str(root/'unused-build-docker-config')}
tag='compadre-stage7:'+config['sha']
with (root/'build.log').open('w') as log:
    result=subprocess.run(['/usr/bin/docker','build','--label','org.opencontainers.image.revision='+config['sha'],
                           '--tag',tag,'--iidfile',str(root/'candidate.image-id'),str(context)],
                          env=env,stdout=log,stderr=subprocess.STDOUT,check=False,timeout=600)
assert result.returncode==0,'PRIVATE_BUILD_FAILED'
image_id=(root/'candidate.image-id').read_text().strip()
image=json.loads(subprocess.run(['/usr/bin/docker','image','inspect',image_id],env=env,capture_output=True,text=True,check=True).stdout)[0]
assert image['Id']==image_id and image['Config']['Labels']['org.opencontainers.image.revision']==config['sha']
assert image['Config']['WorkingDir']=='/app' and not image['Config'].get('Volumes')
allowed={'PATH','LANG','GPG_KEY','PYTHON_VERSION','PYTHON_SHA256','PYTHONDONTWRITEBYTECODE','PYTHONUNBUFFERED'}
assert all(entry.split('=',1)[0] in allowed for entry in image['Config'].get('Env',[]))
result={'phase':'private-image-build','utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'candidate_sha':config['sha'],'image_id':image_id,'oci_revision':image['Config']['Labels']['org.opencontainers.image.revision'],
        'tag':tag,'context_sha256':config['context_digest'],'containers_started':False,'health_or_migration_executed':False}
(root/'image-receipt.json').write_text(json.dumps(result,sort_keys=True)+'\n')
print(json.dumps(result,sort_keys=True),flush=True)
'''

SYNTHETIC = r'''
import base64,datetime,hashlib,importlib.util,json,os,pathlib,shutil,sqlite3,sys,uuid
root=pathlib.Path(config['root'])
assert root==pathlib.Path('/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z')
os.umask(0o077)
source=base64.b64decode(config['harness'])
assert hashlib.sha256(source).hexdigest()==config['harness_digest']
(root/'harness.py').write_bytes(source)
spec=importlib.util.spec_from_file_location('proof_harness',root/'harness.py')
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)
sys.path.insert(0,str(root/'control'))
from compadre_assistant.core.data.sqlite import migration_runner,runtime_schema
case=root/('synthetic-cabc0e2-'+uuid.uuid4().hex)
case.mkdir(mode=0o700)
(case/'fixtures').mkdir(mode=0o700)
old_sql=case/'previous-sql';old_sql.mkdir(mode=0o700)
sql=root/'build-context/compadre_assistant/core/data/sqlite/migrations'
migrations=migration_runner._discover_migrations(sql)
shutil.copyfile(migrations[0].path,old_sql/migrations[0].name)
database=case/'fixtures/source.db'
connection=sqlite3.connect(database)
migration_runner.apply_migrations(connection,old_sql)
connection.execute("INSERT INTO app_users(display_name) VALUES('synthetic seed')")
connection.commit();connection.close()
candidate=case/'candidate-schema.db';shutil.copyfile(database,candidate)
connection=sqlite3.connect(candidate);migration_runner.apply_migrations(connection,sql)
fingerprint=runtime_schema.schema_fingerprint(connection);connection.close()
ci=json.loads((root/'ci-receipt.json').read_text());image=json.loads((root/'image-receipt.json').read_text())
receipt={'candidate_sha':config['sha'],'previous_sha':ci['previous_sha'],'run_id':ci['run_id'],'run_attempt':ci['run_attempt'],
    'full_ci_success':ci['full_ci_success'],'repository':'karateka-git/compadre-assistant','synthetic':True,
    'source_digest':harness.digest(database),'image_id':image['image_id'],
    'previous_image_id':'sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8',
    'schema_fingerprint':fingerprint,'snapshot_digest':ci['snapshot_digest'],'policy_sha':ci['policy_sha'],
    'artifact_digest':ci['artifact_digest'],'artifact_id':ci['artifact_id'],'nonce':uuid.uuid4().hex,'control':str(root/'control'),
    'schema_before':[migrations[0].name],'schema_after':[item.name for item in migrations]}
def migrate(private_root,path,image_id,sha):
    assert private_root==case and sha==config['sha'] and image_id==image['image_id']
    connection=sqlite3.connect(path)
    migration_runner.apply_migrations(connection,sql)
    connection.close()
try:
    result=harness.run_case(case,receipt,migrate)
except Exception as error:
    text=(case/'controller.log').read_text() if (case/'controller.log').exists() else ''
    print(json.dumps({'phase':'linux-synthetic-failed','error_type':type(error).__name__,'private_case':str(case),
                      'last_child_error':text.splitlines()[-1:]},sort_keys=True),flush=True)
    raise SystemExit(1)
assert result['synthetic'] and result['real_flock'] and result['model_starts']==1
assert fingerprint==ci['schema_fingerprint']
result.update(phase='linux-synthetic-proof',utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),private_case=str(case))
(root/'synthetic-receipt.json').write_text(json.dumps(result,sort_keys=True)+'\n')
print(json.dumps(result,sort_keys=True),flush=True)
'''


def main():
    """Выполняет build либо synthetic proof между readonly baseline проверками."""
    repository = HERE.parents[1]
    subprocess.run(["git", "status", "--short"], cwd=repository, capture_output=True, check=True)
    if sys.argv[1:] == ["build"]:
        payload = subprocess.run(["git", "archive", "--format=tar", admission.SHA, "Dockerfile", "requirements.txt",
                                  "compadre_assistant", "document_chunker"], cwd=repository, capture_output=True, check=True).stdout
        config = {"root": ROOT, "sha": admission.SHA, "snapshot_digest": admission.DIGEST,
                  "context": base64.b64encode(payload).decode("ascii"), "context_digest": hashlib.sha256(payload).hexdigest()}
        code = BUILD
    elif sys.argv[1:] == ["synthetic"]:
        payload = (HERE / "stage7_3_harness.py").read_bytes()
        config = {"root": ROOT, "sha": admission.SHA, "harness": base64.b64encode(payload).decode("ascii"),
                  "harness_digest": hashlib.sha256(payload).hexdigest()}
        code = SYNTHETIC
    else:
        raise SystemExit("Только build или synthetic; реального case в CLI нет")
    admission.remote(admission.PREFLIGHT)
    try:
        admission.remote("config=" + repr(config) + "\n" + code)
    finally:
        admission.remote(admission.PREFLIGHT)


if __name__ == "__main__":
    main()
