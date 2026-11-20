# Text rehearsal semantics and limits

[checked] The tool is a conversation rehearsal, not a robot executor. It has a local illustrative FIFO queue, no ROS import, no real driving, no production waiting timer and no Nav2 call. It records typed input and every role line inside `records/rehearsals/`.

[checked] Each accepted job shows the English rewrite/plan as screen data and fixed readback in the requester language. It requires a listed yes before any step. No/cancel/STOP never implies yes. A second `/queue` request is announced in that requester's language, waits for the current job, and gets its own readback/confirmation when selected. The approximate four-minute queue example is explicitly illustrative.

[checked] Deliver waits for taken/not_here/later. Retrieval waits for done at the source, then requires taken at the destination before reporting that the item was brought there. It never pretends to deliver an uncollected item. A leave_with policy before retrieving the item is semantically invalid and must be rephrased; this restriction is documented rather than inventing a source.

[checked] Check displays the question, rewrites a free answer to English when needed, and keeps the answer on screen for the final report. Known yes/no answers use no model. A conditional branch requires an explicit yes/no interpretation; otherwise it asks again and never guesses a branch. The check outcome is recorded before the selected child actions. Tell displays the message and waits for acknowledgement.

[checked] Absence policies can use an approved alternate person, ask for an approved available person, skip, return an undelivered carried item, or defer. A second absent alternate ends that attempt rather than looping indefinitely. A later reply without a duration records a deferral and does not invent a delay. Pending deferrals end in a waiting-for-instructions state, not a false “all jobs completed” announcement. The console owns actual retries and timers.

[checked] An unclear delivery/retrieval reply illustrates fixed reminders at the 15/30-second boundaries, with no actual delay in this text tool. After three unclear responses it records failure. The console must implement and test real wall-clock timers later.

[checked] Destination language comes from `people[].language`; a shared place without an identified interlocutor falls back explicitly to requester language. `/person <approved ID>` selects the current respondent and repeats the arrival line in their profile language. The draft example roster contains fictional language assignments; real profiles and native phrasing must be reviewed. The renderer never inserts a model's message/question/answer, free reason, or unknown item label into speech.

[checked] STOP is recognized across all four phrase lists before any expected-kind filtering, including while awaiting an answer or confirmation. A stopped rehearsal does not announce an automatic return home. Model inference itself belongs in a cancellable worker outside console control; the typed rehearsal does not claim to provide the production STOP transport or UI.

[likely] The integration builder should preserve these distinctions: model agreement is provisional intent extraction, yes is explicit confirmation, announcements are fixed templates, free content is separate UI data, and movement/state verification remains the console's responsibility.

[checked] Task 4 checks caller-supplied map facts before accepting a plan, including conditional branches and alternate recipients. Off-map/unreachable/person-location asks permit a one-job mapped replacement, followed by revised readback and a new yes. Unknown-name asks require a corrected new request. `data/task4_names_example.yaml` is explicitly scripted mock map data; it is not a live reachability report.

[checked] Person not_here first asks nearby people where the person is, with hear_reply location mode. An exact mapped/reachable alias is offered to the requester as a new step, requiring fresh yes; no answer, no, or follow_person_hint:never uses the original absence policy. An unmapped hint only logs a suggestion. Three confirmed successful interactions at the same place away from usual_place produce an owner suggestion, never a roster edit. Location choices are transient job context. The six map events are fixed in four languages, with non-English text needing native-speaker check. See INTEGRATION.md and TASK4_HYBRID_CHECK.md for fields and scripted outcomes.
