# Contributing

This repository is published so that the work can be read and reviewed; see [LICENSE](LICENSE).
Questions, bug reports and suggestions are welcome as GitHub issues. Please do not open pull
requests without asking first: changes are made in the development repositories and copied here
(below), so a pull request cannot be merged as it is.

## Checking a change

Each project has its own rules, which still apply:
[amr-console-dev/CONTRIBUTING.md](amr-console-dev/CONTRIBUTING.md). In short:

```bash
bash amr-console-dev/scripts/check.sh                       # console: five offline suites
(cd amr-language && python3 -m pytest -q -p no:cacheprovider tests)
python3 release/sync.py --check-only                        # no private paths, keys or addresses
```

Results are "in simulation" unless they were measured on a robot. Say what was tested and what
was not.

## How this repository is updated (maintainer)

`amr-console-dev/` and `amr-language/` are copies of the committed files of two development
repositories, less what [release/manifest.json](release/manifest.json) leaves out (measurement
records, dated development reports). With the development repositories side by side:

```bash
python3 release/sync.py --console ../amr-console-dev --language ../amr-language
git status && git diff --stat                               # review what changed
bash amr-console-dev/scripts/check.sh && (cd amr-language && python3 -m pytest -q tests)
git add -A && git commit                                    # then push when you decide to
```

`sync.py` copies only committed files (`git archive`), refuses links, and stops before writing
anything if a file would publish one of the patterns in the manifest (home-folder paths, keys
and tokens of common services, Indian phone and SRM register numbers), an e-mail address that
is not on the allowed list, or a file over 3 MB. It replaces only files committed here: what git
ignores (a model runner in `.runtime/`, `records/`, `data/`) is left alone, and untracked files
stop it. It does not read prose: whether a document still reads like a private note is for the
person committing to judge. `release/source.json` records a digest of what was
copied from each development repository; `--verify` checks that this tree and that record still
match the development repositories.
