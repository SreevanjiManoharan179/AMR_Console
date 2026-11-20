#!/usr/bin/env python3
"""Copy the development repositories' committed files into this public repository, then check it.

    python3 release/sync.py --console ../amr-console-dev --language ../amr-language
    python3 release/sync.py --console ... --language ... --verify   # compare only, write nothing
    python3 release/sync.py --check-only                            # privacy and size checks only

What is copied is what `git archive <ref>` gives for each development repository: committed
files only, never untracked, ignored or uncommitted work. The patterns in release/manifest.json
then leave some of those out. The privacy and size checks run on the result before anything is
written. Each project folder here is replaced as a whole, so a file deleted in development
disappears here too. Nothing is committed or pushed: review `git status` and `git diff`, then
commit yourself.
"""
import argparse
import fnmatch
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')


class ReleaseError(Exception):
    pass


def load_manifest(path=HERE / 'manifest.json'):
    manifest = json.loads(Path(path).read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1:
        raise ReleaseError('release/manifest.json: unknown schema_version')
    for name in manifest['projects']:
        if '/' in name or name in ('', '.', '..', '.git', '.github', 'release', 'docs'):
            raise ReleaseError(f'release/manifest.json: {name!r} cannot be a project folder')
    return manifest


def excluded(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def committed_files(repository, ref='HEAD'):
    """{path: (mode, bytes)} of the files committed at ref, read with git archive."""
    result = subprocess.run(['git', '-C', str(repository), 'archive', '--format=tar', ref],
                            capture_output=True, check=False)
    if result.returncode:
        raise ReleaseError(f'git archive failed in {repository}: {result.stderr.decode(errors="replace").strip()}')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts:
                raise ReleaseError(f'{repository}: unsafe path in the archive: {member.name}')
            if member.isdir():
                continue
            if not member.isfile():
                raise ReleaseError(f'{repository}: {member.name} is a link; links are not published')
            files[str(name)] = (0o755 if member.mode & 0o111 else 0o644, archive.extractfile(member).read())
    return files


def digest(files):
    """SHA-256 over the sorted (mode, git blob id, path) of files: the same for the same content."""
    lines = []
    for path, (mode, raw) in sorted(files.items()):
        blob = hashlib.sha1(b'blob %d\0' % len(raw) + raw).hexdigest()
        lines.append(f'{mode:o} {blob} {path}\n')
    return hashlib.sha256(''.join(lines).encode()).hexdigest()


def build(sources, manifest, ref='HEAD'):
    """The files this repository should hold for the projects: {relative path: (mode, bytes)}."""
    files, report = {}, {}
    for name, settings in manifest['projects'].items():
        if name not in sources:
            raise ReleaseError(f'no development repository given for {name}')
        patterns = settings.get('exclude', [])
        committed = committed_files(sources[name], ref)
        kept = {path: value for path, value in committed.items() if not excluded(path, patterns)}
        copied = {f'{name}/{path}': value for path, value in kept.items()}
        files.update(copied)
        # Counts per pattern only: the names of files left out are not published.
        left_out = {pattern: sum(1 for path in committed if fnmatch.fnmatchcase(path, pattern)) for pattern in patterns}
        report[name] = {'published': len(kept), 'published_sha256': digest(copied), 'left_out': left_out}
    return files, report


def problems(files, checks):
    """Privacy and size findings for {relative path: (mode, bytes)}; an empty list means none.

    Text files are read as UTF-8 (UTF-16 when they start with its byte-order mark); other files
    are searched through their readable bytes, so text in image metadata is checked too. Prose
    is not judged: whether a document reads as a private note is for a person to decide."""
    allowed = [re.compile(p) for p in checks['allowed_email_patterns']]
    forbidden = {label: re.compile(p) for label, p in checks['forbidden_patterns'].items()}
    found = []
    for path, (mode, raw) in sorted(files.items()):
        if mode == 0:
            found.append(f'{path}: a link; links are not published')
            continue
        if len(raw) > checks['max_file_bytes']:
            found.append(f'{path}: {len(raw)} bytes, more than {checks["max_file_bytes"]}')
        if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
            text = raw.decode('utf-16', errors='replace')
        elif b'\0' in raw[:8192]:
            text = raw.decode('latin-1')
        else:
            text = raw.decode('utf-8', errors='replace')
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in forbidden.items():
                if pattern.search(line):
                    found.append(f'{path}:{number}: {label}')
            for address in EMAIL.findall(line):
                if not any(p.match(address) for p in allowed):
                    found.append(f'{path}:{number}: an e-mail address that is not on the allowed list')
    return found


def tree_files(root=ROOT, top=None):
    """{relative path: (mode, bytes)} of the files a commit here could hold.

    In a git repository: the tracked files and the untracked ones git does not ignore (what
    `git add -A` would take), so ignored test and build output is not counted. Elsewhere: every
    file except .git and __pycache__."""
    root = Path(root)
    if (root / '.git').exists():
        listed = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others',
                                 '--exclude-standard', '--', top or '.'], capture_output=True, check=True).stdout
        paths = sorted({root / name for name in listed.decode('utf-8', 'surrogateescape').split('\0') if name})
    else:
        base = root if top is None else root / top
        paths = [path for path in sorted(base.rglob('*'))
                 if path.relative_to(root).parts[0] != '.git' and '__pycache__' not in path.relative_to(root).parts]
    files = {}
    for path in paths:
        relative = path.relative_to(root)
        if path.is_symlink():
            files[str(relative)] = (0, b'')            # reported as a difference by compare
        elif path.is_file():
            files[str(relative)] = (0o755 if path.stat().st_mode & 0o111 else 0o644, path.read_bytes())
    return files


