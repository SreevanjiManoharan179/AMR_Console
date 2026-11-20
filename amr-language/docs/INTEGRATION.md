# Office language v3 integration contract

[checked] Version 0.8.1 (where automated tests write records, below) on top of 0.8.0 (the assistant's conversation and English-only mode; looking for a person, spoken-style replies and plainer English; chained requests and continued operation; then live-run fixes and movement hooks below) on top of 0.4.3, which implements the owner's v3 design and fixed-speech decision. This is a module and text rehearsal for AMR Console in simulation; the console's Office tasks tab drives it. No console or ROS workspace files are edited by this module. See `TASK4_HYBRID_CHECK.md` for current measured limits and `OFFICE_LANGUAGE_CHECK.md` for task 3. [not checked] Deployment readiness, microphone input and shared-load performance during navigation.

## v0.8.1: records written by tests stay out of records/ (20 Nov 2026)

[checked] 484 tests with the stand-in model (8 of them check the saved measurement records and
are skipped in a copy without `records/`), and the console suite.

- Learning suggestions (and the data snapshots kept when the owner approves one) are written to
  `records/` as before, unless the process has `AMR_LANGUAGE_RECORDS_DIR` set to an absolute
  path; then they go there. A relative path is refused (`ValueError`). `LearningStore(root)` and
  `record_feedback(..., root=...)` with an explicit root still use `<root>/records`.
- Why: until 0.8.0 every test run left 12 pending "owner_row" suggestions in
  `records/learning_suggestions/`, and every console test run one more, which then appeared in the
  owner's review queue (`scripts/review_learning.py`). The module's tests now set the variable to
  a temporary folder for every test; the console sets it for the language worker only in its own
  automated tests (`AMR_CONSOLE_TEST_LANGUAGE=1`).
- Tests that check the saved measurement records (frozen hold-out hashes, saved model rewrites)
  skip when `records/` is absent and fail when `records/` exists but a file is missing.

## v0.8.0: the assistant's conversation, English only (10 Nov 2026)

[checked] 480 tests with the stand-in model, and the console suite. [not checked] With the real
model; `classify` (the model reading a reply) has never run against it.

Three settings, all default false; the console turns them on, the Speech Lab leaves them off.

- `conversational`
  - **Visit.** A `go` step to a listed person (not the requester) greets them (`arrive_visit`),
    waits for what they need (prompt `anything else> `, three calls), and queues their request as
    a follow-up (`follow_up_request`). Outcome `went` with `asked_for_more`. Silence starts the
    search when `search_for_person` is on.
  - **The yes before moving** (`Rehearsal._confirm`). "no" is asked `what_change`; the answer, or
    a new sentence given instead of yes/no ("No, give it to Ravi instead"), is understood as the
    corrected request under the same job id (`correction` event) and read back again. Two
    corrections at most; then `dropped`.
  - **Declined item.** "no" / "not mine" at a delivery: `declined`, the item returns to the
    requester, outcome `failed` with reason `declined`.
  - **Small talk** (`office_pipeline.social`): greeting, thanks, "what can you do?", "who are
    you?", "where is <person>?" return decision `refuse` with reason `conversation` and events
    `greeting`, `welcome`, `capabilities`, `introduce`, `person_place`. No model call, no plan.
  - A request nobody could plan is answered `not_understood`; an unclear reply is asked again
    (`reply_prompt`), then slowly (`say_slowly`). After a session in which nothing was carried
    out, the farewell lines are not spoken.
- `english_only`: a request not written in English (script, or Tamil in English letters) returns
  decision `ask`, event `english_only`, `language_written`; all speech is English; a reply in
  another language is answered with `english_only` and asked again.
- `reply_model`: when a reply matches no known phrase, `backend.classify(text, allowed, ...)`
  asks the model for one of yes / no / taken / done / not_here / later (JSON, constrained). A
  positive reading is discarded when the words contain no, not, don't, wait, stop, cancel, never
  or wrong. Any fault means "unclear". Event `reply_read_by_model`.

Replies: a sentence that opens with "no" and says nothing else known is a no; "No, she's not
here" is `not_here`. Trailing "instead", "please", "thanks" are dropped from a request clause.

## v0.7.0: looking for a person, spoken replies, plainer English (10 Nov 2026)

[checked] 456 tests with the stand-in model, and the console suite. [not checked] With the real
model, with a real microphone, and the wording of the new Tamil, Malayalam and Telugu lines.

- **Search** (setting `search_for_person`, default false; the console turns it on). At a person's
  place the robot speaks, then calls them by name twice more (`call_person`). If nobody answers, or
  someone says the person is away, it asks there where the person is (`ask_people_where`, skipped
  when nobody answered), says `searching`, and visits the two nearest rooms and then the common
  area (`names['common_area']`, else `open_office`), asking `search_ask` at each. "Yes" means the
  person is there; a named place is followed once. Found: the step carries on there (outcome
  `found_by_search`, `at`). Not found: `search_failed`, then the step's own "if not there" rule.
  Events: `search` (stage started / found / not_found). The requester is not asked to confirm a
  new place during a search. Moves have purpose `search`.
- **Replies as people say them** (English): "Yeah, go ahead.", "Taken, thanks!", "She's not here."
  Punctuation and fillers are ignored; the list of accepted phrases is longer. "Okay" where both
  yes and done are expected counts as done.
- **Plainer English.** The read-back is a sentence ("Sure. First I'll take the file to Divya. Then
  I'll ... Shall I go ahead?"), and the fixed lines are polite and short. Other languages keep
  their earlier wording. A message or question for someone who is away now says "I'll let
  <requester> know" instead of "bring it back"; the report says "I could not find <person>".
- **Uncertain readings** (setting `tentative_plans`, default false; the console turns it on). When
  some readings of a request fail but at least two succeed and agree, and none found a rule
  problem, the plan is read back after "I'm not fully sure I understood, so please check this."
  (`result['tentative']`), instead of asking for the request again. The yes is still required.

## v0.6.0: chained requests and continued operation (10 Nov 2026)

[checked] English grammar only; 437 tests with the stand-in model, and the console suite. [not checked]
With the real model, and for Tamil, Malayalam and Telugu requests (these reach the same grammar only
when the translation is clean English).

- **Chains.** "then" and "and" carry the verb on: "Go to reception, then the cafeteria", "Give the
  file to Divya, then the charger to Ravi, then the folder to Meera". Different jobs chain in one
  request ("Give the file to Divya and tell Ravi ..., then come back"). "her / him / them" after a
  stop means the person of that stop. More than six steps is still asked to be split.
- **One trip, up to four items.** All deliveries of a request are loaded in one visit to the
  requester, up to `maximum_items_on_tray` (capability file; 4 when the file does not say). A fifth
  item starts a second trip. A stack for one person ("the file, the charger and the folder to
  Divya") is one stop; more than the limit for one stop is refused (`too_many_items`). Weight is
  not summed: the capability file has no item weights yet. Event: `load` (items, count, limit).
- **Calling a person.** "Call Ravi", "Call Ravi to the conference hall", "Ask Ravi to come here"
  are `tell` steps. The first two carry the fixed message "Please come [to <place>]".
- **If yes / if no** may now hold `go`, `check` and `retrieve` as well as `tell` and `deliver`
  (one level; a branch never branches again).
- **If not there, other steps.** Besides the fixed rules (leave with, bring back, try later, ask
  anyone, skip), any other instruction becomes the step's own steps: `if_not_there: "otherwise"`
  with `otherwise: [steps]`. They run only when the person is away; a carried item then returns to
  the requester. The outcome has `otherwise_used: true`. The model planner's JSON schema is
  unchanged, so only the grammar produces this.
- **Closing question.** Setting `closing_question` (default false; the console turns it on) and
  `follow_up_limit` (default 3). After the last step, when the robot is with a person on the office
  list (not the requester, not a visitor, not a helper who answered for someone, not an empty
  place) and no other request waits, it asks `anything_else`. "no", silence or "go back" sends it
  home. Anything else becomes a new request with that person as requester at that place
  (`follow_up_request` event: job_id, text, requester, place, depth, after), read back and
  confirmed like any request. Prompt for callers: `anything else> `.

Plan data added: `otherwise` (list of steps) on deliver, retrieve, check and tell; policy value
`otherwise`. Callers that walk steps should use `office_schema.walk_steps`.

## v0.5.2: faults from the request battery and the Speech Lab (10 Nov 2026)

[checked] Found with `scripts/office_battery.py` (22 of 47 passed) and the Speech Lab in the console repository:

- A request written in Tamil, Malayalam or Telugu was answered in English when no language was
  chosen, because the requester's profile language won. The words now decide; plain English still
  falls back to the profile (`office_pipeline.py`, `rehearsal.py`).
- The model respelt or translated names (Meera as "Mira", Divya as "the deity", reception as "the
  welcome"). Approved native-script names and places are now written in English letters before
  translation: "மீராவிடம்" becomes "Meera-விடம்" (`office.substitute_aliases`).
- "Ask Arun if the encoder is ready" was refused; "ask <someone> if" now reads as "whether"
  (`office.ask_whether`). A real condition ("if she is not there") is untouched.
- Tamil typed in English letters ("file-a Divya kitta kudunga") was treated as English and never
  translated. It is now detected (`office.romanised_tamil`) and the translation prompt gets a short
  word list. [not checked] with the real model: only the detection and the pipeline are tested.
- "three o'clock" and "3 o'clock" count as the same message when draws are compared.

[checked] Tests: `tests/test_live_faults.py` (20 cases); suite 420 passed.

## v0.5.1: fixes from the console's live runs (6 Nov 2026)

[checked] Found in the console's office logs of 5 Nov (`data/logs/office/20261105_23*`):

- A closing "and come back" passed the grammar but failed the slot check, because "me" never
  appears in the sentence (`office.py`, `_slot_sources`). The request now reads back three steps.
- "Go to the server room" was treated as an unknown *person* and started the new-person questions.
  A go-step whose words end in a place word (room, office, area, hall, lab, floor, wing, desk) now
  asks `unknown_place` and names up to three mapped places (`office_pipeline.py`,
  `office_speech.py`; Tamil, Malayalam and Telugu wording still needs a native check).
- "Starting step N" was said in the next person's language (Arun's Telugu) to the requester at
  the dock. It is now said in the requester's language, and a job keeps the language the request
  was written in (a Tamil request hears its steps and report in Tamil). At a stop the robot still
  speaks that person's language (`rehearsal.py`).

- Later on 6 Nov: a refusal for an unsupported "if" (for example "if she is not there, go to Priya
  and ask her where she is") now adds the rules the robot can follow; a "don't" order and a request
  the planner could not turn into steps get their own one-line hint (`office_speech.py`
  `REFUSAL_HINTS`, `office_pipeline.py`).

[checked] Tests: `test_office_map.py` (unknown place, options), `test_movement_hooks.py`
(announcement language, Tamil request); each fails on 0.5.0 and passes on 0.5.1.

## v0.5.0: movement hooks for the console (5 Nov 2026)

[checked] 0.5.0 adds the places where a caller moves the robot; the language behaviour, prompts,
grammar, speech templates and plans of 0.4.3 are unchanged except for one new failure reason.
AMR Console (Office tasks tab, `tools/amr_console/language_worker.py`) subclasses `Rehearsal` and
overrides only `move_to`, `emit`, `speak`, `reply` and the blocking `input_fn`.

- `Rehearsal.move_to(place, purpose, target=None)` is the single movement call. The text
  rehearsal prints `[robot moves to …]` and returns True; a console drives there and returns
  whether it arrived. `purpose` is `pickup`, `step`, `hint`, `return` or `home`. `place` is always
  an approved place key from the names file (a room, a desk id or an object), never coordinates.
- `arrive(target, purpose)` resolves a person to their current place (`person_places` from a hint
  in this job, a temporary person's place, the requester's `requester_place`, otherwise their
  usual desk), skips the move when already there, and records `position`.
- Delivery collects from the requester first (`pickup`), every deliver/check/collect/tell/go step
  arrives before speaking, a position hint moves to the named place before asking again, and
  `go_home()` runs after the queue (and after a deferral) when the map has `home`.
- A move that fails (refused, blocked, cancelled) never continues the step: the robot says the new
  fixed reason **unreachable** (English, Tamil, Malayalam, Telugu; native check pending), the
  outcome is `{'status': 'failed', 'reason': 'unreachable', …}`, and an item it carries is
  returned to the requester (`returned_to_requester`).
- `drives_robot` (False here) labels the banner and metadata; `run(text=None)` lets a caller that
  queued work itself start the loop.
- Grammar: ", and <action>" and a closing "and come back" now start their own step, and a bare
  "come back" means back to the requester (as "come back to me" already did). Before this, "…,
  then ask Arun whether the encoder is ready, and come back" carried "…, and come back" inside
  Arun's question. Item lists ("the file, and the charger") are still left to the model planner.

[checked] Changed files that the task-4b measurement scripts pin (`office_speech.py`,
`office_grammar.py`) mean those scripts refuse new campaigns against 0.5.0, by design; the earlier
measured numbers, including the frozen held-out grammar coverage, describe 0.4.3.

[checked] `tests/test_movement_hooks.py` (9 tests) covers the move order for delivery and
collection, unreachable stops, an absent recipient, STOP during a move and no move when already
there; `tests/test_long_requests.py` (7 tests) the grammar change; the full suite passes. [not checked] Live Gazebo runs through the console belong to
the console's records.

## v0.4.3 integration candidate: current startup and job contract

[checked] Use **office_B explicitly**; the low-level settings loader retains A for historical callers. The supported preview and rehearsal CLIs now default to office_B. Start the installed runner in its own terminal and warm B outside request timing:

```bash
cd ~/AMR_Console/amr-language
python3 scripts/local_runner.py serve
# In another terminal, after the runner is ready:
python3 scripts/warm_office_model.py --profile office_B
python3 scripts/rehearse_job.py --profile office_B --names /absolute/path/console_context.yaml --capability /absolute/path/owner_capability.yaml --requester Bharathwaj --language en
```

[checked] The console adapter should call the separate language worker asynchronously:

```python
from amr_language.office_backend import load_office_profile, prepare_office
from amr_language import understand
settings = load_office_profile("office_B")
settings["capability_profile"] = owner_confirmed_capability_dict
prepare_office(settings)  # outside request timing
proposal = understand(typed_text, caller_map_and_names_dict, requester, language, settings)
```

[checked] B profile: hybrid grammar, adaptive draws, sample .7, office word list off, follow_person_hint ask, token caps 384/768, product budget 5 s. `act` remains a **proposal requiring readback and yes**, never a dispatch. Reject `review_only` and mock results at an adapter boundary. Keep STOP, navigation callbacks, UI timeouts and session/request IDs outside the language worker; discard results from superseded sessions. A socket deadline does not cancel an already-running model generation. No console adapter, microphone, speech engine, physical navigation or arm is installed by this release.

[checked] The caller supplies current map membership, reachability, usual/requester places and physical route requirements (`requires_arm`, `requires_door_operation`, `requires_lift`, `requires_stairs`). `data/robot_capability.example.yaml` has **no invented numeric limits**: fill every `owner_to_set`, review item classes, then set approval_status confirmed. Numeric limits in tests/fixtures are SCRIPTED ONLY. Unconfigured capabilities refuse. Reachable areas always come from caller map facts. Item metadata supports weight_class light/medium/heavy, fits_tray, fragile, personal and restricted. Heavy/nonfit/forbidden payloads, unavailable routes and physical operations refuse with fixed reasons and alternatives. An unmapped place remains a clarification plus an outbox entry. Carrier jobs include delivery, collection, mediator checks, message delivery and repositioning/home.

[checked] Long grammar jobs keep check branches together, remove a redundant go before an action at that destination, partition into at most the configured 1–6 total actions per part and expose every queued part. Agreement covers all parts and disclosed omissions, not just the first prefix. Unsupported independent parts appear explicitly in `omissions` on screen with a fixed spoken notice. Unsupported structural requests that cannot be safely partitioned ask to split or refuse; model fallback stays subject to schema/source checks. Every part gets its own fixed readback ending “Is that OK?” and a fresh yes. Revalidate map, capability, requester and STOP before each part. Pickup rejection cancels that item and dependent actions; independent remaining actions continue. Unknown/fragile items require a loader's yes about weight/fit, and battery packs also require a bag. Handover still requires taken/placed/answered. Text rehearsals illustrate wait boundaries from maximum_wait_at_stop_s; **the console must implement real stop timers**.

[checked] Arbitrary questions, messages, item hints and exact human answers are screen data. Mediator answers never enter the model; show the exact original answer and speak fixed verification, allow one correction and verification retry, and report only verified text. No unverified yes/no branch runs. Transient/new names and optional helper names remain screen data, using a fixed person-on-screen label in speech until the normal roster is owner-approved. A mapped position answer is offered as a new step to the requester, with a new yes, never followed automatically. All new native templates await native-speaker review.

[checked] Example lab vocabulary lives in data/lab_items.example.yaml; every entry is owner_to_review. English technical terms such as encoder must survive mixed-language rewrites; they use fixed English vocabulary in native readbacks. `item_hint` remains text, never a route/map addition. “I think I left [item] at X” carries item_location guess and uses “may be”; failure reports not_found_at X.

[checked] For a consistent unknown-name plan, the rehearsal can accept a requester-selected existing identity (then propose an alias), or confirmed existence plus a reachable mapped place for a new person. New identities live in temporary_people with requester/job ID only, expire at the end of that original job including its queued parts, and never enter the names file. At arrival ask whether that person is present. Collection helpers are allowed by default and must verify item identity and payload safety. Delivery/check helpers require explicit ask_anyone readback permission; personal/restricted items cannot go to a helper or alternative/place recipient. `/helper_name Name` optionally records provenance on screen; no arbitrary helper name enters speech. Reports distinguish helpers and show their supplied names. Failure to find help applies the original skip/defer/return policy.

### Optional C boundary (CPU only for integration)

```python
from amr_language.office_c_client import CWorker
from amr_language.office_backend import make_backend, CrossSystemOfficeBackend
# Parent never imports C model libraries. Close only workers owned by this caller.
with CWorker("cpu", "records/rehearsals/C_cpu") as c:
    paired = dict(settings, draw_policy="cross_system")
    paired["backend"] = CrossSystemOfficeBackend(make_backend(settings), c)
    proposal = understand(typed_text, caller_map_and_names_dict, requester, language, paired)
```

[checked] CWorker always launches `.runtime/indictrans2-C/venv/bin/python -I scripts/translation_worker_C.py --device cpu` in this integration path, strips ROS/library Python paths and uses pinned offline files. All requests and request responses (including errors/close) carry request_id. Startup ready is a separate unsolicited notification. After timeout, late output is drained until the matching ID; it cannot answer the next request. A persistent reader avoids TextIO/select buffering races. Do not switch to a ROS-sourced interpreter or import torch/transformers into the console. GPU results below are historical measurements, not the integration configuration. Product timeout stays 5 s; a slow C result fails closed. C remains optional and uncalibrated; prior C measurements were preserved, not rerun or tuned for this task.

### Outboxes, owner review and privacy

[checked] After a real console job completes or is declined, call `amr_language.office_learning.record_feedback(typed_text, requester_id, language, readback, "yes"/"no"/"not_asked"/"unclear", outcome)`. This appends a pending suggestion only. The console must supply the real decision/outcome; understand() alone cannot know them. Supplied preview/rehearsal CLIs already record their real typed-use feedback; scripted runs do not enter the live feedback store.

[checked] `records/map_requests/*.jsonl` is an append-only map/person suggestion outbox; the console owner reviews it, and the language module never edits maps/names. `records/learning_suggestions/YYYY-MM-DD.jsonl` records typed requests/readbacks/decisions/outcomes and candidate aliases, confirmed word corrections, owner examples or person places. No audio. Source SCRIPTED logs are kept separate and never become real OWNER rows automatically. `/correction {"language":"en","word":"adapter","meaning":"small cable adapter"}` asks the requester to confirm that exact correction and appends a pending suggestion only.

```bash
python3 scripts/review_learning.py --names /absolute/path/console_context.yaml --capability /absolute/path/owner_capability.yaml
python3 scripts/delete_person_records.py --requester REQUESTER_ID
```

[checked] Review shows each suggestion and evidence, supports reject/edit, then runs baseline/candidate B checks on all 70 starters, both 36-row held-out sets and every explicitly confirmed OWNER row before final `approve`. Generation liveness surrounds live checks; measurement mode has 60 s safety per call, no five-second product aborts. Unmeasured cases are counted/excluded and block acceptance; a regression is shown and refused. The conservative oracle preserves previous accepted plans and refuses refusal-to-act changes; it is **not a claim of native meaning accuracy**, especially while starter labels are pending. Word-meaning review exercises the opt-in word list; office_B remains word-list-off. Review-only overrides cannot run as rehearsal jobs. Data/candidate hashes must still match at acceptance.

[checked] Approved aliases are a versioned, SHA-256 checked overlay in learned_aliases.json; meanings and owner_check.csv receive bumped versions/hashes and private data snapshots. No automatic grammar, weights, maps, names or Git edits. Person-place approvals remain console-review suggestions. Pending suggestions expire after learning_retention_days (default 30) when the store is maintained on use; schedule maintenance if idle expiration is required. Reviewed evidence remains until requested erasure. Private usage/history/rehearsal records are Git-ignored; do not commit real feedback/data updates accidentally. Erasure removes matching local learning/rehearsal/map-suggestion data and approved contributions, not past Git history, exported evidence or chat copies. Owner examples remain uncollected in this release; all learning acceptance/rejection demonstrations use temporary scripted data.

[not checked] Seamless real console/Gazebo shared-load operation, physical payload limits and native command accuracy. These remain integration/owner checks. See TASK4C_BEHAVIOR.md for the final test count and scripted evidence. The sections below preserve architecture and earlier measurement context; the current startup, carrier and mediator rules above supersede earlier behavior.

## Public API

```python
from amr_language import understand, say, hear_reply
result = understand(text, names, requester, language=None, settings=None)
line = say(event, language, **slots)
reply = hear_reply(text, expect, language=None)
```

[checked] Python >=3.12, standard library for inference; existing PyYAML loads the CLI roster; existing pytest runs development checks. No package install is needed with this folder on a separate worker's search path. No import-time network calls or function-triggered downloads. Model requests use literal localhost 127.0.0.1 only. The private runner is Ollama 0.40.12 with CUDA, context 4096, one resident model/parallel request and cloud disabled. `python3 scripts/local_runner.py serve` starts it; no system service was installed.

[checked] `text` accepts 1–2000 characters of typed input, including line breaks, normalized internally with NFC. Longer input asks to split. Invalid controls/context refuse. `names` is a parsed dictionary with `approval_status: confirmed`, rooms, and optional people/seats/objects, validated reference links and aliases in the reference format. Proposed additions are `people[].language` and entry-level `aliases_by_language: {ta: [...], ml: [...], te: [...]}`. These are demonstrated only in `data/office_names_example.yaml`; its fictional profiles and translations remain draft.

[checked] `requester` is an approved person ID, or trusted caller identity as a string or `{id, name?, language?}`. A known room/object is not a requester. An unlisted caller identity is context, not a model-created map destination: the console must retain and resolve that person's location before scheduling a return. Language hints/profile values are en/ta/ml/te; without a hint the requester's profile takes precedence over script detection. Romanised language detection is not established; pass the known language.

[checked] Return keys are `requester, language, english, steps, decision, ask, refuse, readback, model, latency_ms, planner, draws`. `planner` is grammar, model (if any fallback was used), or null before planning. `draws` is the actual attempted rewrite count. Map asks also carry validated target, reason/options and logging status; `steps` stays empty for ask/refuse. Inactive ask/refuse are null. Model identity contains the local name and actual main-weight SHA-256 (mock has null). English is model data for escaped screen display and owner review. Never pass it into speech or interpret it as executable instructions.

[checked] Actions/fields: deliver(item,to), retrieve(item,from,to), check(at,question,on_answer), tell(to,message), go(to). All targets become approved IDs or caller ID. Every step has n and if_not_there; check also has on_answer null or yes/no arrays. Retrieve's destination defaults to requester. At most six actions including both branches; branch actions are only deliver/tell, one level. Numbering is unique depth-first spoken order. The validator/schema are `amr_language/office_schema.py` and `plan_v3.schema.json`.

[checked] Absence policy is bring_back (default), leave_with:approved_person, try_later:minutes (integer 1–1440), ask_anyone or skip. Retrieve cannot use leave_with before collection: rephrase rather than inventing an item source. Conditions other than absence and explicit yes/no check branches refuse. The module does not add idle return; console owns that. Explicit come back to requester is a go step to requester.

[checked] Processing defaults to a greedy English rewrite followed by the pure-Python office grammar. Only an unparsed rewrite falls back to the model planner. Unknown names remain literal until grounding. A valid, grounded greedy grammar plan gets one sampled rewrite at temperature .7. If both grounded plans agree, the result can be act; otherwise the remaining two draws run and all distinct plans must agree. A greedy failure/model fallback uses four draws. Unicode NFC/whitespace-identical rewrites reuse planning and grounding, so duplicates are not independent votes. Invalid/missing samples cannot authorize act. Source spans and omitted known names are audited for every distinct rewrite. **Two agreeing draws are a weaker check than four. Agreement is uncalibrated, and readback with explicit yes remains mandatory.** The grammar does not validate translation meaning.

[checked] Grammar preserves clock text literally inside messages/questions, including “at 3”, “at 3 pm” and Tamil time text. It never creates a duration from them. The English rewrite remains a separate possible source of time/meaning errors. The fixed office meanings file is optional, hash checked, and derived from starter errors; gains on those same starters are optimistic. It is disabled by default pending the comparison and owner review.

## Settings and failures

| Setting | Default / rule |
| --- | --- |
| model | office_B profile selects qwen3.5:4b-q4_K_M; compatibility settings without an explicit profile retain A |
| timeout_s | 5.0, whole understand call budget; diagnostic .01–120 |
| reply_timeout_s | 1.0 compatibility setting; mediator answers bypass inference |
| num_ctx | 4096; 1024–4096 |
| rewrite_tokens / plan_tokens | 384 / 768 caps; 64–1024 / 128–1536; rewrite also bounded by input length |
| sample_temperature | 0.7 default; validated 0–1; greedy stays 0.0; task 4b compares 0.3 without assuming it is safer |
| seed | 1729; rewrite index increments, plan offset 100, answer offset 200 |
| port | 11434; literal localhost, no arbitrary host/proxy |
| model_file / model_manifest_file | Verified files under this module, pinned in contestant profile |
| runner_version | 0.40.12, verified with server |
| planner_mode / draw_policy | hybrid / adaptive; model / four retained for baseline comparison; cross_system requires the explicit isolated C+B backend |
| office_meanings | false; fixed data/office_meanings.json with SHA-256 guard |
| follow_person_hint | ask (default), or never; only rehearsal interprets destination replies |
| input_source | UNREVIEWED_TYPED default; STARTER_MEASUREMENT, SCRIPTED or OWNER_TYPED supplied by trusted caller |
| trace | Optional caller-owned list of intermediate model data, for review only |
| backend | Explicit MockOfficeBackend for tests only, or CrossSystemOfficeBackend with verified local B and an already-running isolated CWorker; never dispatch mock plans |

[checked] Unknown settings refuse. File reads resolve under this module. Main weights, original registry-manifest relationships and running identity are verified; hashes are cached against file metadata. The 0.40.12 wrapper manifest and registry digest are distinct and both recorded. Mutating a model/profile requires reinspection and a new check.

[checked] Off/unreachable/mismatched runner produces fixed model_unavailable; slow calls produce fixed model_timeout; malformed/truncated plans fail validation. A timeout is a refusal, not a completed plan. The five-second budget is checked between calls and used for sockets. Synchronous hashing/identity checks and scheduling can exceed it; observed terminal latency exceeds five seconds. This is not a hard real-time cancellation API. Cold load is separate and slow; warm-up records are under `records/office_checks/`. The owner retired the previous two-second target in favour of p95 <=5 seconds for this task.

[checked] B became unresponsive to even a simple prompt after repeated deadlines; an explicit unload/reload restored a hello response and a full scripted delivery. That delivery still took 14.1 seconds. The precise stall cause is not established. A healthy localhost version/identity response does not prove the model can answer. [likely] Treat timeout storms as an unhealthy worker condition; use bounded recovery outside any active navigation path and test it before integration. Do not silently unload a shared model on every user request.

[likely] Use a separate cancellable worker and show Thinking while awaiting it. Keep STOP, navigation callbacks and session state independent. Bound the queue, discard stale results after STOP/session changes, and serialize requests across workers as well as within one Python process. Caller cancellation does not imply an already-running server request stopped. Enforce an external deadline for responsive UI behavior.

[likely] On timeout/unavailable, offer the console's existing rule-based grammar for requests that grammar supports, with its existing map/name checks and explicit confirmation. Do not map a failed office job to partial navigation. Never dispatch a prefix of ask/refuse. Before dispatching act, reject mock identity, validate the entire plan/names/context, show payloads separately, obtain explicit yes and recheck STOP/session. This proposes the adapter; no fallback was added to console code here.

## Speech and replies

[checked] `say` implements the required 19 events in four languages plus fixed split/rephrase/timeout/reply/waiting events. Identity slots need names and requester; step/readback lines also take validated steps. Reasons are fixed enums, numeric fields bounded, unknown items use a fixed generic item phrase. Arbitrary question/message/answer/english/text slots are rejected. See `office_speech.py` for event-specific slots and `tests/test_office_v3.py` for filled examples. Every non-English template is marked needs native-speaker check.

[checked] Owner decision: **fixed speech; show content on screen for now**. Check/tell announcements say fixed question/message phrases; actual content and returned answers are UI data. Report speech is a fixed summary, detailed outcomes on screen. Escaping is the UI renderer's responsibility. Fully spoken arbitrary messages/questions are not implemented.

[checked] `hear_reply` expects allowed reply kinds. Location replies preserve the literal answer for exact alias matching; they do not guess a new destination or call the model. It uses exact normalized fixed phrases and English loanwords; STOP across all four languages is checked first even when the hint differs. Ambiguous short words return unclear if expectation cannot resolve them. Known yes/no answers are fixed English. Free answers preserve answer_original exactly; they never call the model. answer_english is a compatibility field containing the original ASCII text or null for native text. The public signature has no settings parameter and uses A's default profile; a future per-call setting is proposed, not silently added. Rehearsal's private helper retains its chosen contestant. Word lists are draft fixtures, not measured real speech recognition.

## Artifacts and reproduction

[checked] `data/office_model_A.json` and B pin main blob/manifest paths and settings. Exact SHA-256 and byte size of **every** config/projector/weight/template/licence/parameter/registry-manifest blob, plus wrapper hash, are in `records/office_checks/model_A_inventory.json` and `model_B_inventory.json`. Those inventories are the full artifact manifest; large files stay ignored under `.runtime/`.

| Artifact | SHA-256 |
| --- | --- |
| Ollama 0.40.12 executable | e38ef99ac4a7af0f7a7bec618ed38c77617fad8c412c025d69df49b4d7001786 |
| A main weights | 20cb277f0967ace47b0b5d5658e5e494a88937f7378b74b5a266496400938f4c |
| A registry manifest | 1e2d21a4f03ab9e4a9c1976a4b7e88f21de61d65f2c5485d8fde8942be73f1e7 |
| B main weights | a9d0a3e1d8c91732a3812becd31f8d4a8ff57414903f226306069f0437e2ac50 |
| B registry manifest | d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04 |

[checked] B installation used the exact approved commands in `INSTALL_REQUEST_B.md`. No speech/translation model was installed. IndicTrans2 is now installed in its isolated environment; see `C_INSTALLATION.md` and its historical pinned request in `INSTALL_REQUEST_C.md`. R is a manual cloud reference, not the product; exported prompts and an empty results sheet are supplied, with no cloud measurements claimed.

[checked] Run `scripts/try_command.py` for one request, `scripts/rehearse_job.py` for a text session, `scripts/check_office_language.py` for a new STARTER check folder, and `scripts/verify.py` for tests/reference/console drift checks. Rehearsal has /queue and /person, explicit confirmation, absence policies, branches, report and illustrative idle home. Real timers/motion are absent. Deferrals wait for instructions; STOP does not announce home. See `REHEARSAL_RULES.md`.

[not checked] Meaning/long-request owner ratings, native translations, microphones/ASR, real calibration, and <=4 GB while Gazebo navigates. Owner data/labels remain pending, including SHA freezing; current hashes are drift checks only. No training is needed to collect commands or review outputs. Observed failures, intervals and exact paths are in `OFFICE_LANGUAGE_CHECK.md`. Historical v1 results remain unchanged; its old API contract is `INTEGRATION_LEGACY.md`.

## Task 3 worker and measurement changes

[checked] Call `from amr_language.office_backend import prepare_office; startup = prepare_office(settings)` once at worker startup, outside user request timing. Startup returns actual hash/manifest duration and model hash. Prepared artifacts are cached with file-metadata guards; a change requires worker restart. The three primary function signatures are unchanged. Supplied CLIs do this automatically.

[checked] Settings add mode (product default, or measurement), pipeline_parallel (1 default, experimental 4) and deduplicate_plans (true default). Product still uses timeout_s=5; measurement uses a 60-second safety limit per model call and evaluates five-second attainment afterwards. Trace includes per-draw rewrite/plan/grounding work time and pipeline wall times, distinct pairs, valid pairs and reuse counts. Task 4 adds the `planner` and `draws` fields described above.

[checked] The measurement harness owns its server, probes generation before/after each request, logs restarts and retries once; failed infrastructure is not measured. It never terminates an unowned service. This is a diagnostic lifecycle, not a console recovery implementation. Product health/recovery must stay outside navigation callbacks and keep STOP independent. See TASK3_MEASUREMENT.md for exact denominators and exclusions.

[checked] Requested OLLAMA_NUM_PARALLEL=4 did not produce four effective runner slots in the installed stack. B’s /api/ps reported size_vram equal to size at the existing 4-GiB reservation. Defaults remain sequential; no driver or runtime install occurred. Valid six-step outputs used 327/326 generated tokens (A/B), so plan_tokens remains 768.

[checked] The October report’s stalled periods are not model-quality failures. Current evidence is under records/task3 and OFFICE_LANGUAGE_CHECK.md; archived October source/results remain intact. C is installed: all pins verified, custom sources reviewed before loading, and offline CPU/GPU greeting smoke tests passed. The full fixed-planner C office comparison is now recorded in TASK4_C_CHECK.md. Use its isolated Python with -I to exclude inherited ROS PYTHONPATH. See C_INSTALLATION.md; no console/ROS package was changed.

## Caller-owned map facts (task 4 proposal)

[checked] The console owns and approves the names file and map. Pass fresh facts through `names`; this module never writes either. These proposed fields are exercised only in the separate `data/task4_names_example.yaml` **scripted mock fixture**, not a claim about live routes:

```yaml
map_id: owner_approved_map_id
requester_place: home    # captured origin for this job, optional only if caller resolves it
rooms:
  - name: reception
    # existing approved aliases/coordinates retained
    on_map: true
    reachable: false
    unreachable_reason: blocked
    nearest_mapped_place: cafeteria  # optional approved ID
people:
  - name: Divya
    # existing aliases/profile retained
    usual_place: team_office_r1c1
    last_seen_place: conference_hall  # optional observation, never automatic relocation
    last_seen_at: '2026-11-02T10:00:00+05:30'
```

[checked] Places in rooms/seats/objects accept `on_map` and `reachable` booleans. Seats/objects can inherit room facts; existing approved coordinates/parent can establish mapping if on_map is absent, but missing reachability never establishes a reachable route. Reason codes are blocked, no_route, closed, not_verified and unknown (spaces accepted as underscores). Supply false with a reason; the fixed unknown/not_verified wording covers missing facts. `usual_place` and `last_seen_place` reference approved place IDs. Last-seen observations need a string `last_seen_at`; they are accepted as context, not freshness-verified or used to move a person. Every destination, source, check location, branch target and leave_with alternative is checked before act. Caller-origin targets use requester_place when supplied; without it the console must resolve and validate the captured requester origin before dispatch. Never infer caller location from language-model text.

[checked] Missing names ask with up to three closest approved names, without choosing one. Known off-map places ask with a mapped/reachable alternative and log an owner request. Alternatives use caller-supplied nearest_mapped_place or Euclidean distance between supplied map coordinates; without coordinates no nearest claim is made. This is not navigation route distance. Unreachable places offer later/another place/cancel, with a fixed reason. People missing usual_place ask where; an old seat link is not silently promoted to usual_place. The text rehearsal can select a mapped replacement or one-job person location, then requires a revised readback and yes. Generic unknown-name asks end the job awaiting a new corrected request. Console UI must implement that clarification loop.

[checked] At a person’s destination, not_here first asks nearby people using hear_reply(expect='location'). Only exact approved location aliases can produce a hint; no fuzzy relocation. With follow_person_hint=ask, a mapped/reachable hint is offered to the requester as a new step and requires a fresh yes. No answer/no/never/invalid hint follows the original absence policy. Unmapped hints append a map request and follow the policy. The rehearsal keeps any chosen place in one-job memory; it never changes the roster. Three successful interactions away from usual_place at the same approved place produce a suggested person-location update, not an applied change.

[checked] Append-only suggestions live in records/map_requests/YYYY-MM-DD.jsonl. Each contains time (Asia/Kolkata), who_asked, exact_words, language, missing_or_wrong, nearest_mapped_place, kind, map_id, source and applied:false; observations add person/place/usual_place. Known off-map requests are logged before the ask; a write failure returns a fixed failure notice and cannot authorize act. Writes reject symlinks/hardlinks and use append + file locking. SCRIPTED records are fixtures and must not appear as real owner requests. The console’s later approval UI is outside this module. The six new map spoken events exist in en/ta/ml/te; **all non-English templates need native-speaker check**. Arbitrary replies/messages still stay on screen.

[not checked] Shared GPU operation with Gazebo, live routing facts, microphone/ASR, native meanings and real command accuracy. Do not dispatch mock identities or infer reachability from these scripted fixtures. Keep STOP/navigation independent, revalidate facts and session after yes, and call this worker asynchronously. See TASK4_HYBRID_CHECK.md for the 20 English/Tamil scripted scenario groups and measurement evidence.

## Task 4b clarification and grammar fixes

[checked] Structural contractions in absence rules normalize he/she/they and here/there; message/question payloads stay literal. Retrieve productions include from, relative possession “the X that is with Y”, and return here/to me/back to me. Noun slots reject unexplained clause/relation words so malformed text falls back to the model; unknown simple names still await grounding. The new authored v2 holdout is frozen independently of v1. Coverage limits are reported in TASK4B_FIXES.md.

[checked] Buy from an exact approved person produces confirm_collect with empty steps and requires_new_request:true, never a silent mapping. A caller can show the suggested collect interpretation and obtain a fresh explicit collect request and full readback/yes. The rehearsal does not dispatch such asks or treat a bare yes as a new job. Ordinary shopping is still out of scope.

[checked] Grounded-plan disagreements ask about the first differing person/place/item/absence/message/question/action slot using fixed templates. Message/question alternatives are UI data only; speech directs the requester to choose on screen. Arbitrary item text uses fixed generic speech. Rejected name-dropping rewrites may be interpreted by the grammar solely to explain the differing slot, marked clarification_only_plan; they are never a valid draw or permission to act. A generic repeat request remains for incomplete/uncomparable interpretations and contains no technical plan-disagreement language.

[checked] Unknown-name suggestions combine spelling and coarse phonetic similarity across approved names and aliases in all four languages. Doctor/Daqu/dok offer home/dock among the closest three. The coarse Unicode-letter aid is not calibrated pronunciation recognition; suggestions never ground automatically. All new non-English questions need native-speaker check. See TASK4B_FIXES.md for temperature results; two agreeing draws remain weaker than four and mandatory yes stays in place.

## C process boundary and explicit paired mode

[checked] The default B path and sample_temperature:0.7 remain unchanged. Opt-in cross_system requires CrossSystemOfficeBackend and an already-running CWorker; ordinary backends cannot claim cross-system agreement. The C worker uses only `.runtime/indictrans2-C/venv/bin/python -I`, rejects an unisolated/wrong-prefix interpreter before model import, excludes ROS paths and runs offline against verified pinned/reviewed files. The parent process never imports C's model libraries. CWorker is serialized by the existing understand lock and must not be called concurrently by other code. Create/warm workers outside request timing; close only workers owned by the caller. No system service is installed.

[checked] CrossSystemOfficeBackend delegates model identity verification and model planning to verified local B. Draw 0 is one deterministic C translation; draw 1 is one live B greedy rewrite with its original greedy seed and temperature zero. No random extra draws are made, even after disagreement. Grammar, source guards, grounding, map checks, explicit collection questions and first-differing-slot questions are the same fixed task 4b components. Both valid supported plans must agree before act, still requiring readback/yes. English C input is passthrough, not a translation-model draw. Trace includes rewrite_source and rewrite_temperature (null for deterministic C, zero for B); returned model identifies B as planner, while the caller must retain CWorker.ready's verified C revision/file identities too.

[checked] The local translator caps source and output at 256 tokens, never silently truncates a request and never plans a partial translation. Capacity/truncation/empty output produces invalid rewrite/clarification rather than act. Process/transport/safety failures fail closed; measurement excludes failed infrastructure after one recovery/retry. Worker liveness generation and startup remain separate from true request wall timing. No inference download, credential cache or pip-cache deletion is performed.

[checked] TASK4_C_CHECK.md records CPU float32/GPU float16 translation, B-loaded/unloaded memory, live paired latency/quality and raw C/B/A rewrites. The C GPU paired warm peak was 4394 MiB; CPU C plus GPU B stayed at 3795 MiB in this standalone run. B remained fully GPU-allocated in both paired profiles. Warm-state and startup-inclusive peaks are separated because a B shutdown tail briefly contaminated the beginning of the GPU-unloaded startup samples. Full paired p95 still exceeds five seconds, and native/owner meaning accuracy is uncalibrated. Two different systems can agree on a wrong interpretation, as the authored make-tea case demonstrates. This is a review path, not verified console/Gazebo integration; preserve mandatory yes, fixed speech and exact plan content on screen.
