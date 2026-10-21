"""Local and CI checks; reports contain locations and rules, never matching secret values."""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    'telegram-token': r'\b\d{6,15}:[A-Za-z0-9_-]{25,}\b',
    'github-token': r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
    'api-token': r'\bsk-[A-Za-z0-9_-]{24,}\b',
    'google-key': r'\bAIza[0-9A-Za-z_-]{30,}\b',
    'aws-key': r'\bAKIA[0-9A-Z]{16}\b',
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'secret-assignment': r'''(?i)\b(?:api[_ -]?key|access[_ -]?key|secret[_ -]?key|access[_ ]?token|refresh[_ ]?token|client[_ ]?secret|password)["']?\s*[:=]\s*["']?([A-Za-z0-9_./+:-]{20,})''',
}
FORBIDDEN_PARTS = {'credential', 'credentials', 'runtime', 'audit', 'bank konten', 'bank konten-fb-ytb', 'bank-konten-fahra-automation-tiktok-ig-fb-ytb', '.venv', '__pycache__'}
VETTED_BINARY_ASSETS = {
    'fahra_automation/assets/fonts/BarlowSemiCondensed-Bold.ttf':
        '5bd6757e459a110da81b44532f0f43058e2dab2401688abca84f2fdbde193cbb',
}


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.PIPE)


def paths(raw):
    return {p.decode('utf-8') for p in raw.split(b'\0') if p}


def changed_paths(root, base):
    result = paths(git(root, 'ls-files', '--others', '--exclude-standard', '-z'))
    result |= paths(git(root, 'diff', '--name-only', '-z', 'HEAD'))
    if base and set(base) != {'0'}:
        commit = git(root, 'rev-parse', '--verify', base + '^{commit}').decode().strip()
        result |= paths(git(root, 'diff', '--name-only', '-z', commit, 'HEAD'))
        return result, commit
    return result, None


def select_mode(requested, changed):
    docs_only = bool(changed) and all(Path(p).suffix.lower() == '.md' for p in changed)
    if requested == 'docs' and not docs_only:
        raise ValueError('Mode docs membutuhkan perubahan Markdown saja dan base/diff yang jelas.')
    return ('docs' if docs_only else 'full') if requested == 'auto' else requested


def known_secrets(folders):
    values = set()
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.rglob('*'):
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
                continue
            source = path.read_text(errors='replace').replace('\\_', '_')
            if path.name in {'repliz-webhook-token.txt', 'cloudflared-tunnel-token.txt'}:
                value = source.strip()
                if re.fullmatch(r'[\x21-\x7e]{32,2048}', value):
                    values.add(value)
            pairs = {}
            for match in re.finditer(r'''(?im)^\s*(access_key|secret_key|api[_ -]?key)\s*[:=]\s*["'`]?([^\s"'`]+)''', source):
                pairs[match[1]] = match[2]
                if len(match[2]) >= 8:
                    values.add(match[2])
            values.update(re.findall(SECRET_PATTERNS['telegram-token'], source))
            if pairs.get('access_key') and pairs.get('secret_key'):
                values.add(base64.b64encode((pairs['access_key'] + ':' + pairs['secret_key']).encode()).decode())
            if path.suffix == '.json':
                def collect(obj):
                    if isinstance(obj, dict):
                        for key, value in obj.items():
                            if key in {'token', 'access_token', 'refresh_token', 'client_secret', 'private_key'} and isinstance(value, str) and len(value) >= 8:
                                values.add(value)
                            else:
                                collect(value)
                    elif isinstance(obj, list):
                        for value in obj:
                            collect(value)
                collect(json.loads(source))
    return values


def secret_issues(name, text, secrets):
    issues = []
    for number, line in enumerate(text.splitlines(), 1):
        rules = [rule for rule, pattern in SECRET_PATTERNS.items() if re.search(pattern, line)]
        if any(value in line for value in secrets):
            rules.append('credential-aktual')
        for rule in rules:
            issues.append({'file': name, 'line': number, 'rule': rule})
    # Multiline private keys and JSON-escaped credentials need a whole-file comparison too.
    if any(value in text or json.dumps(value)[1:-1] in text for value in secrets):
        if not any(i['rule'] == 'credential-aktual' for i in issues):
            issues.append({'file': name, 'rule': 'credential-aktual'})
    return issues