def compare(expected, actual):
    lines = []
    for path in sorted(set(expected) | set(actual)):
        if path not in actual:
            lines.append(f'missing here: {path}')
        elif path not in expected:
            lines.append(f'only here: {path}')
        elif expected[path] != actual[path]:
            lines.append(f'differs: {path}' + (' (mode)' if expected[path][1] == actual[path][1] else ''))
    return lines


def write(files, projects, root=ROOT):
    """Make each project folder hold exactly these files, leaving files git ignores alone.

    Only files committed here are replaced or removed: what git ignores (a model runner in
    .runtime/, records/, data/, build output) stays. Untracked files that git would commit stop
    the copy, so nothing of yours is deleted and nothing unreviewed is published."""
    root = Path(root)
    git_repository = (root / '.git').exists()
    for name in projects:
        target = root / name
        if target.is_symlink() or (target.exists() and target.resolve().parent != root.resolve()):
            raise ReleaseError(f'{name} is not a folder directly in {root}')
        if not target.exists():
            continue
        if not git_repository:
            raise ReleaseError(f'{root} is not a git repository, so {name}/ cannot be replaced safely')
        listing = lambda *options: [line for line in subprocess.run(
            ['git', '-C', str(root), 'ls-files', '-z', *options, '--', name],
            capture_output=True, check=True).stdout.decode('utf-8', 'surrogateescape').split('\0') if line]
        untracked = listing('--others', '--exclude-standard')
        if untracked:
            raise ReleaseError(f'untracked files in {name}/ would be published: ' + ', '.join(untracked[:5])
                               + ('...' if len(untracked) > 5 else '') + '; commit, ignore or move them first')
        for path in listing('--cached'):
            if path not in files and (root / path).is_file():
                (root / path).unlink()
    for path, (mode, raw) in sorted(files.items()):
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        destination.write_bytes(raw)
        destination.chmod(mode)
    for name in projects:
        for directory in sorted((root / name).rglob('*'), reverse=True):
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--console', type=Path, help='the amr-console-dev development repository')
    parser.add_argument('--language', type=Path, help='the amr-language development repository')
    parser.add_argument('--ref', default='HEAD', help='commit to copy from both repositories (default HEAD)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify', action='store_true', help='compare with this tree; write nothing')
    mode.add_argument('--check-only', action='store_true', help='only check the files already here')
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest()
        if args.check_only:
            found = problems(tree_files(), manifest['checks'])
            print('\n'.join(found) if found else 'Checks passed: no private paths, keys or e-mail addresses, no oversized files.')
            return 1 if found else 0
        if not (args.console and args.language):
            parser.error('--console and --language are needed unless --check-only is given')
        sources = {'amr-console-dev': args.console, 'amr-language': args.language}
        files, report = build(sources, manifest, args.ref)
        found = problems(files, manifest['checks'])
        if found:
            print('Not copied: the development files would publish the following.\n' + '\n'.join(found))
            return 1
        # What was copied, by content; the counts of files left out are printed, not recorded.
        record = {'ref': args.ref, 'projects': {name: {key: item[key] for key in ('published', 'published_sha256')}
                                                for name, item in report.items()}}
        if args.verify:
            actual = {}
            for name in manifest['projects']:
                actual.update(tree_files(ROOT, name))
            differences = compare(files, actual)
            recorded = json.loads((HERE / 'source.json').read_text()) if (HERE / 'source.json').exists() else {}
            for name, item in report.items():
                if recorded.get('projects', {}).get(name, {}).get('published_sha256') != item['published_sha256']:
                    differences.append(f'release/source.json does not record these files for {name}')
            print('\n'.join(differences) if differences else 'Identical: this tree is what the development repositories give.')
            return 1 if differences else 0
        write(files, manifest['projects'])
        (HERE / 'source.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        for name, item in report.items():
            print(f'{name}: {item["published"]} committed files copied; left out by release/manifest.json: '
                  + (', '.join(f'{count} {pattern}' for pattern, count in item['left_out'].items()) or 'none') + '.')
        print('Review with git status and git diff, then commit. Nothing was committed or pushed.')
        return 0
    except ReleaseError as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
