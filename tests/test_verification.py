import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.verify import (changed_paths, known_secrets, main, markdown_issues, redact,
                            repository_files, scan_files, secret_issues, select_mode, syntax_issues)


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='verify repo ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Verification test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('.gitignore', 'runtime/\nCredential/\n')
        self.write('README.md', '# Contoh\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Fixture')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.PIPE)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_scope_includes_committed_staged_and_untracked_changes(self):
        base = self.git('rev-parse', 'HEAD').decode().strip()
        self.write('README.md', '# Baru\n')
        self.git('add', 'README.md')
        self.git('commit', '-m', 'Dokumen')
        changed, resolved = changed_paths(self.root, base)
        self.assertEqual(resolved, base)
        self.assertEqual(select_mode('auto', changed), 'docs')
        self.write('new.py', 'value = 1\n')
        changed, _ = changed_paths(self.root, base)
        self.assertEqual(select_mode('auto', changed), 'full')
        with self.assertRaises(ValueError):
            select_mode('docs', changed)
        self.assertEqual(select_mode('auto', set()), 'full')

    def test_invalid_base_fails_and_initial_zero_base_uses_full(self):
        with self.assertRaises(subprocess.CalledProcessError):
            changed_paths(self.root, 'no-such-commit')
        changed, base = changed_paths(self.root, '0' * 40)
        self.assertIsNone(base)
        self.assertEqual(select_mode('auto', changed), 'full')

    def test_secret_in_index_cannot_hide_behind_clean_working_copy(self):
        token = 'ghp_' + 'A' * 36
        self.write('config.txt', token)
        self.git('add', 'config.txt')
        self.write('config.txt', 'placeholder\n')
        issues, _ = scan_files(self.root, repository_files(self.root), set())
        self.assertTrue(any(i['file'] == 'config.txt (index)' for i in issues))
        self.assertNotIn(token, json.dumps(issues))

    def test_known_secrets_are_read_only_and_reports_are_redacted(self):
        secret = 'unusual-value-without-standard-prefix'
        path = self.write('Credential/model_ai.md', 'api key=' + secret)
        self.assertIn(secret, known_secrets([path.parent]))
        self.assertTrue(secret_issues('notes.md', secret, {secret}))
        self.assertNotIn(secret, redact('response ' + secret, {secret}))
        self.assertEqual(path.read_text(), 'api key=' + secret)

    def test_forced_credential_file_and_outside_symlink_are_rejected(self):
        self.write('Credential/access.md', 'fixture')
        self.git('add', '-f', 'Credential/access.md')
        issues, _ = scan_files(self.root, repository_files(self.root), set())
        self.assertIn('file-lokal-terlarang-di-git', {i['rule'] for i in issues})
        (self.root / 'outside.md').symlink_to(self.root.parent / 'missing')
        issues, _ = scan_files(self.root, ['outside.md'], set())
        self.assertEqual(issues[0]['rule'], 'symlink-tidak-diperiksa')

    def test_markdown_links_fences_and_external_links(self):
        self.write('docs/ada file.md', 'isi')
        good = '[lokal](<docs/ada file.md>) [web](https://example.test/a) [mail](mailto:test@example.invalid)\n'
        good += '[rujuk]: docs/ada%20file.md\n```sh\necho "[bukan link](hilang)"\n```\n'
        self.assertFalse(markdown_issues(self.root, 'README.md', good))
        self.assertTrue(markdown_issues(self.root, 'README.md', '[rusak](tidak-ada.md)'))
        self.assertTrue(markdown_issues(self.root, 'README.md', '```python\nprint(1)'))
        self.assertFalse(markdown_issues(self.root, 'README.md', '````md\n```\n````\n'))

    def test_invalid_python_json_toml_are_rejected(self):
        for name, text in [('x.py', 'def broken('), ('x.json', '{'), ('x.toml', '[x')]:
            self.assertTrue(syntax_issues(name, text))

    def test_docs_command_writes_pass_but_broken_link_returns_failure(self):
        self.write('README.md', '# Baru\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--mode', 'docs', '--base', 'HEAD'], self.root), 0)
        reports = sorted((self.root / 'runtime/verification').glob('*/report.json'))
        data = json.loads(reports[-1].read_text())
        self.assertEqual(data['result'], 'PASS')
        self.assertEqual(data['tests'], 'not-required-docs-only')
        self.write('README.md', '[hilang](missing.md)\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--mode', 'docs', '--base', 'HEAD'], self.root), 1)

    def test_full_command_does_not_pass_when_test_runner_fails(self):
        self.write('scripts/run_tests.py', 'raise SystemExit(1)\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--mode', 'full'], self.root), 1)
        report = next((self.root / 'runtime/verification').glob('*/report.json'))
        self.assertEqual(json.loads(report.read_text())['tests'], 'FAIL')

    def test_runner_blocks_network_and_empty_suite_fails(self):
        runner = Path(__file__).resolve().parents[1] / 'scripts/run_tests.py'
        self.write('scripts/run_tests.py', runner.read_text())
        self.write('tests/test_network.py', 'import socket, unittest\nclass Check(unittest.TestCase):\n'
                   ' def test_network(self):\n'
                   '  with self.assertRaises(OSError): socket.create_connection(("example.invalid", 443))\n')
        import sys
        result = subprocess.run([sys.executable, str(self.root / 'scripts/run_tests.py')], capture_output=True)
        self.assertEqual(result.returncode, 0)
        (self.root / 'tests/test_network.py').unlink()
        result = subprocess.run([sys.executable, str(self.root / 'scripts/run_tests.py')], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
