# Office tasks

People in the office ask the robot for small jobs, typed or spoken, in English, Tamil, Malayalam,
Telugu or a mix of them. The robot does four kinds of work and nothing else:

| Job | Example | What the robot does |
|---|---|---|
| Deliver | "Give this file to Divya" | collects it from you, takes it to Divya, waits for "taken" |
| Collect | "Bring my charger from the demo area" | goes there, asks someone to place it on the tray, brings it to you |
| Check / ask | "Ask Arun if the encoder is ready" | goes to Arun, shows the question, reports the exact answer back |
| Pass on a message, go | "Tell Meera the meeting moved", "Go to reception" | goes there and passes it on, or just goes |

When it has nothing to do it returns home.

## How a request runs

1. **Understand.** The language module (`../amr-language`, model B on this computer) turns the
   request into short steps. A long request is split; more than six steps is refused politely.
2. **Read back and wait for yes.** "I will collect the file from you and take it to Divya. If
   she is not there, I will bring it back. Is that OK?" Nothing moves before a yes.
3. **At each stop** it asks the person there: "taken" / "not here" / their answer. If nobody
   answers it follows the request's rule: bring the item back (default), leave it with someone
   named, try later, ask anyone nearby, or skip.
4. **Unknown people and places.** A name or place that is not on the map is never guessed: the
   robot asks where they sit, whether they are in today, or whether someone nearby can help
   ("Can anyone load the charger?"). A new answer becomes a new step that needs a new yes.
5. **Too heavy, unsafe or not its job.** The tray and weight limits in
   `config/robot_capability_simulation.yaml` decide: documents, chargers, encoders, boards are fine;
   laptops and battery packs are confirmed at pickup; hot tools, open drinks, cash and sharp items
   are refused with the reason.

## Chained requests and continued operation

Needs language module 0.6.0 or newer. English requests; other languages work when their
translation comes out as plain English.

| You say | The robot does |
|---|---|
| Give the file to Divya, then the charger to Ravi, then the folder to Meera | loads all three in one visit to you (up to four items), then three stops |
| Give the file to Divya and tell Ravi the meeting moved, then come back | three steps in order |
| Give the file, the charger and the folder to Divya | one stop with a stack of three |
| Call Ravi to the conference hall / Ask Ravi to come here | goes to Ravi with that message |
| Ask Arun whether the encoder is ready. If yes, go to Divya and tell her it is ready. If no, call Ravi. | one question; the answer chooses the next step |
| Give the file to Divya. If she is not there, go to Priya and ask her where she is now. | the second part runs only if Divya is away; the file then comes back to you |

At most six steps per request and four items on the tray; a fifth item makes a second trip.
Item weights are not added up yet.

**Closing question.** After the last step, if the robot is with a person on the office list, it
asks whether they need anything else. "No", silence or "go back" sends it to the dock. Anything
else is that person's own new request: read back, and it starts on their yes. The task list shows
it as a new task under their name. Not asked: at the requester, to a visitor, to a helper who
answered for someone, at an empty place, or while other requests wait. Switch and limit:
`continued_operation` in `config/office_tasks.yaml`.

## Talking with the robot

Needs language module 0.7.0 or newer.

The full list of situations and what the robot does in each is in [SCENARIOS.md](SCENARIOS.md).
This console works in English; a request or reply in another language is answered with an apology
and a request to say it again in English. The Speech Lab keeps all four languages.

**A conversation.** "Go to Divya" greets her and waits for what she needs. "No" at the read-back
is asked what to change; a new sentence instead of yes or no is taken as the corrected request.
Someone who does not want an item says so and it goes back. "Hello", "thank you", "what can you
do?" and "where is Ravi?" get an answer and move nothing.

**Spoken replies.** Tick "Listen for spoken replies" above the conversation. When the robot asks
something, the microphone opens once the robot has finished speaking, stops about a second after
the person stops talking, and sends what it heard. If the words are not an answer, the robot asks
again. Nothing heard for 7 seconds: use the buttons, Speak, or type. Tick "Read the robot's lines
aloud" as well for a conversation without the keyboard. A request itself is still checked by the
person before it is sent.

