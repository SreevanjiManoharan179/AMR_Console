# Speech Lab and evaluation tools

The Speech Lab is a separate test page for speech and language, without the robot. It starts the
same language service and speech models as the console's Office tasks tab, so what you measure
here is what the console does.

Close the AMR Console first (both start the same language model), then:

```bash
cd amr-console-dev        # in the folder that holds amr-console-dev and amr-language
python3 tools/speech_lab/server.py
```

The page opens at the address printed in the terminal (127.0.0.1:8770, this computer only).
Ctrl+C stops it.

## The page

| Card | What it shows |
|---|---|
| 1 · Speak | Choose English, Tamil or Tanglish, pick a sentence to read (or free speech), press Record (15 s at most). |
| 2 · Signal | Live loudness bar (green band = right level), waveform, loudest sound, room noise, voice over noise, share of overloaded samples, and advice. |
| 3 · Words as they are heard | The words about once a second while you speak; grey words may still change. After Stop: word errors, character errors, key words found (names, items), time to the first words, model time, speed. |
| 4 · What the robot understands | Sends the words to the language model: English rewrite, decision (do, ask, refuse), steps, read-back, time. Nothing is carried out. |
| 5 · This session | Totals per language and every take; download as CSV. |

Tanglish uses the Tamil speech model, which writes English words in Tamil letters ("file" →
ஃபைல்). Its word-error figure is therefore harsh; read "key words found" for Tanglish.

How the figures are counted (`tools/speech_lab/server.py`, tested in `tools/speech_lab/tests`):
both texts are first folded to lower case, with punctuation and symbols turned into spaces.

- **Word errors**: substitutions, deletions and insertions against the reference words, divided
  by the number of reference words.
- **Character errors**: the same over the characters with the spaces removed (Unicode code
  points, so a Tamil vowel sign counts as one character).
- **Key words found**: a key word (any of its spellings, `|`) counts when it starts a word of
  what was heard. Suffixes count (திவ்யா in திவ்யாவிடம்); a word that only contains it does not
  ("ready" in "already"), and a number must not continue with another digit ("3" in "3 pm" and
  "3pm", not in "13" or "30"). Until 20 Nov 2026 any match inside a word counted.

## Logs

Each session writes `data/evaluation/speech_lab/<time>/`: `takes.jsonl` (one line per take with
every figure), `understand.jsonl`, and `audio/` when "Keep the recording" is ticked.

## Other evaluation tools

| Command | What it does | Writes |
|---|---|---|
| `bash scripts/mic_check.sh` | four test takes of the laptop microphone at four gain settings; `--apply` sets the best one | `data/evaluation/mic_check/` |
| `../amr-speech/venv/bin/python -I scripts/speech_fleurs.py --count 15` | fetches native-speaker test sentences (Google FLEURS, CC BY 4.0) in English, Tamil, Malayalam, Telugu | `data/evaluation/speech_fleurs/` |
| `../amr-speech/venv/bin/python -I scripts/speech_check.py --clips <folder>` | word and character errors of the speech models on a folder of clips | `<folder>/speech_check.json` |
| `python3 scripts/office_request_check.py --only en` | the office request check: 30 English test requests through the real language model, each compared with the result it should give (without `--only en` it also runs the other-language cases kept for this lab) | `data/evaluation/office_requests/<time>/` |
