# Check the office layer in your own words

[checked] This is typed input in a text-only conversation rehearsal, with no robot. No model weights are trained. The agreement rule is provisional, not real calibrated coverage. Questions/messages/answers appear on screen; spoken-role lines always use fixed templates, per the owner's choice.

## Start a rehearsal

[checked] From this folder, start the private local server in a separate terminal if it is not running: `python3 scripts/local_runner.py serve`. No new installation or model pull is needed. Before a warm rehearsal, run `python3 scripts/warm_office_model.py --contestant A`; this records startup and load separately in a new timestamped file rather than overwriting October evidence. Read `OFFICE_LANGUAGE_CHECK.md` first: the current four-language target has not passed; slow/refused requests are expected in this development version.

```bash
python3 scripts/rehearse_job.py --requester Bharathwaj --language ta --names data/office_names_example.yaml
```

[checked] Type your own request. The tool shows Thinking, the English rewrite, plan data, fixed readback and decision. For act, type an explicit yes (or the supported Tamil equivalent). An ask/refuse result does not execute any plan prefix. At each destination, type the person's reply. At a reply prompt:

- `/queue Ravi en Give the file to Divya.` adds a second text-rehearsal request; it waits for the current job and gets its own confirmation. The displayed four-minute estimate is an illustrative rehearsal number, not measured queue timing.
- `/person Priya` identifies the current respondent at a shared place and switches to that person's draft profile language. Without an identified person, a shared place uses the requester's language as an explicit fallback; the console must provide interlocutor context for real per-person language choice.
- Any listed stop phrase in any of the four languages halts the rehearsal and prevents a later automatic home return.

[checked] Logs go to `records/rehearsals/`. Scripted starter sessions are marked STARTER_SCRIPTED; normal interactive sessions are UNREVIEWED_TYPED until the owner rates them. Do not call them real-world accuracy merely because a model ran. The draft example roster's language assignments and native aliases need confirmation; it is separate from the unchanged console roster.

[checked] For a single request:

```bash
python3 scripts/try_command.py --requester Bharathwaj --language en 'Give the file to Divya.'
```

[checked] The default request-to-readback deadline is five seconds. `--timeout 30` is an explicitly labelled diagnostic option for seeing a longer plan; it does not satisfy the five-second target. With B approved/installed, add `--settings data/office_model_B.json`. Do not download anything from this how-to.

## Fill and review the sheets

[checked] `data/owner_check.csv` has 40 original authored STARTER requests, 30 authored STARTER-MIXED variants, and 40 blank OWNER rows. Original fields/wording are preserved; the new notes column records ambiguities and native-review status. Mixed rows are not owner collection. Fill at least ten OWNER rows per language with exact words from you/friends, including four long requests with at least three steps. Use speaker pseudonyms. Put the intended English meaning and steps in plain words. Keep a rejected/ambiguous request if that is what you actually said; do not silently clean it up.

[checked] `data/replies_check.csv` contains authored short-reply fixtures, not recordings or independently collected people’s replies. Review their expected kinds and add actual variants when available. Non-English entries need native-speaker check. An ambiguous negative such as “இல்ல” depends on `expect`; if both no and not_here are plausible, the parser returns unclear rather than guessing.

[checked] Proposed starter JSON is in `data/owner_labels_proposed.json`; `data/owner_labels_review.csv` is the label-review sheet. The owner chose to **keep labels pending**. Real OWNER rows have not been supplied, so there are no derived confirmed labels for them. After filling them, have their JSON labels derived and reviewed before evaluation/freeze. Owner meaning ratings for an actual model run go in that run's `owner_results_review.csv`, not in an automatic model-agreement score.

[checked] `python3 scripts/freeze_office_checks.py --check-only` reports missing data and current hashes. Those hashes are pending drift checks, not a confirmed freeze. Final freezing requires ten real owner/friend rows and four confirmed long rows per language, complete owner-approved JSON labels, reply-label confirmation, and a confirmation record covering the exact CSV/JSON bytes. The freeze command must not be run by Codex without that confirmation. Later edits create a new reviewed version; never overwrite an older frozen set.

[checked] `data/cloud_reference_prompts.md` supplies the same rewrite/planning instruction text for manual collection in ChatGPT. Label every such result **cloud reference, not the product**. UI roles, temperature and model identity are not necessarily controllable, so record them honestly. No cloud reference result has been collected by preparing prompts alone.

## Task 3 measurements

[checked] `python3 scripts/measure_office.py --contestant A --output records/task3/<new-folder>` owns a temporary server, warms it, hashes once before timing, checks generation before/after requests and uses 60-second per-call safety limits. It refuses to take over an existing localhost server. Use a new output folder and close only your own manual server first. B uses the same command with contestant B. These are complete timings; the five-second target is judged afterwards. The production rehearsal still uses its five-second deadline. No console session is started or moved.

[checked] Current final evidence is in records/task3/final_A and final_B. Their rewrite_review.csv files put all raw model rewrites, including rejected/truncated output, beside intended English with blank owner rating cells. Per-language readable pages are linked in OFFICE_LANGUAGE_CHECK.md. Distinct rewrites count once even when several draws are identical.

[checked] C is not installed. Read INSTALL_REQUEST_C.md and the exact pins/sizes before deciding on approved C. The task requires stopping before those installs; the Hugging Face gate must be accepted personally by the owner. Labels remain pending independently of model installation approval.
