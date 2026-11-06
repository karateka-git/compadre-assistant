"""Воспроизводит приватную проверку snapshot 7.2 через отдельные SSH-процессы.

Не устанавливает production controller. Все удалённые записи выполняются
под guard; bootstrap создаёт только новый каталог с заранее заданным именем.
Запускать только после свежего read-only preflight и отдельного допуска staging.
"""

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys


CANDIDATE = "512cb57e8ac80460f71b793fb1a55819244365d9"
PREVIOUS = "dd71c2ba42a0744b5b654cedcf73009ce8879058"
DIGESTS = {
    CANDIDATE: "977ebea27322293a0d2626ed9d3c233dabaf51b2a7b6e630474730f262214654",
    PREVIOUS: "c889525aeaa02edbb5efc4a1d72d0b0b30823a6643f60e57a0b64938a9e24872",
}
# Только этот новый подкаталог разрешён bootstrap; production-пути не принимаются.
ROOT = "/var/lib/compadre-assistant-stage7-20261106/snapshot-proof-512cb57-20261106T1500Z"

REMOTE = r'''
import base64, datetime, hashlib, io, json, os, pathlib, sys, tarfile, types
root = pathlib.Path(config['root'])
assert str(root) == '/var/lib/compadre-assistant-stage7-20261106/snapshot-proof-512cb57-20261106T1500Z'
assert root.parent.resolve() == root.parent
assert root.parent.stat().st_uid == 0 and root.parent.stat().st_mode & 0o077 == 0
os.umask(0o077)
if config['phase'] == 'prepare':
    root.mkdir(mode=0o700)
assert root.resolve() == root and root.stat().st_uid == 0 and root.stat().st_mode & 0o077 == 0
raw = base64.b64decode(config['archives'][config['candidate']])
assert hashlib.sha256(raw).hexdigest() == config['digests'][config['candidate']]
with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
    guard_source = archive.extractfile('scripts/stage7_transport.py').read()
    snapshot_source = archive.extractfile('scripts/controller_snapshot.py').read()
guard = types.ModuleType('private_guard')
exec(compile(guard_source, '<verified-guard>', 'exec'), guard.__dict__)
guard.install_guard(root)
snapshot = types.ModuleType('private_snapshot')
exec(compile(snapshot_source, '<verified-snapshot>', 'exec'), snapshot.__dict__)

def emit(**values):
    """Выводит только разрешённые технические результаты без содержимого state."""
    print(json.dumps({'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'phase': config['phase'], **values}), flush=True)

def unpack(sha, destination):
    """Распаковывает архив с закреплённым digest в новый приватный каталог."""
    path = root / (sha + '.tar')
    if not path.exists():
        payload = base64.b64decode(config['archives'][sha])
        assert hashlib.sha256(payload).hexdigest() == config['digests'][sha]
        path.write_bytes(payload)
    return snapshot.unpack(path, sha, config['digests'][sha], destination)

if config['phase'] == 'prepare':
    manifest = unpack(config['candidate'], root / 'control')
    snapshot.verify_directory(root / 'control', manifest, require_root=True)
sys.path.insert(0, str(root / 'control'))
from scripts import deploy_prod as prod, deploy_prepare as prep
from scripts import deploy_backup, deploy_runtime, deploy_schema_admission
assert prod.SOURCE_ROOT == root / 'control'
assert all(pathlib.Path(module.__file__).is_relative_to(root / 'control')
           for name, module in sys.modules.items()
           if name.startswith(('scripts.', 'compadre_assistant.')) and getattr(module, '__file__', None))

if config['phase'] == 'prepare':
    state_bytes = (root.parent / 'fixtures/state.legacy.json').read_bytes()
    assert hashlib.sha256(state_bytes).hexdigest() == 'fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317'
    for name in ('before', 'gap', 'after', 'v2'):
        case = root / name
        case.mkdir(mode=0o700)
        (case / 'checkout').mkdir(mode=0o700)
        (case / 'state').mkdir(mode=0o700)
        (case / 'state/state.json').write_bytes(state_bytes)
        unpack(config['previous'], case / 'current')
        unpack(config['candidate'], case / 'candidate')
    emit(imports=True, files=len(manifest['files']), sha=config['candidate'], digest=config['digests'][config['candidate']])
    raise SystemExit(0)

case = root / config['case']
state_dir, checkout = case / 'state', case / 'checkout'
current, saved, candidate = case / 'current', case / 'saved', case / 'candidate'
state = prep.read_state(state_dir, checkout)
assert state['stage'] == 'success'
host = prod.Host(checkout, state_dir)

def identity(path):
    """Проверяет байты, права и владельца перед использованием snapshot."""
    manifest = json.loads((path / 'controller-manifest.json').read_bytes())
    snapshot.verify_directory(path, manifest, require_root=True)
    return manifest['controller_sha']

def restore_legacy():
    """Возвращает legacy каталог только при отсутствии признаков протокола v2."""
    # Это политика проверочного установщика по OPERATIONS, не свойство old binary.
    durable = prep.read_state(state_dir, checkout)
    if 'protocol_version' in durable or any((state_dir / name).exists() for name in
            ('active-db.json', 'database-compose.yml')):
        raise ValueError('LEGACY_RESTORE_FORBIDDEN')
    if saved.exists():
        if current.exists():
            assert not candidate.exists()
            current.rename(candidate)
        saved.rename(current)
    assert identity(current) == config['previous']

with prep.deploy_lock(state_dir, checkout):
    inode = (state_dir / 'deploy.lock').stat().st_ino
    if config['phase'] == 'crash':
        assert identity(current) == config['previous']
        assert identity(candidate) == config['candidate']
        (case / 'lock-inode').write_text(str(inode))
        if config['case'] != 'before':
            current.rename(saved)
        if config['case'] in ('after', 'v2'):
            candidate.rename(current)
        if config['case'] == 'v2':
            # Преднамеренно неполное v2: достаточно признака для запрета legacy.
            state['protocol_version'] = 2
            prep.write_state(state_dir, checkout, state)
        emit(case=config['case'], current_exists=current.exists(), lock_inode=inode, injected_exit=73)
        os._exit(73)
    assert inode == int((case / 'lock-inode').read_text())
    try:
        with prep.deploy_lock(state_dir, checkout):
            raise AssertionError('second lock acquired')
    except prep.PreparationError:
        pass
    if config['case'] == 'v2':
        try:
            restore_legacy()
        except ValueError as error:
            assert str(error) == 'LEGACY_RESTORE_FORBIDDEN'
        else:
            raise AssertionError('legacy restore accepted')
        assert identity(current) == config['candidate']
        assert identity(saved) == config['previous']
        # Настоящий backup не возвращается к legacy при недоказанном v2.
        try:
            deploy_backup.selected_database(host)
        except (prod.DeployError, prep.PreparationError, ValueError):
            pass
        else:
            raise AssertionError('v2 backup accepted')
        emit(case=config['case'], legacy_restore='forbidden', candidate_retained=True, lock_inode=inode)
    else:
        restore_legacy()
        restore_legacy()
        assert hashlib.sha256((state_dir / 'state.json').read_bytes()).hexdigest() == 'fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317'
        marker = state_dir / 'deploy-in-progress'
        marker.write_text('stage7-private-proof')
        try:
            deploy_backup.selected_database(host)
        except prod.DeployError:
            pass
        else:
            raise AssertionError('marker backup accepted')
        marker.unlink()
        assert state['timer_before'] is not None
        config_text = (root / 'control/deploy/obs-chat-bot-backup-selector.conf').read_text()
        assert '-B -m scripts.deploy_backup' in config_text and 'env -i' in config_text
        for filename in ('obs-chat-bot-backup-guard.conf', 'obs-chat-bot-backup-service-guard.conf'):
            assert 'ConditionPathExists=!/var/lib/compadre-assistant-deploy/deploy-in-progress' in (root / 'control/deploy' / filename).read_text()
        emit(case=config['case'], repeat='legacy-restored-twice', legacy_state_unchanged=True,
             marker_backup='refused', backup_settings=True, lock_inode=inode)
'''


