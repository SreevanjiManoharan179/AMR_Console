"""release/sync.py: what is copied, what is refused, and what the checks catch."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

_spec = importlib.util.spec_from_file_location('release_sync', Path(__file__).resolve().parents[1] / 'sync.py')
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

MANIFEST = sync.load_manifest()
CHECKS = MANIFEST['checks']


def git(repository, *args):
    subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True)


def repository(path, files, executable=()):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if name in executable:
            target.chmod(0o755)
    git(path, 'add', '-A')
    git(path, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'files')
    return path


@pytest.fixture
def sources(tmp_path):
    console = repository(tmp_path / 'dev/amr-console-dev', {
        'README.md': 'console\n', 'setup/run.sh': 'echo run\n', '.github/workflows/x.yml': 'on: push\n'},
        executable=('setup/run.sh',))
    language = repository(tmp_path / 'dev/amr-language', {
        'README.md': 'language\n', 'records/task3/a.json': '{}\n', 'docs/history/old.md': 'old\n',
        'docs/README.md': 'docs\n', 'scripts/write_task3_handoff.py': 'x = 1\n'})
    (language / 'untracked.txt').write_text('not committed\n')
    return {'amr-console-dev': console, 'amr-language': language}


def test_only_committed_files_less_the_manifest_patterns_are_copied(sources):
    files, report = sync.build(sources, MANIFEST)
    assert sorted(files) == ['amr-console-dev/README.md', 'amr-console-dev/setup/run.sh',
                             'amr-language/README.md', 'amr-language/docs/README.md']
    assert files['amr-console-dev/setup/run.sh'][0] == 0o755
    assert files['amr-language/README.md'][0] == 0o644
    assert report['amr-language']['left_out'] == {'records/**': 1, 'docs/history/**': 1,
                                                  'scripts/write_task3_handoff.py': 1, 'scripts/report_task4b.py': 0}
    assert report['amr-console-dev']['left_out'] == {'.github/**': 1, 'docs/records/**': 0}
    assert report['amr-language']['published_sha256'] == sync.digest(
        {path: value for path, value in files.items() if path.startswith('amr-language/')})


def test_the_digest_follows_content_mode_and_path_only():
    a = {'p/x': (0o644, b'one'), 'p/y': (0o755, b'two')}
    assert sync.digest(a) == sync.digest(dict(reversed(list(a.items()))))
    assert sync.digest(a) != sync.digest({**a, 'p/x': (0o755, b'one')})
    assert sync.digest(a) != sync.digest({**a, 'p/x': (0o644, b'One')})
    assert sync.digest(a) != sync.digest({'p/z': a['p/x'], 'p/y': a['p/y']})


def public_repository(tmp_path, ignore='log/\n.runtime/\n'):
    root = tmp_path / 'public'
    root.mkdir()
    git(root, 'init', '-q')
    (root / '.gitignore').write_text(ignore)
    return root


def commit_all(root):
    git(root, 'add', '-A')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'release')


def test_write_then_verify_and_a_changed_file_is_reported(sources, tmp_path):
    root = public_repository(tmp_path)
    (root / 'amr-language' / 'stale').mkdir(parents=True)
    (root / 'amr-language' / 'stale' / 'gone.txt').write_text('deleted in development\n')
    commit_all(root)
    files, _ = sync.build(sources, MANIFEST)
    sync.write(files, MANIFEST['projects'], root)
    assert not (root / 'amr-language/stale').exists()
    actual = {}
    for name in MANIFEST['projects']:
        actual.update(sync.tree_files(root, name))
    assert sync.compare(files, actual) == []
    (root / 'amr-console-dev/README.md').write_text('edited here\n')
    (root / 'amr-language/extra.txt').write_text('new\n')
    actual = {}
    for name in MANIFEST['projects']:
        actual.update(sync.tree_files(root, name))
    assert sync.compare(files, actual) == ['differs: amr-console-dev/README.md', 'only here: amr-language/extra.txt']


def test_ignored_files_survive_a_copy_and_untracked_ones_stop_it(sources, tmp_path):
    root = public_repository(tmp_path)
    files, _ = sync.build(sources, MANIFEST)
    sync.write(files, MANIFEST['projects'], root)
    commit_all(root)
    model = root / 'amr-language/.runtime/models/blob'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'ten gigabytes of model')
    sync.write(files, MANIFEST['projects'], root)
    assert model.read_bytes() == b'ten gigabytes of model'
    (root / 'amr-console-dev/notes.txt').write_text('mine\n')
    with pytest.raises(sync.ReleaseError, match='untracked'):
        sync.write(files, MANIFEST['projects'], root)
    assert (root / 'amr-console-dev/notes.txt').exists()


def test_a_folder_outside_git_is_never_replaced(sources, tmp_path):
    root = tmp_path / 'plain'
    (root / 'amr-language').mkdir(parents=True)
    files, _ = sync.build(sources, MANIFEST)
    with pytest.raises(sync.ReleaseError, match='not a git repository'):
        sync.write(files, MANIFEST['projects'], root)


def test_a_link_in_a_development_repository_is_refused(sources):
    link = sources['amr-language'] / 'link'
    link.symlink_to('README.md')
    git(sources['amr-language'], 'add', 'link')
    git(sources['amr-language'], '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-qm', 'link')
    with pytest.raises(sync.ReleaseError, match='link'):
        sync.build(sources, MANIFEST)


def test_a_missing_repository_is_an_error(tmp_path):
    with pytest.raises(sync.ReleaseError):
        sync.committed_files(tmp_path / 'nothing')


@pytest.mark.parametrize('text,label', [
    ('see /home/' + 'alice/AMR_Console for the files', 'home folder'),
    ('-----BEGIN OPENSSH ' + 'PRIVATE KEY-----', 'private key'),
    ('token = ghp_' + 'a' * 36, 'GitHub token'),
    ('AKIA' + 'B' * 16, 'AWS'),
    ('hf_' + 'c' * 34, 'Hugging Face'),
    ('key sk-ant-' + 'd' * 30, 'Anthropic'),
    ('mail me: someone.private@' + 'gmail.com', 'e-mail'),
    ('C:\\' + 'Users\\alice\\AMR', 'home folder'),
    ('/Users/' + 'alice/AMR', 'home folder'),
    ('/media/' + 'alice/usb', 'home folder'),
    ('xoxb-' + '1' * 20, 'Slack'),
    ('glpat-' + 'e' * 20, 'GitLab'),
    ('AIza' + 'f' * 35, 'Google'),
    ('call +91 ' + '98765' + '43210', 'phone'),
    ('RA' + '2512053010003', 'register'),
])
def test_the_checks_catch_private_material(text, label):
    found = sync.problems({'amr-language/x.md': (0o644, (text + '\n').encode())}, CHECKS)
    assert len(found) == 1 and found[0].startswith('amr-language/x.md:1:') and label.split()[0].lower() in found[0].lower()


@pytest.mark.parametrize('text', [
    'maintainer: Name <Someone123@users.noreply.github.com>',
    'an example: person@example.com',
    "('ruby', ['ruby', '/home/user/app.rb'])",
    'ordinary text, decorators like @pytest.fixture and a@b are not addresses',
    'sizes and times: 2553503826 bytes, 1794286560.318, 0.9876543210, id 79876543210',
    'hash 7d68c3c991ea9876543210c1 and version 1.2.9876543210',
])
def test_the_checks_allow_ordinary_text(text):
    assert sync.problems({'amr-console-dev/x.py': (0o644, (text + '\n').encode())}, CHECKS) == []


def test_binary_and_utf16_files_are_searched_too():
    binary = b'\x89PNG\0\x01tEXt someone.private@' b'gmail.com /home/' b'alice/'
    found = sync.problems({'amr-console-dev/icon.png': (0o644, binary)}, CHECKS)
    assert len(found) == 2 and all(f.startswith('amr-console-dev/icon.png:1:') for f in found)
    utf16 = ('note: someone.private@' + 'gmail.com\n').encode('utf-16')
    assert len(sync.problems({'amr-console-dev/note.txt': (0o644, utf16)}, CHECKS)) == 1
    big = b'\0' * (CHECKS['max_file_bytes'] + 1)
    assert len(sync.problems({'amr-console-dev/big.npz': (0o644, big)}, CHECKS)) == 1


def test_a_link_in_this_tree_is_a_finding():
    assert sync.problems({'amr-language/link': (0, b'')}, CHECKS) == ['amr-language/link: a link; links are not published']


def test_the_published_tree_passes_its_own_checks():
    """The repository as committed: what CI runs with --check-only."""
    assert sync.problems(sync.tree_files(), CHECKS) == []


def test_manifest_refuses_a_project_folder_that_could_overwrite_the_repository(tmp_path):
    bad = dict(MANIFEST, projects={'.git': {}})
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(bad))
    with pytest.raises(sync.ReleaseError):
        sync.load_manifest(path)


def test_in_a_git_repository_ignored_output_is_not_compared_but_new_files_are(sources, tmp_path):
    root = tmp_path / 'public'
    root.mkdir()
    git(root, 'init', '-q')
    (root / '.gitignore').write_text('log/\n')
    files, _ = sync.build(sources, MANIFEST)
    sync.write(files, MANIFEST['projects'], root)
    (root / 'amr-console-dev/log').mkdir()
    (root / 'amr-console-dev/log/simulation.lock').write_text('')
    actual = {}
    for name in MANIFEST['projects']:
        actual.update(sync.tree_files(root, name))
    assert sync.compare(files, actual) == []
    (root / 'amr-console-dev/new.txt').write_text('would be committed\n')
    assert sync.compare(files, sync.tree_files(root, 'amr-console-dev') | sync.tree_files(root, 'amr-language')) == [
        'only here: amr-console-dev/new.txt']
