# Changelog

## 1.0.1 (20 Nov 2026)

- README: an animated demo of one office task from request to return (4.5× speed), four
  screenshots of the system at work (office tasks, navigation, a clarifying question, the demo
  office) and the office map with its people and places, in `docs/images/`.
- CI runs for pushes to `main` and for pull requests, not twice for every pull-request branch.
- Dependabot proposes new versions of the GitHub Actions only. The Python test requirements are
  broad ranges on purpose; its first pull requests raised their lower bounds (pytest to 9.2, for
  example, which would exclude the pytest 7 that Ubuntu 24.04 ships), which only narrows them. CI
  already tests the newest allowed versions on every run.

## 1.0.0 (prepared 20 Nov 2026)

First public version: the system as it stood at the end of the case study (development tag
`case-study-final`), with the release fixes below.

- **Language module 0.8.1.** Automated tests no longer leave "owner_row" learning suggestions
  in the owner's review queue (`records/learning_suggestions/`): the module honours
  `AMR_LANGUAGE_RECORDS_DIR`, its tests set it to a temporary folder, and the console sets it for
  the language worker in its own automated tests. Tests that check the saved measurement records
  skip cleanly where `records/` is absent and still fail where a record is missing. A test no
  longer needs the translation environment installed in `.runtime/`.
- **Speech Lab scoring.** A key word now counts only where it starts a word of what was heard
  ("ready" is no longer found in "already", "3" no longer in "13" or "30"); suffixes still count.
  New tests cover text normalisation, word alignment, word and character errors, key words and
  the signal figures, and run as the fifth suite of `scripts/check.sh`. Speech Lab takes recorded
  before this change keep their stored key-word counts.
- **Text.** The port-in-use message no longer names a console that is not part of this
  repository; the office settings no longer say the console works in four languages; the
  model-runner install note gives the module folder instead of a home-folder path.
- **Notices.** NOTICE and the Apache License 2.0 text for the three Nav2 parameter files derived
  from `nav2_bringup`; THIRD_PARTY_LICENSES.md; the ROS package's maintainer address is a GitHub
  no-reply address.
- **Repository.** README, installation guide, security policy, contribution notes, citation
  file, CI for both projects, Dependabot, and `release/sync.py` with its manifest and checks.
- After an independent review: install and design documents no longer read as chat notes or
  pending requests (the installs they describe were approved and done); dated development notes
  are left out like the language module's history; `setup/run_console.sh` prints the console
  link when `AMR_CONSOLE_NO_BROWSER=1` (it showed no link at all before); the typed-command
  parser is rule-based by default even when `OPENAI_API_KEY` is set (the console already chose
  that mode); stale paths and hashes in documents corrected; `pandas` dropped from the test
  requirements (nothing imports it); IndicTrans2 and FLEURS added to THIRD_PARTY_LICENSES.md;
  `release/sync.py` never removes files git ignores, stops on untracked files, flags links and
  searches binary and UTF-16 files too.
- Unused imports removed from test files. Product code keeps its imports: some are imported
  for their side effects or used by other modules and by tests.