def main():
    """Проверяет локальные digest и исполняет только фиксированные remote phases."""
    archives = {}
    for sha, filename in ((CANDIDATE, "stage7-2-512cb57.tar"), (PREVIOUS, "stage7-2-dd71c2b.tar")):
        payload = (Path.home() / "AppData/Local/Temp" / filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == DIGESTS[sha]
        archives[sha] = base64.b64encode(payload).decode("ascii")
    config = dict(root=ROOT, candidate=CANDIDATE, previous=PREVIOUS, digests=DIGESTS, archives=archives)
    phases = [("prepare", None, 0)] if sys.argv[1:] == ["prepare"] else [
        (phase, case, expected) for case in ("before", "gap", "after", "v2")
        for phase, expected in (("crash", 73), ("retry", 0), ("retry", 0))]
    for phase, case, expected in phases:
        config.update(phase=phase, case=case)
        source = "config = " + repr(config) + "\n" + REMOTE
        result = subprocess.run([
            "ssh", "-i", str(Path.home() / "Downloads/SSH/H3LLO_CLOUD/karateka"),
            "user@91.188.213.142", "sudo /usr/bin/python3 -I -B -"],
            input=source.encode("utf-8"), capture_output=True, timeout=60)
        print(result.stdout.decode("utf-8"), end="", flush=True)
        if result.returncode != expected:
            # Traceback проверочного кода не содержит production state или строк БД.
            print(result.stderr.decode("utf-8"), file=sys.stderr)
            raise SystemExit(f"phase={phase} case={case} rc={result.returncode}, expected={expected}")


if __name__ == "__main__":
    main()