def repository_files(root):
    return sorted(paths(git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z'))
                  | VETTED_BINARY_ASSETS.keys())


def scan_blob(name, raw, secrets, expected_digest=None, *, index=False):
    label = name + ' (index)' if index else name
    if len(raw) > 2_000_000:
        return [{'file': label, 'rule': 'file-lebih-dari-2mb'}], None
    if expected_digest is not None:
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            return [{'file': label, 'rule': 'aset-biner-hash-tidak-cocok'}], None
        return [], None
    try:
        source = raw.decode('utf-8')
    except UnicodeDecodeError:
        return [{'file': label, 'rule': 'index-bukan-teks' if index else 'file-bukan-teks'}], None
    issues = [{'file': label, 'rule': 'nul-di-file-teks'}] if '\0' in source else []
    return issues + secret_issues(label, source, secrets), source


def scan_files(root, names, secrets):
    issues, contents = [], {}
    for name in names:
        path = root / name
        expected_digest = VETTED_BINARY_ASSETS.get(name)
        if any(part.casefold() in FORBIDDEN_PARTS for part in Path(name).parts) or path.name in {'config.local.toml', 'repliz.md'} or path.name.startswith('.env'):
            issues.append({'file': name, 'rule': 'file-lokal-terlarang-di-git'})
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            issues.append({'file': name, 'rule': 'symlink-tidak-diperiksa'})
        elif path.exists():
            with path.open('rb') as source_file:
                raw = source_file.read(2_000_001)
            working_issues, source = scan_blob(name, raw, secrets, expected_digest)
            issues += working_issues
            if source is not None:
                contents[name] = source
        elif expected_digest is not None:
            issues.append({'file': name, 'rule': 'aset-biner-wajib-hilang'})
        # Inspect the index too: a secret removed in the working copy may still be staged.
        try:
            staged = git(root, 'show', ':' + name)
        except subprocess.CalledProcessError:
            if expected_digest is not None:
                try:
                    git(root, 'cat-file', '-e', 'HEAD:' + name)
                except subprocess.CalledProcessError:
                    pass
                else:
                    issues.append({'file': name + ' (index)', 'rule': 'aset-biner-index-hilang'})
            continue
        staged_issues, _ = scan_blob(name, staged, secrets, expected_digest, index=True)
        issues += staged_issues
    return issues, contents


def markdown_issues(root, name, source):
    issues, visible, fence = [], [], None
    for number, line in enumerate(source.splitlines(), 1):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if fence is None:
            visible.append((number, line))
    if fence:
        issues.append({'file': name, 'rule': 'code-fence-belum-ditutup'})
    for number, line in visible:
        targets = re.findall(r'\]\((<[^>]+>|[^)]+)\)', line)
        reference = re.match(r'^\s{0,3}\[[^]]+\]:\s*(<[^>]+>|\S+)', line)
        if reference:
            targets.append(reference[1])
        for target in targets:
            target = target[1:-1] if target.startswith('<') else re.split(r'\s+["\']', target, maxsplit=1)[0]
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            target = unquote(parsed.path)
            if not target or target.startswith('/'):
                continue
            if not (root / name).parent.joinpath(target).exists():
                issues.append({'file': name, 'line': number, 'rule': 'tautan-lokal-tidak-ditemukan'})
    return issues


def syntax_issues(name, source):
    try:
        if name.endswith('.py'):
            ast.parse(source, filename=name)
        elif name.endswith('.json'):
            json.loads(source)
        elif name.endswith('.toml'):
            tomllib.loads(source)
    except (SyntaxError, ValueError) as exc:
        return [{'file': name, 'line': getattr(exc, 'lineno', None), 'rule': 'sintaks-tidak-valid'}]
    return []


def redact(text, secrets):
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    for pattern in SECRET_PATTERNS.values():
        text = re.sub(pattern, '[REDACTED]', text)
    return text


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description='Periksa perubahan lokal/CI tanpa publikasi.')
    parser.add_argument('--mode', choices=('auto', 'full', 'docs'), default='auto')
    parser.add_argument('--base', help='Commit dasar perbandingan; tanpa base memakai perubahan lokal.')
    parser.add_argument('--output', type=Path, help='Folder laporan baru di runtime/verification/.')
    parser.add_argument('--secrets-from', type=Path, action='append', default=[], help='Folder kredensial tambahan, dibaca lokal saja.')
    args = parser.parse_args(argv)
    root = root.resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = args.output or root / 'runtime/verification' / ('verify-' + stamp)
    if not output.is_absolute():
        output = root / output
    if not output.resolve().is_relative_to(root / 'runtime/verification') or output.exists():
        parser.error('Folder laporan harus baru dan berada di runtime/verification/.')
    output.mkdir(parents=True)
    report = {'at': stamp, 'result': 'FAIL', 'checks': {}, 'issues': [], 'tests': 'not-run'}
    secrets = set()
    try:
        for folder in args.secrets_from:
            if not folder.is_dir():
                raise ValueError('Folder --secrets-from tidak tersedia.')
        secrets = known_secrets([root / 'Credential', root / 'credentials', *args.secrets_from])
        changed, base = changed_paths(root, args.base)
        mode = select_mode(args.mode, changed)
        report.update(mode=mode, base=base, head=git(root, 'rev-parse', 'HEAD').decode().strip(),
                      changed_files=sorted(changed), known_secret_count=len(secrets))
        issues, contents = scan_files(root, repository_files(root), secrets)
        report['checks']['secrets_and_files'] = not issues
        report['issues'] += issues
        for name, source in contents.items():
            report['issues'] += syntax_issues(name, source)
            if name.endswith('.md'):
                report['issues'] += markdown_issues(root, name, source)
            if name.endswith('.sh') and subprocess.run(['bash', '-n', str(root / name)], capture_output=True).returncode:
                report['issues'].append({'file': name, 'rule': 'sintaks-shell-tidak-valid'})
        report['checks']['syntax_and_docs'] = not report['issues']
        diffs = [('diff', '--check'), ('diff', '--cached', '--check')]
        if base:
            diffs.append(('diff', '--check', base, 'HEAD'))
        for args_diff in diffs:
            if subprocess.run(['git', '-C', str(root), *args_diff], capture_output=True).returncode:
                report['issues'].append({'rule': 'git-diff-check-gagal'})
        report['checks']['diff'] = not any(i['rule'] == 'git-diff-check-gagal' for i in report['issues'])
        if not report['issues'] and mode == 'full':
            try:
                result = subprocess.run([sys.executable, str(root / 'scripts/run_tests.py')], cwd=root,
                                        capture_output=True, text=True, timeout=300)
                (output / 'tests.log').write_text(redact(result.stdout + result.stderr, secrets))
                report['tests'] = 'PASS' if result.returncode == 0 else 'FAIL'
                if result.returncode:
                    report['issues'].append({'rule': 'unit-test-gagal'})
            except subprocess.TimeoutExpired:
                report['tests'] = 'FAIL'
                report['issues'].append({'rule': 'unit-test-timeout'})
        elif not report['issues']:
            report['tests'] = 'not-required-docs-only'
        report['result'] = 'PASS' if not report['issues'] else 'FAIL'
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report['issues'].append({'rule': 'verifikasi-tidak-dapat-dijalankan', 'type': type(exc).__name__})
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{report['result']}: mode={report.get('mode', 'unknown')}, tes={report['tests']}, temuan={len(report['issues'])}")
    for issue in report['issues'][:20]:
        print(f"- {issue.get('file', 'pemeriksaan')}:{issue.get('line', '')} {issue['rule']}")
    print('Laporan: ' + str(output / 'report.json'))
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
