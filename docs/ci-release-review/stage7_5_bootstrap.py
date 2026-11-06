"""Готовит новые private cases 7.5 в уже открытом maintenance окне.

Не выполняет stop/start production. Исторический bootstrap 7.3 используется
только для read-only baseline и проверки exact receipts/snapshot.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import sys


PARENT = Path('/var/lib/compadre-assistant-stage7-20261106')
ADMISSION = PARENT / 'admission-cabc0e2-20261106T160953Z'
BOOTSTRAP = ADMISSION / 'maintenance-bootstrap.py'
BOOTSTRAP_HASH = '7e9c81565d3590a7e239046f9a0c8cb18f69eae57d1897de29951b37dc5e0b15'
HARNESS_HASH = 'b7eb07874247df2dae47180bd2bb21520438e29175aec9daec0a10a3f6f1623f'
BUNDLE = PARENT / 'post-probe-20261106T1730Z'
FAULTS = ('admission', 'wal', 'bad_evidence', 'bad_state')


def load(path):
    """Импортирует только уже сверенный проверочный файл без вызова CLI."""
    spec = importlib.util.spec_from_file_location('fault_harness', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trusted():
    """Требует исходные байты maintenance bootstrap до импорта его функций."""
    if (BOOTSTRAP.resolve() != BOOTSTRAP or BOOTSTRAP.stat().st_uid != 0
            or BOOTSTRAP.stat().st_mode & 0o077
            or hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest() != BOOTSTRAP_HASH):
        raise ValueError('FAULT_BOOTSTRAP_BYTES')
    return runpy.run_path(str(BOOTSTRAP))


def root_for(fault):
    """Возвращает единственный фиксированный новый root выбранного отказа."""
    if fault not in FAULTS:
        raise ValueError('FAULT_NAME')
    return PARENT / ('controller-proof-cabc0e2-post-' + fault + '-01')


def prepare(harness_bytes):
    """После read-only baseline создаёт только отсутствующие private roots.

    Args:
        harness_bytes: Проверенные локальные UTF-8 байты harness 7.5.
    """
    if hashlib.sha256(harness_bytes).hexdigest() != HARNESS_HASH:
        raise ValueError('FAULT_HARNESS_BYTES')
    os.umask(0o077)
    m = trusted()
    before = m['maintenance_baseline']()
    old_harness = m['load_harness']()
    snapshot = m['verified_snapshot'](old_harness, ADMISSION)
    config = m['evidence_config'](old_harness, ADMISSION)
    m['verify_recent_admission'](config)
    recent = json.loads((m['RECENT'] / 'reviewed-config.json').read_text())
    config['schema_before'] = recent['schema_before']
    config['schema_after'] = recent['schema_after']
    if BUNDLE.exists() or any(root_for(fault).exists() for fault in FAULTS):
        raise ValueError('FAULT_ROOT_EXISTS')
    BUNDLE.mkdir(mode=0o700)
    (BUNDLE / 'harness.py').write_bytes(harness_bytes)
    harness = load(BUNDLE / 'harness.py')
    for fault in FAULTS:
        root = root_for(fault)
        root.mkdir(mode=0o700)
        (root / 'candidate.tar').write_bytes((ADMISSION / 'candidate.tar').read_bytes())
        snapshot.unpack(root / 'candidate.tar', m['SHA'], m['SNAPSHOT'], root / 'control')
        for name in m['RECEIPTS']:
            shutil.copyfile(ADMISSION / name, root / name)
        (root / 'fixtures').mkdir(mode=0o700)
        shutil.copyfile(m['SOURCE'], root / 'fixtures/source.db')
        (root / 'fault-name').write_text(fault)
        current = config | {'control': str(root / 'control')}
        harness.validate(current, root)
        harness.verify_snapshot(root, current)
        m['write_new'](root / 'reviewed-config.json', current)
        m['write_new'](root / 'prepared.json', {
            'baseline': before, 'harness_sha256': HARNESS_HASH,
            'config_sha256': harness.digest(root / 'reviewed-config.json'),
            'source_sha256': harness.digest(root / 'fixtures/source.db')})
        print(json.dumps({'phase': 'prepared', 'fault': fault, 'root': str(root),
                          'prepared_sha256': harness.digest(root / 'prepared.json')}), flush=True)
    print(json.dumps({'phase': 'postprepare', 'baseline': m['maintenance_baseline']()}), flush=True)


def execute():
    """Выполняет отказ и два новых процесса повтора, сверяя baseline каждого case."""
    m = trusted()
    path = BUNDLE / 'harness.py'
    m['private'](BUNDLE, directory=True)
    m['private'](path)
    if m['digest'](path) != HARNESS_HASH:
        raise ValueError('FAULT_HARNESS_BYTES')
    harness = load(path)
    for fault in FAULTS:
        before = m['maintenance_baseline']()
        root = root_for(fault)
        config = json.loads((root / 'reviewed-config.json').read_text())
        prepared = json.loads((root / 'prepared.json').read_text())
        if (m['digest'](root / 'reviewed-config.json') != prepared['config_sha256']
                or prepared['harness_sha256'] != HARNESS_HASH):
            raise ValueError('FAULT_PREPARED_BINDING')
        m['verify_recent_admission'](config)
        harness.validate(config, root)
        m['verified_snapshot'](harness, root)
        from scripts.stage7_migration import migrate
        m['write_new'](root / 'execution-started.json', {'baseline': before})
        first = harness.run_case(root, config, migrate)
        print(json.dumps(first), flush=True)
        second = harness.run_case(root, config, migrate)
        print(json.dumps(second), flush=True)
        third = harness.run_case(root, config, migrate)
        if fault.startswith('bad_'):
            keys = ('selected', 'admission', 'model_starts', 'marker', 'new_inode', 'test_row_count', 'selector_sha256')
            if any(second[key] != third[key] for key in keys) or not third['fail_closed']:
                raise ValueError('POST_NEGATIVE_REPEAT_CHANGED')
        elif third != second:
            raise ValueError('POST_REPEAT_CHANGED')
        print(json.dumps(third | {'second_repeat_invariants': True}), flush=True)
        after = m['maintenance_baseline']()
        print(json.dumps({'phase': 'postcase', 'fault': fault, 'baseline': after,
                          'crash_result_sha256': harness.digest(root / 'result-crash.json'),
                          'retry_result_sha256': harness.digest(root / 'result-retry.json'),
                          'transport_sha256': harness.digest(root / 'transport.jsonl')}), flush=True)


if __name__ == '__main__':
    raise SystemExit('Только reviewed prepare/execute; production stop/start отсутствуют')
