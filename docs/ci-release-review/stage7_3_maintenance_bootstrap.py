"""Закрытый maintenance bootstrap 7.3 для уже остановленных пользователем ботов.

CLI отсутствует. Модуль не вызывает systemctl stop/start и не пишет production.
Функции вызываются оператором только из проверенного private каталога.
"""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import uuid

ADMISSION = Path('/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z')
CASE = ADMISSION.parent / 'controller-proof-cabc0e2-real03'
SOURCE = ADMISSION.parent / 'fixtures/source.db'
SHA = 'cabc0e24d28947123868db8a60f6b55095feb63e'
PREVIOUS = 'dd71c2ba42a0744b5b654cedcf73009ce8879058'
SNAPSHOT = '578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca'
SOURCE_HASH = '5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c'
STATE_HASH = 'fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317'
IMAGE = 'sha256:2025e11b35b9e8898ca41d740d4a10e318e048e91d0eeda04b1000c4db5bc093'
OLD_IMAGE = 'sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8'
IDS = {'2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42',
       '31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00'}
# Отдельный reviewed harness загружается как точные байты перед prepare.
HARNESS_HASH = '77b772aa94ba8cffd1de66de59360e803713e44b9572e5f5accd6141bf86da41'
RECEIPTS = {
    'ci-receipt.json': '914166c01b089cab426d5f2705b4040c47d334deecc1779b629892f6640fd72d',
    'image-receipt.json': '42c8fffe8b4753545dfd65244c5100626570661a07b346a9bd6cbcdb97727d4e',
    'synthetic-receipt.json': '5d0545e18b316635ee558472ee0258beec12a7196821a1575960e1595b5cf69b',
}
# Исходные 7.1 sidecars сохранены; пустой WAL не содержит committed frames.
SOURCE_FILES = {
    '': (12677120, 1793973262336112272, SOURCE_HASH),
    '-wal': (0, 1793973263392123030, 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
    '-shm': (32768, 1793973263393057257, 'fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb'),
}


def digest(path):
    """Возвращает hash файла без публикации его содержимого."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private(path, directory=False, read_only=False):
    """Требует canonical root:root private файл/каталог без alias."""
    info = path.lstat()
    if (path.resolve() != path or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o400 if read_only else 0o600)
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ValueError('BOOTSTRAP_PRIVATE')


def command(*args):
    """Выполняет только внутренние фиксированные read-only команды bootstrap."""
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30,
                          env={'PATH': '/usr/bin:/bin', 'HOME': str(ADMISSION),
                               'DOCKER_CONFIG': str(ADMISSION / 'unused-readonly-docker-config')}).stdout.strip()


def load_harness():
    """Импортирует только закреплённый reviewed harness из admission root."""
    path = ADMISSION / 'harness-maintenance.py'
    private(path)
    if digest(path) != HARNESS_HASH:
        raise ValueError('BOOTSTRAP_HARNESS_BYTES')
    spec = importlib.util.spec_from_file_location('stage7_reviewed_harness', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verified_snapshot(harness, root):
    """Сверяет tar/control до любого импорта кода snapshot."""
    harness.verify_snapshot(root, {'synthetic': False, 'control': str(root / 'control'),
                                   'snapshot_digest': SNAPSHOT})
    sys.path.insert(0, str(root / 'control'))
    from scripts import controller_snapshot
    manifest, _ = controller_snapshot.inspect_archive(root / 'candidate.tar', SHA, SNAPSHOT)
    controller_snapshot.verify_directory(root / 'control', manifest, require_root=True)
    return controller_snapshot


def evidence_config(harness, root):
    """Читает только exact reviewed receipts; не принимает пользовательский флаг CI."""
    for name, expected in RECEIPTS.items():
        path = root / name
        private(path)
        if digest(path) != expected:
            raise ValueError('BOOTSTRAP_RECEIPT_BYTES')
    ci = json.loads((root / 'ci-receipt.json').read_text())
    image = json.loads((root / 'image-receipt.json').read_text())
    config = {key: ci[key] for key in ('candidate_sha', 'previous_sha', 'run_id', 'run_attempt',
              'full_ci_success', 'schema_fingerprint', 'snapshot_digest', 'policy_sha', 'artifact_digest', 'artifact_id')}
    config.update(repository='karateka-git/compadre-assistant', synthetic=False,
                  source_digest=SOURCE_HASH, image_id=image['image_id'], previous_image_id=OLD_IMAGE,
                  nonce=uuid.uuid4().hex, control=str(CASE / 'control'), schema_before=[], schema_after=[])
    harness.validate_native_receipts(root, config)
    if config['candidate_sha'] != SHA or image['image_id'] != IMAGE or config['previous_sha'] != PREVIOUS:
        raise ValueError('BOOTSTRAP_EXACT_PAIR')
    return config


def baseline(stopped):
    """Проверяет прежний контур без SQLite и без production mutations."""
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('BOOTSTRAP_LINUX_ROOT')
    private(ADMISSION, directory=True)
    checkout = '/opt/obs-chat-bot'
    if (command('git', '-c', 'safe.directory=' + checkout, '-C', checkout, 'status', '--porcelain')
            or command('git', '-c', 'safe.directory=' + checkout, '-C', checkout, 'rev-parse', 'HEAD') != PREVIOUS):
        raise ValueError('BOOTSTRAP_CHECKOUT')
    state = Path('/var/lib/compadre-assistant-deploy')
    if (digest(state / 'state.json') != STATE_HASH or any((state / name).exists() for name in
            ('deploy-in-progress', 'active-db.json', 'controller.sha', 'recovery-blocked.json'))):
        raise ValueError('BOOTSTRAP_PRODUCTION_STATE')
    if set(command('docker', 'ps', '--all', '--no-trunc', '-q').splitlines()) != IDS:
        raise ValueError('BOOTSTRAP_CONTAINER_IDS')
    for item in json.loads(command('docker', 'inspect', *sorted(IDS))):
        if (item['Image'] != OLD_IMAGE or item['RestartCount'] != 0
                or item['HostConfig']['RestartPolicy']['Name'] != 'unless-stopped'
                or item['State']['Running'] != (not stopped)
                or (item['State']['Status'] != 'exited' if stopped else item['State']['Health']['Status'] != 'healthy')):
            raise ValueError('BOOTSTRAP_CONTAINERS')
    for image, sha in ((OLD_IMAGE, PREVIOUS), (IMAGE, SHA)):
        item = json.loads(command('docker', 'image', 'inspect', image))[0]
        if item['Id'] != image or item['Config']['Labels']['org.opencontainers.image.revision'] != sha:
            raise ValueError('BOOTSTRAP_IMAGE_OCI')
    properties = command('systemctl', 'show', 'obs-chat-bot.service', '--property=ActiveState,ActiveEnterTimestampMonotonic,InactiveEnterTimestampMonotonic,ExecStart,ExecStop')
    values = dict(line.split('=', 1) for line in properties.splitlines())
    if values['ActiveState'] != ('inactive' if stopped else 'active'):
        raise ValueError('BOOTSTRAP_SERVICE')
    for unit, expected in (('obs-chat-bot-backup.timer', 'active'), ('obs-chat-bot-backup.service', 'inactive')):
        if command('systemctl', 'show', unit, '--property=ActiveState', '--value') != expected:
            raise ValueError('BOOTSTRAP_BACKUP')
    verify_closed_source()
    if shutil.disk_usage(ADMISSION).free < 2 * 1024**3:
        raise ValueError('BOOTSTRAP_SPACE')
    return {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic_us': time.monotonic_ns() // 1000,
            'active_enter': values['ActiveEnterTimestampMonotonic'],
            'inactive_enter': values['InactiveEnterTimestampMonotonic'],
            'effective_units_sha256': stable_units_digest(values), 'manual_db_holders': check_manual_writers()}


def stable_units_digest(values):
    """Хеширует exact effective команды без runtime timestamps/PID/status.

    Args:
        values: Systemctl show properties для ExecStart и ExecStop.

    Raises:
        ValueError: Неизвестный descriptor или effective команда изменились.
    """
    expected = {
        'ExecStart': '/usr/bin/docker compose up -d --no-build --pull never vk_assistant tg_assistant',
        'ExecStop': '/usr/bin/docker compose stop vk_assistant tg_assistant',
    }
    commands = {}
    for name, argv in expected.items():
        match = re.fullmatch(r'\{ path=([^;{}]+) ; argv\[\]=([^;{}]+) ; ignore_errors=(yes|no) ; (.*?) \}', values[name])
        if not match or match.group(1) != '/usr/bin/docker' or match.group(2) != argv or match.group(3) != 'no':
            raise ValueError('BOOTSTRAP_UNIT_COMMAND')
        runtime_keys = [field.strip().split('=', 1)[0] for field in match.group(4).split(';')]
        if len(runtime_keys) != 5 or set(runtime_keys) != {'start_time', 'stop_time', 'pid', 'code', 'status'}:
            raise ValueError('BOOTSTRAP_UNIT_DESCRIPTOR')
        commands[name] = [match.group(1), match.group(2), match.group(3)]
    return hashlib.sha256(json.dumps(commands, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def is_live_database_descriptor(target):
    """Распознаёт любой live data FD, включая deleted SQLite и временные файлы.

    Расширение не доказывает отсутствие writer: SQLite допускает произвольное
    имя main/attached БД и временные файлы. Поэтому неизвестный файл в live
    data тоже закрывает допуск; тип доступа по одному имени FD не выводится.
    """
    return target.removesuffix(' (deleted)').startswith(('/opt/obs-chat-bot/data/', '/app/data/'))


def check_manual_writers():
    """Закрывает допуск при live DB descriptor вне двух прежних containers.

    Возвращает только число разрешённых процессов. Исчезнувшие процессы
    допустимы; permission/read errors не подменяются пустым результатом.
    Краткоживущий writer между осмотрами исключается операторским окном.
    """
    holders = 0
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            found = False
            for descriptor in (process / 'fd').iterdir():
                try:
                    target = os.readlink(descriptor)
                except FileNotFoundError:
                    continue
                found |= is_live_database_descriptor(target)
            if found:
                cgroup = (process / 'cgroup').read_text()
                if not any(identity in cgroup for identity in IDS):
                    raise ValueError('BOOTSTRAP_MANUAL_DB_HOLDER')
                holders += 1
        except (FileNotFoundError, ProcessLookupError):
            continue
    return holders


def verify_closed_source():
    """Read-only сверяет exact 7.1 main/sidecars и immutable integrity/FK/журнал.

    Пустой исходный WAL и exact SHM сохраняются. Они не копируются в case.
    Изменение bytes/mtime/size до либо после immutable чтения закрывает запуск.
    """
    def metadata():
        result = {}
        for suffix in SOURCE_FILES:
            path = Path(str(SOURCE) + suffix)
            private(path, read_only=not suffix)
            result[suffix] = (path.stat().st_size, path.stat().st_mtime_ns, digest(path))
        return result
    if metadata() != SOURCE_FILES or Path(str(SOURCE) + '-journal').exists():
        raise ValueError('BOOTSTRAP_CLOSED_SOURCE')
    with closing(sqlite3.connect('file:' + str(SOURCE) + '?mode=ro&immutable=1', uri=True)) as db:
        if (db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                or db.execute('PRAGMA foreign_key_check').fetchall()
                or db.execute('SELECT version,name FROM schema_migrations ORDER BY version').fetchall() != [
                    (1, '0001_initial_schema.sql'), (2, '0002_user_capability_preferences.sql'),
                    (3, '0003_backfill_existing_user_names.sql'), (4, '0004_private_chat_address_lookup.sql')]):
            raise ValueError('BOOTSTRAP_SOURCE_INTEGRITY')
    if metadata() != SOURCE_FILES:
        raise ValueError('BOOTSTRAP_SOURCE_CHANGED')


def write_new(path, value):
    """Создаёт private JSON однократно; существующий receipt не перезаписывается."""
    with path.open('x', encoding='utf-8') as output:
        os.chmod(path, 0o600)
        json.dump(value, output, sort_keys=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def prepare():
    """В maintenance окне проверяет exact admission и создаёт новый private case.

    Returns:
        Путь к staging и SHA256 prepared.json для независимого review.
    """
    os.umask(0o077)
    before = maintenance_baseline()
    harness = load_harness()
    config = evidence_config(harness, ADMISSION)
    snapshot = verified_snapshot(harness, ADMISSION)
    verify_recent_admission(config)
    CASE.mkdir(mode=0o700)
    (CASE / 'candidate.tar').write_bytes((ADMISSION / 'candidate.tar').read_bytes())
    snapshot.unpack(CASE / 'candidate.tar', SHA, SNAPSHOT, CASE / 'control')
    for name in RECEIPTS:
        shutil.copyfile(ADMISSION / name, CASE / name)
        os.chmod(CASE / name, 0o600)
    shutil.copyfile(ADMISSION / 'harness-maintenance.py', CASE / 'harness.py')
    (CASE / 'fixtures').mkdir(mode=0o700)
    shutil.copyfile(SOURCE, CASE / 'fixtures/source.db')
    os.chmod(CASE / 'fixtures/source.db', 0o600)
    if any(Path(str(CASE / 'fixtures/source.db') + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('BOOTSTRAP_COPY_SIDECARS')
    with closing(sqlite3.connect('file:' + str(CASE / 'fixtures/source.db') + '?mode=ro&immutable=1', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('BOOTSTRAP_COPY_INTEGRITY')
        config['schema_before'] = [row[0] for row in db.execute('SELECT name FROM schema_migrations ORDER BY version')]
    sql = ADMISSION / 'build-context/compadre_assistant/core/data/sqlite/migrations'
    context = ADMISSION / 'build-context.tar'
    if digest(context) != json.loads((ADMISSION / 'image-receipt.json').read_text())['context_sha256']:
        raise ValueError('BOOTSTRAP_CONTEXT_BYTES')
    with tarfile.open(context) as archive:
        prefix = 'compadre_assistant/core/data/sqlite/migrations/'
        values = {Path(item.name).name: archive.extractfile(item).read() for item in archive.getmembers()
                  if item.isfile() and item.name.startswith(prefix) and item.name.endswith('.sql')}
    if not values or set(values) != {path.name for path in sql.glob('*.sql')} or any((sql / name).read_bytes() != value for name, value in values.items()):
        raise ValueError('BOOTSTRAP_SQL_BYTES')
    config['schema_after'] = sorted(values)
    harness.validate(config, CASE)
    harness.verify_snapshot(CASE, config)
    if digest(CASE / 'fixtures/source.db') != SOURCE_HASH:
        raise ValueError('BOOTSTRAP_COPY_BYTES')
    after = maintenance_baseline()
    if any(before[key] != after[key] for key in ('active_enter', 'effective_units_sha256')):
        raise ValueError('BOOTSTRAP_BASELINE_CHANGED')
    write_new(CASE / 'reviewed-config.json', config)
    write_new(CASE / 'prepared.json', {'baseline': after, 'config_sha256': digest(CASE / 'reviewed-config.json'),
                                     'harness_sha256': HARNESS_HASH, 'maintenance_stop_sha256': MAINTENANCE_HASH})
    return {'root': str(CASE), 'prepared_sha256': digest(CASE / 'prepared.json')}


# Пользователь явно открыл это окно; новое stop/start этим модулем не выполняется.
MAINTENANCE = ADMISSION.parent / 'maintenance-stop-20261106T165819Z.json'
MAINTENANCE_HASH = '35f7e53afe2f4872838c8d5ad9ff687979dc29b03642733361325cd973ed4de3'
RECENT = ADMISSION.parent / 'controller-proof-cabc0e2-real02'
RECENT_PREPARED = 'f507efb5d396a63715fd37c9bba5b59ed911c8980ace30dd6d42b09018f7166a'
RECENT_CONFIG = 'd9a63a8ff93fb1c272875d9fe6a2b532f9b8d3fc08c2884ff342992a0898fb7c'


def verify_recent_admission(config):
    """Связывает новый case с reviewed live verifier real02 от16:52 без нового API."""
    for path, expected in ((RECENT / 'prepared.json', RECENT_PREPARED), (RECENT / 'reviewed-config.json', RECENT_CONFIG)):
        private(path)
        if digest(path) != expected:
            raise ValueError('MAINTENANCE_RECENT_BYTES')
    previous = json.loads((RECENT / 'reviewed-config.json').read_text())
    prepared = json.loads((RECENT / 'prepared.json').read_text())
    if prepared['config_sha256'] != RECENT_CONFIG:
        raise ValueError('MAINTENANCE_RECENT_BINDING')
    fields = ('candidate_sha', 'previous_sha', 'run_id', 'run_attempt', 'full_ci_success', 'image_id',
              'previous_image_id', 'schema_fingerprint', 'snapshot_digest', 'policy_sha', 'artifact_digest', 'artifact_id')
    if any(config[key] != previous[key] for key in fields):
        raise ValueError('MAINTENANCE_RECENT_BINDING')


def verify_maintenance_binding(stopped, current):
    """Требует тот же stop/activation/units, не допускает restart между case.

    Args:
        stopped: Baseline из exact maintenance-stop receipt пользователя.
        current: Fresh baseline stopped контура.

    Raises:
        ValueError: Окно изменилось или monotonic clock пошёл назад.
    """
    if (any(stopped[key] != current[key] for key in ('active_enter', 'inactive_enter', 'effective_units_sha256'))
            or current['monotonic_us'] < stopped['monotonic_us']):
        raise ValueError('MAINTENANCE_STOP_BINDING')


def maintenance_baseline():
    """Read-only подтверждает exact пользовательское окно без stop/start/auto-recovery."""
    private(MAINTENANCE)
    if digest(MAINTENANCE) != MAINTENANCE_HASH:
        raise ValueError('MAINTENANCE_RECEIPT_BYTES')
    receipt = json.loads(MAINTENANCE.read_text())
    if (receipt['service'] != 'inactive' or receipt['automatic_recovery'] is not False
            or receipt['previous_ids_exited'] is not True or receipt['production_started'] is not False):
        raise ValueError('MAINTENANCE_RECEIPT_CONTRACT')
    current = baseline(True)
    verify_maintenance_binding(receipt['baseline'], current)
    return current


def run_in_maintenance():
    """Однократно запускает real broker после review нового case в том же stop окне."""
    prepared = json.loads((CASE / 'prepared.json').read_text())
    current = maintenance_baseline()
    if (prepared['maintenance_stop_sha256'] != MAINTENANCE_HASH
            or any(current[key] != prepared['baseline'][key] for key in ('active_enter', 'inactive_enter', 'effective_units_sha256'))
            or not 0 <= current['monotonic_us'] - prepared['baseline']['monotonic_us'] <= 15 * 60 * 1000000
            or digest(CASE / 'reviewed-config.json') != prepared['config_sha256']):
        raise ValueError('MAINTENANCE_CASE_BINDING')
    harness = load_harness()
    config = json.loads((CASE / 'reviewed-config.json').read_text())
    harness.validate(config, CASE)
    verify_recent_admission(config)
    verified_snapshot(harness, CASE)
    from scripts.stage7_migration import migrate
    write_new(CASE / 'execution-started.json', {'utc': current['utc'], 'prepared_sha256': digest(CASE / 'prepared.json'),
                                              'maintenance_stop_sha256': MAINTENANCE_HASH})
    result = harness.run_case(CASE, config, migrate)
    maintenance_baseline()
    return result


if __name__ == '__main__':
    raise SystemExit('CLI отсутствует; только reviewed prepare/run_in_maintenance; stop/start не выполняются')
