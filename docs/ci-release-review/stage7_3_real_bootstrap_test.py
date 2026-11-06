"""Проверяет fail-closed bootstrap локально без VPS, stop и Docker."""

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bootstrap', HERE / 'stage7_3_maintenance_bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    """Проверяет привязку reviewed кода и границы окна остановки."""

    def test_harness_pin_matches_reviewed_source(self):
        """Изменение harness требует явного обновления reviewed pin."""
        self.assertEqual(bootstrap.HARNESS_HASH, hashlib.sha256((HERE / 'stage7_3_harness.py').read_bytes()).hexdigest())

    def test_maintenance_binding_rejects_restart_or_another_stop(self):
        """Окно остаётся открытым, но restart/новый stop/changed units запрещены."""
        prepared = {'active_enter': '50', 'inactive_enter': '10', 'monotonic_us': 100,
                    'effective_units_sha256': 'a' * 64}
        current = prepared | {'monotonic_us': 120}
        bootstrap.verify_maintenance_binding(prepared, current)
        bootstrap.verify_maintenance_binding(prepared, current | {'monotonic_us': 10**12})
        for field, value in (('active_enter', '101'), ('inactive_enter', '99'), ('effective_units_sha256', 'b' * 64),
                             ('monotonic_us', 99)):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'STOP_BINDING'):
                bootstrap.verify_maintenance_binding(prepared, current | {field: value})

    def test_effective_command_hash_ignores_only_runtime_metadata(self):
        """Реальный stop меняет PID/times/status, но не exact команду unit."""
        start = '/usr/bin/docker compose up -d --no-build --pull never vk_assistant tg_assistant'
        stop = '/usr/bin/docker compose stop vk_assistant tg_assistant'
        def values(metadata):
            return {key: '{ path=/usr/bin/docker ; argv[]=' + argv + ' ; ignore_errors=no ; ' + metadata + ' }'
                    for key, argv in (('ExecStart', start), ('ExecStop', stop))}
        before = values('start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0')
        after = values('start_time=[Fri 2026-11-06 16:25:00 UTC] ; stop_time=[Fri 2026-11-06 16:25:01 UTC] ; pid=123 ; code=exited ; status=0')
        self.assertEqual(bootstrap.stable_units_digest(before), bootstrap.stable_units_digest(after))
        for value in (after['ExecStop'].replace(' stop ', ' restart '),
                      after['ExecStop'].replace('ignore_errors=no', 'ignore_errors=yes'),
                      after['ExecStop'] + ' { path=/usr/bin/true ; argv[]=/usr/bin/true }',
                      after['ExecStop'].replace('status=0', 'unknown=0')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bootstrap.stable_units_digest(after | {'ExecStop': value})

    def test_live_database_descriptor_matches_sqlite_sidecars(self):
        """Deleted, sidecars, attached БД и неизвестные live data files не теряются."""
        for directory in ('/opt/obs-chat-bot/data/', '/app/data/'):
            for name in ('app.db', 'app.db-wal', 'app.db-shm', 'app.db-journal', 'app.sqlite', 'app.sqlite3',
                         'attached', 'etilqs_temporary', 'app.db.log'):
                for marker in ('', ' (deleted)'):
                    with self.subTest(directory=directory, name=name, marker=marker):
                        self.assertTrue(bootstrap.is_live_database_descriptor(directory + name + marker))
        for path in ('/var/lib/compadre-assistant-stage7-20261106/fixtures/source.db', '/tmp/app.db',
                     '/tmp/etilqs_x (deleted)', '/app/data-other/app.db', 'anon_inode:[eventpoll]', '/memfd:sqlite (deleted)'):
            self.assertFalse(bootstrap.is_live_database_descriptor(path))

    def test_live_holder_scan_rejects_unknown_process_and_permission_error(self):
        """Неизвестный writer и непрочитанный descriptor закрывают baseline."""
        proc, process = MagicMock(), MagicMock()
        process.name = '123'
        proc.iterdir.return_value = [process]
        directory, cgroup = MagicMock(), MagicMock()
        process.__truediv__.side_effect = lambda name: directory if name == 'fd' else cgroup
        directory.iterdir.return_value = [Path('/proc/123/fd/7')]
        cgroup.read_text.return_value = next(iter(bootstrap.IDS))
        with patch.object(bootstrap, 'Path', return_value=proc), patch.object(bootstrap.os, 'readlink', return_value='/app/data/app.db-wal (deleted)') as readlink:
            self.assertEqual(bootstrap.check_manual_writers(), 1)
            cgroup.read_text.return_value = 'unknown-process'
            with self.assertRaisesRegex(ValueError, 'MANUAL_DB_HOLDER'):
                bootstrap.check_manual_writers()
            readlink.side_effect = PermissionError('denied')
            with self.assertRaises(PermissionError):
                bootstrap.check_manual_writers()

    def test_missing_prepared_receipt_cannot_start_any_command(self):
        """Запуск без reviewed staging не вызывает broker или baseline."""
        with TemporaryDirectory() as temporary, patch.object(bootstrap, 'CASE', Path(temporary)), \
                patch.object(bootstrap, 'baseline') as baseline, patch.object(bootstrap, 'load_harness') as load:
            with self.assertRaises(FileNotFoundError):
                bootstrap.run_in_maintenance()
            baseline.assert_not_called()
            load.assert_not_called()

    def test_private_write_refuses_replay(self):
        """Однократный execution receipt не перезаписывается при повторе."""
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / 'execution-started.json'
            bootstrap.write_new(path, {'first': True})
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                bootstrap.write_new(path, {'second': True})
            self.assertEqual(before, path.read_bytes())

    def test_recent_live_receipt_binding_rejects_other_pair_and_bytes(self):
        """Повторное использование16:52 evidence требует exact reviewed bytes/пару."""
        keys = ('candidate_sha', 'previous_sha', 'run_id', 'run_attempt', 'full_ci_success', 'image_id',
                'previous_image_id', 'schema_fingerprint', 'snapshot_digest', 'policy_sha', 'artifact_digest', 'artifact_id')
        config = {key: str(index) for index, key in enumerate(keys)}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'reviewed-config.json').write_text(json.dumps(config))
            config_hash = bootstrap.digest(root / 'reviewed-config.json')
            (root / 'prepared.json').write_text(json.dumps({'config_sha256': config_hash}))
            with patch.object(bootstrap, 'RECENT', root), patch.object(bootstrap, 'RECENT_CONFIG', config_hash), \
                    patch.object(bootstrap, 'RECENT_PREPARED', bootstrap.digest(root / 'prepared.json')), patch.object(bootstrap, 'private'):
                bootstrap.verify_recent_admission(config)
                with self.assertRaisesRegex(ValueError, 'RECENT_BINDING'):
                    bootstrap.verify_recent_admission(config | {'candidate_sha': 'other'})
                (root / 'prepared.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'RECENT_BYTES'):
                    bootstrap.verify_recent_admission(config)

    def test_closed_source_exact_sidecars_and_immutable_read(self):
        """Empty WAL допускается только с exact SHM/main; immutable чтение не пишет."""
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / 'source.db'
            db = sqlite3.connect(source)
            db.execute('CREATE TABLE schema_migrations (version INTEGER, name TEXT)')
            db.executemany('INSERT INTO schema_migrations VALUES (?, ?)', [
                (1, '0001_initial_schema.sql'), (2, '0002_user_capability_preferences.sql'),
                (3, '0003_backfill_existing_user_names.sql'), (4, '0004_private_chat_address_lookup.sql')])
            db.commit(); db.close()
            Path(str(source) + '-wal').write_bytes(b'')
            shm = Path(str(source) + '-shm'); shm.write_bytes(b'synthetic coordination')
            pins = {suffix: (p.stat().st_size, p.stat().st_mtime_ns, bootstrap.digest(p))
                    for suffix in ('', '-wal', '-shm') for p in (Path(str(source) + suffix),)}
            # На Windows моделируются только root/mode; SQLite и bytes реальные.
            with patch.object(bootstrap, 'SOURCE', source), patch.object(bootstrap, 'SOURCE_FILES', pins), patch.object(bootstrap, 'private'):
                bootstrap.verify_closed_source()
                self.assertEqual(pins, {suffix: (p.stat().st_size, p.stat().st_mtime_ns, bootstrap.digest(p))
                    for suffix in pins for p in (Path(str(source) + suffix),)})
                shm.write_bytes(b'changed coordination!')
                with self.assertRaisesRegex(ValueError, 'CLOSED_SOURCE'):
                    bootstrap.verify_closed_source()


if __name__ == '__main__':
    unittest.main()
