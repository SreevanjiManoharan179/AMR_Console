# Office assistant scenarios

What people in an office ask a small carrier robot to do, what this console's robot does in each
case, and what is left for later. English only in this console; the Speech Lab keeps Tamil,
Malayalam and Telugu for testing.

Status words: **Works** = built and covered by automated tests with a stand-in language model.
**Later** = not built. Rows marked Works still need the owner's run with the real model
(qwen3.5:4b) before they count as verified.

Nothing in any row moves the robot before the requester says yes to the read-back, and "stop"
ends any job at any moment.

## 1. Carrying things

| Someone says | The robot | Status |
|---|---|---|
| Give the file to Divya. | Reads the plan back, loads at the requester, hands over at Divya's desk | Works |
| Give the file to Divya, then the charger to Ravi, then the folder to Meera, then the letter to Arun. | Loads all four in one visit, then four stops | Works |
| Give the file, the charger and the folder to Divya. | One stop with a stack of three | Works |
| Five or more items in one request | Refuses a stack of more than four for one person; a fifth item for another person makes a second trip | Works |
| Collect the charger from Ravi. / Get the report from Arun and take it to Meera. | Goes to the source, asks for the item, brings it to the requester or the named person | Works |
| An item that is heavy, does not fit, or is not allowed | Refuses and says why; asks at pickup when it is unsure | Works |
| The person at the stop says "no, it's not mine" | "No problem", takes it back to the requester, reports "Divya did not take the file" | Works |
| The total weight of a stack | Not added up; the capability file has no item weights | Later |
| The tray notices an item placed or removed | Needs the weighing sensors of the hardware | Later |

## 2. Messages, questions, calling people

| Someone says | The robot | Status |
|---|---|---|
| Tell Ravi the meeting moved to 3. | Goes to Ravi, shows the message, waits for "okay" | Works |
| Ask Arun whether the encoder is ready. | Asks, shows the exact answer for Arun to confirm, brings it back | Works |
| Ask Arun whether the encoder is ready. If yes, go to Divya and tell her it is ready. If no, call Ravi. | One question; the answer chooses the next step | Works |
| Call Ravi. / Call Ravi to the conference hall. / Ask Ravi to come here. | Goes to Ravi with "Please come" | Works |
| Go to Divya. | Greets her, says who sent it, and waits for what she needs; her request becomes her own task | Works |
| Where is Ravi? | Says Ravi's usual place; does not move | Works |
| A reply the robot should carry back as free text ("tell him I'll come at four") | Carried only as the exact answer to a question | Works for questions; Later for messages |

## 3. When someone is not there

| What happens | The robot | Status |
|---|---|---|
| Nobody answers | Calls the person by name twice more | Works |
| Still nobody, or someone says "she's not here" | Asks there where the person is, then looks in the two nearest rooms, then the open office | Works |
| Someone says "she is in the cafeteria" | Goes there and carries on | Works |
| Not found anywhere | Says so, follows the request's own rule, returns to the dock | Works |
| "If she is not there, leave it with Priya / bring it back / try again in 10 minutes / ask anyone there" | Follows that rule | Works |
| "If she is not there, go to Priya and ask where she is" (any other instruction) | Runs those steps only if the person is away | Works |
| "Come back later" / "I'm busy" | Leaves the step for later and reports it; the console owner reschedules | Works; automatic retry Later |

## 4. Understanding people

| What happens | The robot | Status |
|---|---|---|
| A clear request | "Sure. I'll take the file to Divya. Shall I go ahead?" | Works |
| A long request | Read back as "First I'll … Then I'll … And last, I'll …" | Works |
| The requester says "no" | "No problem. What should I do instead?" and understands the new request | Works |
| The requester answers with a correction ("No, give it to Ravi instead") | Takes the correction and reads the new plan back | Works |
| The robot is not sure it understood | Reads its best plan back with "please check this" | Works |
| A name it does not know, or two people with the same name | Asks who is meant, with suggestions | Works |
| A place that is not on the map | Says so and offers mapped places | Works |
| More than six steps | Asks for the request in parts | Works |
| "Make tea", "open the door", "use the lift" | Says politely that it cannot, and what it can do | Works |
| "Don't give the file to Ravi" | Asks to be told what to do rather than what not to do | Works |
| "Hello", "thank you", "what can you do?", "who are you?" | Answers; no plan, no movement | Works |
| Words it cannot make sense of | "Sorry, I didn't understand that. Could you say it again slowly, in short sentences?" | Works |
| A reply it does not recognise | Asks again, then asks for it slowly; the language model may read a free reply | Works; the model's reading is untested with the real model |
| A request or reply in Tamil, Malayalam, Telugu or Tanglish | "Sorry, I can only work in English for now. Could you say that again in English, please?" | Works |
| Spoken replies without touching the screen | "Listen for spoken replies" opens the microphone after each question | Built; needs the owner's microphone test |
| A spoken request without pressing Speak (a wake word) | | Later |

## 5. Several people

| What happens | The robot | Status |
|---|---|---|
| A second request while it works | Queues it and says where it is in the queue | Works |
| After the last step, at a listed person | Asks whether they need anything else; their request runs next, at most three in a row | Works |
| Someone stops the robot in a corridor with a new request | | Later |
| Two requests going the same way are combined | | Later |

## 6. Safety and limits

| What happens | The robot | Status |
|---|---|---|
| "Stop" in any reply, the STOP button, Cancel | Stops; the queue ends | Works |
| A stop it cannot reach | Says so, brings a carried item back | Works |
| Stairs, lifts, doors it would have to open | Refuses those routes | Works |
| Low battery, a blocked corridor, a person in the way | Navigation handles obstacles; battery is not modelled in the simulation | Later |

## Switches

`config/office_tasks.yaml`: `assistance` (conversational, english_only, search_for_person,
tentative_plans, model_reads_replies) and `continued_operation` (closing_question,
follow_up_limit). Each can be turned off on its own.
