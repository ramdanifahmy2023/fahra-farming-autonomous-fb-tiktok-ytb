import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fahra_automation.config import Config, ConfigurationError
from fahra_automation.db import Store
from fahra_automation.storage import database_path, fingerprint, migrate, status
from fahra_automation.util import process_lock


class StorageLocationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        self.runtime = self.root / 'runtime'
        self.source = self.runtime / 'workflow.sqlite3'
        self.store = Store(self.source)
        self.store.set('paused', True)
        self.store.set('reply_enabled', False)
        self.store.set('account_report_enabled', False)
        self.store.set('migration_id', 'fixture-cutover')
        self.destination = self.root.parent / 'internal/state/workflow.sqlite3'
        self.guard = Mock()
        self.config = SimpleNamespace(root=self.root, runtime=self.runtime, data={}, require_production_checkout=self.guard)

    def pointer(self, **extra):
        path = self.runtime / 'database-location.json'
        path.write_text(json.dumps({'version': 1, 'root': str(self.root), 'database': str(self.destination), **extra}))
        return path

    def test_default_is_local_and_does_not_create_database(self):
        self.assertEqual(database_path(self.config), self.source)
        other = SimpleNamespace(root=self.root / 'other', runtime=self.root / 'other/runtime')
        self.assertEqual(database_path(other), other.runtime / 'workflow.sqlite3')
        self.assertFalse(other.runtime.exists())

    def test_move_checks_all_tables_blobs_settings_and_preserves_runtime(self):
        with self.store.tx() as connection:
            connection.execute('CREATE TABLE fixture_mixed(id INTEGER PRIMARY KEY, text TEXT, value BLOB, n REAL)')
            connection.execute('INSERT INTO fixture_mixed VALUES(?,?,?,?)', (1, 'Nasi 🍚', b'\x00\xff', 1.25))
            before = fingerprint(connection)
        (self.runtime / 'ownership.json').write_text('{"fixture":true}')
        (self.runtime / 'attempts').mkdir()
        result = migrate(self.config, self.destination)
        self.guard.assert_called_once()
        self.assertEqual(database_path(self.config), self.destination)
        self.assertTrue(self.source.is_dir())
        self.assertTrue((self.source / 'MOVED.json').exists())
        self.assertTrue(Path(result['archive']).is_file())
        self.assertTrue((self.runtime / 'ownership.json').is_file())
        self.assertTrue((self.runtime / 'attempts').is_dir())
        with sqlite3.connect(self.destination) as connection:
            self.assertEqual(fingerprint(connection), before)
        with sqlite3.connect(result['archive']) as connection:
            self.assertEqual(fingerprint(connection), before)
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(self.source)
        self.assertTrue(status(self.config)['ready'])
        self.assertFalse(status(self.config)['migration_incomplete'])
        relocated = Store(self.destination, readonly=True)
        self.assertTrue(relocated.get('paused'))
        self.assertEqual(relocated.get('migration_id'), 'fixture-cutover')

    def test_config_data_copy_cannot_inherit_other_projects_pointer(self):
        migrate(self.config, self.destination)
        data = {'workflow': {'runtime_dir': 'runtime'}}
        original = Config(self.root, data)
        self.assertEqual(original.database_path, self.destination)
        fixture = Config(self.root / 'isolated', copy.deepcopy(original.data))
        self.assertEqual(fixture.database_path, fixture.runtime / 'workflow.sqlite3')

    def test_wrong_root_relative_missing_or_unknown_pointer_rejected(self):
        for extra in ({'root': '/different'}, {'database': 'relative.sqlite3'}, {'version': 99}, {'version': True}, {}):
            self.pointer(**extra)
            with self.assertRaises(ConfigurationError):
                database_path(self.config)

    def test_pointer_cannot_initialize_empty_replacement_database(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.touch()
        self.pointer()
        with self.assertRaises(ConfigurationError):
            database_path(self.config)
        self.assertFalse(status(self.config)['ready'])

    def test_symlink_pointer_target_and_parent_rejected(self):
        target = self.runtime / 'metadata.json'; target.write_text('{}')
        link = self.runtime / 'database-location.json'; link.symlink_to(target)
        with self.assertRaises(ConfigurationError):
            database_path(self.config)
        link.unlink()
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(self.source)
        self.pointer()
        with self.assertRaises(ConfigurationError):
            database_path(self.config)
        self.destination.unlink()
        (self.root.parent / 'linked').symlink_to(self.runtime, target_is_directory=True)
        self.pointer(database=str(self.root.parent / 'linked/workflow.sqlite3'))
        with self.assertRaises(ConfigurationError):
            database_path(self.config)

    def test_project_root_alias_does_not_allow_symlink_runtime(self):
        alias = self.root.parent / 'project-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        config = SimpleNamespace(root=alias, runtime=alias / 'runtime')
        self.assertEqual(database_path(config), self.source)
        (self.root / 'runtime-link').symlink_to(self.runtime, target_is_directory=True)
        config.runtime = alias / 'runtime-link'
        with self.assertRaises(ConfigurationError):
            database_path(config)
        config.root = self.root
        config.runtime = self.root / 'runtime-link'
        with self.assertRaises(ConfigurationError):
            database_path(config)

    def test_incomplete_marker_blocks_even_when_old_or_new_db_exists(self):
        marker = self.runtime / 'database-migration.json'
        marker.write_text('{"phase":"copying"}')
        with self.assertRaises(ConfigurationError):
            database_path(self.config)
        self.assertTrue(status(self.config)['migration_incomplete'])
        self.assertFalse(status(self.config)['ready'])

    def test_existing_destination_and_active_worker_refuse_without_marker(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b'preserve')
        with self.assertRaises(ConfigurationError):
            migrate(self.config, self.destination)
        self.assertEqual(self.destination.read_bytes(), b'preserve')
        self.destination.unlink()
        with process_lock(self.runtime / 'worker.lock'):
            with self.assertRaises(RuntimeError):
                migrate(self.config, self.destination)
        self.assertFalse((self.runtime / 'database-migration.json').exists())

    def test_running_controls_or_leases_hold_before_copy(self):
        for key, value in (('paused', False), ('reply_enabled', True), ('account_report_enabled', True)):
            old = self.store.get(key)
            self.store.set(key, value)
            with self.assertRaises(ConfigurationError):
                migrate(self.config, self.destination)
            self.store.set(key, old)
        self.store.enqueue(1, 2, 3, [('https://instagram.com/reel/fixture/', 'fixture')])
        job = self.store.queue()[0]
        self.store.checkpoint(job['id'], leased=1)
        with self.assertRaises(ConfigurationError):
            migrate(self.config, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.runtime / 'database-migration.json').exists())

    def test_production_guard_runs_before_touching_destination(self):
        self.guard.side_effect = ConfigurationError('fixture denied')
        with self.assertRaises(ConfigurationError):
            migrate(self.config, self.destination)
        self.assertFalse(self.destination.exists())

    def test_crash_after_copy_or_after_archive_fails_closed(self):
        import fahra_automation.storage as storage
        original = storage._atomic_json
        def fail_at_pointer(path, value, **kwargs):
            if path.name == 'database-location.json':
                raise OSError('fixture crash before pointer switch')
            return original(path, value, **kwargs)
        with patch.object(storage, '_atomic_json', side_effect=fail_at_pointer):
            with self.assertRaises(OSError):
                migrate(self.config, self.destination)
        self.assertTrue(self.destination.is_file())
        self.assertTrue(self.source.is_dir())
        self.assertTrue(status(self.config)['migration_incomplete'])
        with self.assertRaises(ConfigurationError):
            database_path(self.config)
        with self.assertRaises(ConfigurationError):
            migrate(self.config, self.root.parent / 'another.sqlite3')

    def test_fingerprint_detects_data_changes_and_fk_violation(self):
        with self.store.connect() as connection:
            before = fingerprint(connection)
        self.store.set('fixture', 'new value')
        with self.store.connect() as connection:
            self.assertNotEqual(fingerprint(connection), before)
        with sqlite3.connect(self.source) as connection:
            connection.execute('CREATE TABLE fixture_parent(id INTEGER PRIMARY KEY)')
            connection.execute('CREATE TABLE fixture_child(parent INTEGER REFERENCES fixture_parent(id))')
            connection.execute('INSERT INTO fixture_child VALUES(999)')
        with self.store.connect() as connection:
            with self.assertRaises(ConfigurationError):
                fingerprint(connection)

    def test_archived_source_cannot_be_used_as_new_active_database(self):
        result = migrate(self.config, self.destination)
        self.store = Store(self.destination)
        self.store.set('after_move', 'new data')
        with sqlite3.connect(result['archive']) as connection:
            self.assertIsNone(connection.execute("SELECT value FROM settings WHERE key='after_move'").fetchone())
        self.assertEqual(Store(database_path(self.config), readonly=True).get('after_move'), 'new data')

    def test_already_open_writer_cannot_write_source_after_switch(self):
        import fahra_automation.storage as storage
        original = storage._atomic_json
        opened, switch_started, attempting = threading.Event(), threading.Event(), threading.Event()
        def stale_writer():
            connection = sqlite3.connect(self.source, timeout=3, isolation_level=None)
            try:
                connection.execute('SELECT count(*) FROM settings').fetchone()
                opened.set()
                self.assertTrue(switch_started.wait(3))
                attempting.set()
                try:
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute("INSERT INTO settings VALUES('stale_write','true')")
                    connection.commit()
                    return 'unexpected success'
                except sqlite3.Error as exc:
                    return exc.sqlite_errorname
            finally:
                connection.close()
        def guarded_write(path, value, **kwargs):
            result = original(path, value, **kwargs)
            if path.name == 'database-migration.json' and value.get('phase') == 'verified':
                switch_started.set()
                self.assertTrue(attempting.wait(3))
            return result
        with ThreadPoolExecutor(max_workers=1) as threads:
            future = threads.submit(stale_writer)
            self.assertTrue(opened.wait(3))
            with patch.object(storage, '_atomic_json', side_effect=guarded_write):
                result = migrate(self.config, self.destination)
            self.assertEqual(future.result(timeout=5), 'SQLITE_READONLY_DBMOVED')
        for path in (self.destination, Path(result['archive'])):
            with sqlite3.connect(path) as connection:
                self.assertIsNone(connection.execute("SELECT value FROM settings WHERE key='stale_write'").fetchone())

    def test_wal_move_is_refused_before_any_copy_or_marker(self):
        connection = sqlite3.connect(self.source)
        try:
            self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        finally:
            connection.close()
        with self.assertRaisesRegex(ConfigurationError, 'journal_mode=DELETE'):
            migrate(self.config, self.destination)
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.runtime / 'database-migration.json').exists())