**When someone is not there.** The robot speaks, then calls the person by name twice more. With no
answer, or when someone says the person is away, it asks where they are, looks in the two nearest
rooms and then the open office, and asks there. If it finds them, the step carries on there. If
not, it follows the request's own "if not there" rule (by default it brings the item back) and
returns to the dock.

**Unclear requests.** If the robot is not sure it understood, it reads its plan back with "please
check this" instead of asking for the whole request again. Nothing moves before a yes.

Switches: `assistance` in `config/office_tasks.yaml`.

## Two ways to run it

| Mode on the page | Needs | What moves |
|---|---|---|
| **Without the robot (conversation only)** | the language service | nothing; the page shows "(no robot) moved to …" |
| **With the robot (Gazebo)** | a navigation session on the office map with ground truth (Navigate tab), and every owner approval below | the simulated TurtleBot3, one approved stop at a time |

In the simulation there are no people. The person at the console answers for them: the page shows
whom the robot is asking ("The robot is asking the person at Divya's desk") with answer buttons.

## Owner approvals (robot mode only)

The console never edits these files. Robot mode stays locked, with the reason on the page, until:

| File | Setting |
|---|---|
| `tools/amr_console/approvals.yaml` | `proposed_system_places: true` |
| `src/amr_physical_ai/config/named_locations_proposed_system.yaml` | `approval_status: confirmed` |
| `config/tags_proposed_system.yaml` (people, desks, rooms) | `approval_status: confirmed` |
| `config/robot_capability_simulation.yaml` (tray size, payload, allowed items) | review every value, then `approval_status: confirmed` |

The capability values are proposals for the simulated robot, not measured limits. Replace them
with the real robot's numbers before any physical use. If an approved file changes while the
service runs, requests stop until the service is restarted.

## Safety

- The language worker never drives. It names an approved stop; the console takes the pose from the
  approved files, checks it again on the live map (same map, free cell, clearance) just before
  sending, and records the drive with the simulator's true pose
  (`data/runs/<run>/office/<job>/<drive>.json`).
- **STOP** (top of every page) ends the whole session, as before. **Cancel task and queue** stops
  the office task and the current drive; the navigation session keeps running.
- A drive requested before a Cancel can never start after it.
- Goals and typed commands from the other tabs are refused while an office task is using the robot.
- The conversation is kept in `data/logs/office/<stamp>/timeline.jsonl`.

## Starting

```bash
cd amr-console-dev        # in the folder that holds amr-console-dev and amr-language
bash setup/run_console.sh           # opens the console in the browser
```

On the **Office tasks** tab choose the mode, press **Start language service**, wait for *Ready*
(model B loads onto the graphics card first), choose who is asking and where they are, then type a
request and press **Send request** (or Ctrl+Enter).

### Requests to try

| Request | What should happen |
|---|---|
| Give the file to Divya. If she is not there, leave it with Priya. | read-back with the fallback, then collect from you, Divya's desk, Priya's desk only if needed |
| Ask Arun whether the encoder is ready. | goes to Arun, shows the question, brings back his exact answer |
| Tell Meera the meeting moved to 3 pm. | goes to Meera and passes the message on |
| Take this file to Divya, then ask Arun whether the encoder is ready, and come back. | three short steps (deliver, ask, come back to you), read back together |
| இந்த கோப்பை திவ்யாவிடம் கொடுங்க. | Tamil: deliver the file to Divya, read back in Tamil |
| file-ஐ Divya-கிட்ட கொடுங்க / encoder ரவியிடம் கொடு | mixed Tamil and English |
| Bring hot tea to Divya. | refused: open hot drinks are not carried |
| Go to the server room. | asks: that place is not on the map |

The Tamil lines come from the language module's tests; like all non-English wording here they
still need a native speaker's check.

### The model runner and Ubuntu 24.04

The language module starts its model runner inside a `bubblewrap` sandbox. Ubuntu 24.04 blocks
that for normal programs (`bwrap: setting up uid map: Permission denied`); the page then says
*runner_isolation_blocked*. Two choices, both the owner's:

