#!/usr/bin/env python3
"""Validate the planning repository without contacting application services."""

import argparse
import ast
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    'README.md', 'AGENTS.md', 'tools.md', 'memory.md', 'worklog.md',
    'docs/PLAN.md', 'docs/AUDIT.md', 'docs/ACCEPTANCE.md', 'docs/MIGRATION.md',
    'docs/AUDIT_TIKTOK.md', 'docs/AUDIT_FB_YT_DOWNLOADER.md',
    'docs/PLATFORM_CAPABILITIES.md',
)
SECRET_PATTERNS = (
    re.compile(r'\b\d{6,15}:[A-Za-z0-9_-]{25,}\b'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{24,}\b'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
)


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def forbidden(name):
    path = Path(name)
    parts = [p.lower() for p in path.parts]
    return (
        any(p in {'credential', 'credentials', 'runtime', '.venv', 'venv', '__pycache__'} for p in parts)
        or any(p.startswith('bank-konten') or p.startswith('bank konten') for p in parts)
        or path.name.startswith(('.env', 'config.local.', '._'))
        or path.suffix.lower() in {'.mp4', '.mov', '.wav', '.mp3', '.sqlite3', '.db', '.pem', '.key'}
        or any(ext in path.name.lower() for ext in ('.sqlite3-', '.db-'))
    )


def known_secrets(folders):
    values = set()
    for folder in folders:
        if not folder.is_dir():
            raise ValueError('Credential directory unavailable')
        for path in folder.glob('*.md'):
            if path.is_symlink():
                raise ValueError('Credential symlinks are not read')
            source = path.read_text()
            for pattern in SECRET_PATTERNS:
                values.update(pattern.findall(source))
            for key, value in re.findall(r'^\s*([\w -]+)\s*[:=]\s*(.*?)\s*$', source, re.M):
                if any(word in key.lower() for word in ('key', 'token', 'secret')):
                    value = value.strip(' `\"\'')
                    if len(value) >= 8:
                        values.add(value)
    return values


def source_issues(name, data, secrets, version):
    issues = []

    def issue(rule):
        issues.append({'file': name, 'version': version, 'rule': rule})

    if len(data) > 2_000_000 or b'\0' in data:
        issue('non-text-or-oversize')
        return issues
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        issue('not-utf8')
        return issues
    if any(value in text for value in secrets) or any(p.search(text) for p in SECRET_PATTERNS):
        issue('possible-secret')
    if name.endswith('.py'):
        try:
            ast.parse(text, filename=name)
        except SyntaxError:
            issue('python-syntax')
    if name.endswith('.md'):
        if len(re.findall(r'^\s*```', text, re.M)) % 2:
            issue('unclosed-code-fence')
        if '\u2014' in text:
            issue('em-dash-copy')
        for target in re.findall(r'\[[^\]\n]*\]\(([^\n]+?)\)', text):
            target = target.strip().strip('<>').split('#', 1)[0]
            if not target or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
                continue
            if not (ROOT / name).parent.joinpath(target).exists():
                issue('missing-local-link')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-dir', action='append', type=Path, default=[])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    at = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report = {'at': at, 'result': 'FAIL', 'scope': 'planning-documents-only',
              'application_tests': 'not-implemented', 'issues': []}
    try:
        secrets = known_secrets(args.credentials_dir)
        names = set(git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''}
        staged = set(git('ls-files', '-z').decode().split('\0')) - {''}
        for name in REQUIRED:
            if name not in names or not (ROOT / name).is_file():
                report['issues'].append({'file': name, 'rule': 'required-document-missing'})
        for name in sorted(names):
            path = ROOT / name
            if forbidden(name) or path.is_symlink():
                report['issues'].append({'file': name, 'rule': 'forbidden-file'})
                continue
            if path.is_file():
                report['issues'] += source_issues(name, path.read_bytes(), secrets, 'working')
            else:
                report['issues'].append({'file': name, 'rule': 'missing-file'})
            if name in staged:
                report['issues'] += source_issues(name, git('show', ':' + name), secrets, 'index')
        for args_diff in (('diff', '--check'), ('diff', '--cached', '--check')):
            result = subprocess.run(['git', '-C', str(ROOT), *args_diff], capture_output=True)
            if result.returncode:
                report['issues'].append({'rule': 'git-diff-check'})
        report.update(head=git('rev-parse', 'HEAD').decode().strip(), files=len(names),
                      secret_values_checked=len(secrets))
        report['result'] = 'FAIL' if report['issues'] else 'PASS'
    except Exception as exc:
        report['issues'].append({'rule': 'verification-error', 'error_type': type(exc).__name__})
    output = args.output or Path('runtime/verification') / ('plan-' + at + '.json')
    output = (ROOT / output).resolve()
    if not output.is_relative_to((ROOT / 'runtime/verification').resolve()) or output.exists():
        parser.error('Output must be new and inside runtime/verification')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{report['result']}: planning documents, {len(report['issues'])} issue(s); {output}")
    if report['issues']:
        print(json.dumps(report['issues'], ensure_ascii=False))
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
