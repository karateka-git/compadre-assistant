"""Проверяет необратимый guard в отдельных процессах без Docker/systemd."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from scripts.stage7_transport import IsolationError, migration_command


class Stage7TransportTests(unittest.TestCase):
    """Проверяет прямые и обходящие Host.run внешние операции."""

    def test_import_does_not_install_guard_or_start_processes(self):
        """Импорт не меняет глобальную политику и не запускает команды."""
        code = """
import sys, subprocess
def forbidden(*args, **kwargs):
    raise AssertionError('import side effect')
sys.addaudithook = forbidden
subprocess.Popen = forbidden
import scripts.stage7_transport
"""
        result = subprocess.run([sys.executable, "-B", "-c", code],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_migration_contract_has_only_private_mount_and_no_execution(self):
        """Готовит отдельную команду image migration, отвергает небезопасные входы."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            work = root / "scenarios" / "migration"
            work.mkdir(parents=True)
            database = work / "app.db"
            database.write_bytes(b"synthetic")
            image = "sha256:" + "a" * 64
            command = migration_command(root, database, image)
            self.assertIn("none", command)
            self.assertEqual(1, command.count("--mount"))
            self.assertNotIn("--env-file", command)
            self.assertNotIn("--privileged", command)
            for candidate, pin in ((database, "release-tag"), (root / "source.db", image),
                                   (Path("/opt/obs-chat-bot/data/app.db"), image)):
                with self.assertRaises(IsolationError):
                    migration_command(root, candidate, pin)
            (work / ".env").write_bytes(b"synthetic")
            with self.assertRaises(IsolationError):
                migration_command(root, database, image)

    def test_guard_blocks_host_bypasses_and_preserves_private_files(self):
        """Реальные entry points Host отказывают до запуска внешних команд."""
        code = r'''
import os, pathlib, socket, sqlite3, subprocess, sys
from scripts.stage7_transport import install_guard, IsolationError, ModelTransport
from scripts import deploy_prod as prod
root = pathlib.Path(sys.argv[1]).resolve()
checkout = root / 'checkout'; checkout.mkdir()
state = root / 'state'; state.mkdir()
host = prod.Host(checkout, state)
inherited = os.open(root / 'inherited', os.O_CREAT | os.O_RDWR, 0o600)
install_guard(root)
model = ModelTransport({('systemctl', 'start', 'test.service'): 'modeled'})
assert model.run('systemctl', 'start', 'test.service') == 'modeled'
attempts = [
 lambda: model.run('docker', 'start', 'production'),
 lambda: subprocess.run(['docker', 'ps']),
 lambda: subprocess.run(['systemctl', 'stop', 'obs-chat-bot.service']),
 lambda: socket.socket(),
 lambda: os.system('echo STAGE7_MUST_NOT_EXECUTE'),
 lambda: host.unit_enabled(prod.TIMER),
 lambda: host.logs('real-id'),
 lambda: host.migrate_copy('sha256:' + 'a' * 64, root),
 lambda: pathlib.Path('/var/lib/compadre-assistant-deploy/state.json').read_text(),
 lambda: pathlib.Path('/opt/obs-chat-bot/data/app.db').write_bytes(b'x'),
 lambda: (root.parent / 'outside-stage7').write_bytes(b'x'),
 lambda: sqlite3.connect('/opt/obs-chat-bot/data/app.db'),
 lambda: sqlite3.connect('file:/opt/obs-chat-bot/data/app.db?mode=ro', uri=True),
 lambda: os.fdopen(inherited, 'w'),
 lambda: os.open('state.json', os.O_CREAT | os.O_RDWR, dir_fd=0),
 lambda: os.symlink(root / 'test.db', root / 'alias'),
 lambda: os.link(root / 'inherited', root.parent / 'alias'),
]
for index, action in enumerate(attempts):
 try: action()
 except IsolationError: pass
 else: raise AssertionError('allowed forbidden operation %s' % index)
(state / 'private.json').write_text('{}')
assert (state / 'private.json').read_text() == '{}'
(state / 'state.json').write_text('{"stage": "success"}')
assert prod.prep.read_state(state, checkout)['stage'] == 'success'
with sqlite3.connect(root / 'test.db') as db:
 db.execute('create table test(id integer)')
print('isolated operations:', len(attempts))
'''
        with TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-B", "-c", code, temporary],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("isolated operations: 17", result.stdout)

    def test_real_atomic_state_selector_and_recovery_under_guard(self):
        """Записывает state/selector и проходит обе стороны recovery без хоста."""
        code = r'''
import os, pathlib, sys
from unittest.mock import patch
from scripts.stage7_transport import install_guard, IsolationError
from scripts import deploy_prod as prod, deploy_prepare as prep, deploy_database_switch as switch
from tests.test_deploy_database_recovery import DatabaseRecoveryTests
root = pathlib.Path(sys.argv[1]).resolve()
install_guard(root)
installed=root/'installed'; staging=root/'staging'; saved=root/'saved'
installed.mkdir(); staging.mkdir()
(installed/'old').write_text('synthetic')
(staging/'new').write_text('synthetic')
os.rename(installed,saved)
assert not installed.exists()
os.rename(saved,installed)
os.rename(installed,saved); os.rename(staging,installed)
assert (installed/'new').exists()
os.rename(installed,staging); os.rename(saved,installed)
assert (installed/'old').exists()
for stage in ('database_selected', 'admission_recorded', 'candidate_started'):
 case = root / stage; case.mkdir()
 fixture = DatabaseRecoveryTests()
 with patch.object(prod.compatibility, 'private_text', side_effect=lambda p: p.read_text().strip()):
  host, state, record = fixture._setup(case, stage)
  assert prep.read_state(host.state_dir, host.checkout)['stage'] == stage
  fixture._recover(host, state)
  assert switch.read_selected(host, state) == ('old' if stage == 'database_selected' else 'new')
  assert len(host.found) == 2 and host.starts == (2 if stage == 'candidate_started' else 1)
  assert state['stage'] == 'recovery_verified'
  if os.name == 'posix':
   with prep.deploy_lock(host.state_dir, host.checkout):
    try:
     with prep.deploy_lock(host.state_dir, host.checkout): pass
    except prep.PreparationError: pass
    else: raise AssertionError('second lock admitted')
   with prep.deploy_lock(host.state_dir, host.checkout): pass
print('atomic state/selector/recovery OK; lock=' + str(os.name == 'posix'))
'''
        with TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-B", "-c", code, temporary],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("atomic state/selector/recovery OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