1. **Keep the sandbox:** allow `bwrap` its user namespace with an AppArmor rule (needs `sudo`;
   a system security setting, so not done by the console):

   ```bash
   sudo tee /etc/apparmor.d/bwrap >/dev/null <<'EOF'
   abi <abi/4.0>,
   include <tunables/global>
   profile bwrap /usr/bin/bwrap flags=(unconfined) {
     userns,
     include if exists <local/bwrap>
   }
   EOF
   sudo apparmor_parser -r /etc/apparmor.d/bwrap
   ```

2. **Run it without the sandbox:** set `model_runner: uncontained` in `config/office_tasks.yaml`.
   The runner still listens on 127.0.0.1 only with cloud features off, without the extra file
   sandbox.

## Speaking instead of typing

The **Speak** buttons record up to 15 s in the browser, send it to this computer only, and put the
words in the text box. A person checks the words and presses Send; nothing spoken is acted on
directly. "Automatic" language means the speaker's own language (the requester's, or the language
the robot is using at the stop).

Speech needs its own install (about 9 GB, two parts, owner approval): see
[SPEECH_INSTALL_REQUEST.md](SPEECH_INSTALL_REQUEST.md). Until then the page says *Speech input: not
installed* and typing works as before. The microphone works on `127.0.0.1`; from a phone it needs
HTTPS.

Settings in `config/office_tasks.yaml` → `speech`: which model writes down each language, processor
threads, and `keep_audio: true` to keep each recording in `data/logs/office/<stamp>/speech/` for an
accuracy check with `scripts/speech_check.py`.

## Learning, with the owner's approval

Every job leaves suggestions in `../amr-language/records/learning_suggestions/` (kept 30 days):
the request with its read-back and outcome, a confirmed "Divya is at reception today", a new way of
saying a name, a confirmed correction. Nothing changes by itself. To review them:

```bash
cd amr-console-dev        # in the folder that holds amr-console-dev and amr-language
python3 tools/amr_console/office_context.py --write-names ~/office_names_review.yaml
cd ../amr-language
python3 scripts/review_learning.py --names ~/office_names_review.yaml \
  --capability ../amr-console-dev/config/robot_capability_simulation.yaml
```

Each suggestion is shown; *review* runs the stored checks against it, and only typing *approve*
accepts that exact change (the review needs the confirmed capability file). Map, people and desk
files are never edited by it; a new person or place goes into the tag file by hand.

## Settings (`config/office_tasks.yaml`)

| Key | Meaning |
|---|---|
| `language_module.path` | the language module folder (default `../amr-language`, version 0.5.0 or newer) |
| `language_module.profile`, `timeout_s` | model profile (`office_B`) and the longest wait for one request |
| `language_module.model_runner` | `contained` (default) or `uncontained` (above) |
| `operator` | the person at this console: name, language, where they usually are |
| `maps.<map>` | the names in each language and the robot capability file for that map |
| `continued_operation` | `closing_question` (true or false) and `follow_up_limit` (default 3) |
| `assistance` | `conversational`, `english_only`, `search_for_person`, `tentative_plans`, `model_reads_replies` |
| `speech` | speech input (above) |

People, desks and rooms come from the map's tag file; their names in Tamil, Malayalam and Telugu
and each person's language from `config/office_language_proposed_system.yaml`. All non-English
wording is a draft that needs a native speaker's check.

## Troubleshooting

| The page says | Do this |
|---|---|
| Start navigation on the office map … | Navigate tab → Start navigation on the demo office, ground truth on; or use Without the robot |
| Confirm the robot tray and weight limits … | review `config/robot_capability_simulation.yaml`, set `approval_status: confirmed` |
| runner_isolation_blocked | choose one of the two runner options above |
| The language module was not found | put `amr-language` next to this folder or set `language_module.path` |
| An approved office file changed … | Stop and start the language service |
| Speech input: not installed yet | type, or follow SPEECH_INSTALL_REQUEST.md |
