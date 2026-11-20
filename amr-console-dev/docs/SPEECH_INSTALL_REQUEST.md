# Speech input install (reviewed and approved)

The owner approved this install, and it is in use on the owner's computer. This adds local
speech-to-text for the **Speak** buttons on the Office tasks tab. Everything runs on this computer,
offline, on the processor (no extra graphics memory beside model B and Gazebo).

## What and why

| Part | Languages | Engine and model (pinned revision) | Licence |
|---|---|---|---|
| 1 | English | faster-whisper 1.2.1 with `Systran/faster-whisper-base.en` @ `3d3d5dee26484f91867d81cb899cfcf72b96be6c` (default) and `Systran/faster-whisper-small.en` @ `d1d751a5f8271d482d14ca55d9e2deeebbae577f` (for comparison) | MIT |
| 2 | Tamil, Malayalam, Telugu | AI4Bharat IndicConformer 600M multilingual, `ai4bharat/indic-conformer-600m-multilingual` @ `e9b71b369c048e2c6b634d4c131061c34e441179` (2026-02-07), RNNT decoding, ONNX on the processor | MIT; gated: the owner accepts its conditions on Hugging Face |

This follows the owner's earlier pilot choice (IndicConformer for Tamil, then Malayalam and
Telugu; Whisper base.en for English, measured before anything heavier). Considered and not
requested now: AI4Bharat's newer Indic-Transcribe-core (1.2B parameters, 4.6 GB, its own
licence with conditions on hosting it for others, about 9 GB of graphics memory in its published
batch figures) and large multilingual Whisper models (weaker for Tamil, Malayalam and Telugu).
Either can be proposed later if the measured accuracy below is not good enough.

How it is used: the page records at most 15 s, the console checks the recording (16 kHz mono
WAV), the speech worker writes it down, and the words appear in the text box. A person checks them
and presses Send; nothing spoken is acted on directly. Recordings are deleted after writing down
unless `speech.keep_audio: true`.

## Sizes

| Part | Wheels (pinned) | Models | Download |
|---|---:|---:|---:|
| 1 English | 24 wheels, 121,730,060 bytes | 628,761,874 bytes | 0.75 GB |
| 2 Tamil, Malayalam, Telugu | 26 wheels, 3,020,972,809 bytes (PyTorch 2.7.1 and its CUDA 12.6 libraries, Transformers 4.51.3, ONNX Runtime 1.20.1) | 2,553,503,826 bytes | 5.57 GB |

Installed size of both parts: about 5.7 GB environment plus 3.2 GB models. **Reserve 12 GB free
disk.** Everything goes into `~/AMR_Console/amr-speech/`; nothing system-wide, no `sudo`, no `apt`.

[checked] On a Python 3.12 Linux machine (not the owner's): both lock files install with
`--require-hashes --no-deps`, `pip check` reports no broken requirements, and faster-whisper,
CTranslate2, ONNX Runtime (processor), PyTorch, torchaudio and Transformers import.
[not checked] The model downloads (blocked on that machine), accuracy, and time on the owner's
laptop. The console side is tested with a stand-in engine (no model).

## Exact commands

From the `amr-console-dev` folder. **Part 1 (English):**

```bash
cd amr-console-dev        # in the folder that holds amr-console-dev and amr-language
python3 -m venv ../amr-speech/venv
../amr-speech/venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple --no-cache-dir \
  --only-binary=:all: --no-deps --require-hashes -r config/speech_requirements_english.lock
../amr-speech/venv/bin/python -m pip --isolated check
../amr-speech/venv/bin/python -I scripts/speech_download.py --approved-speech whisper_base_en whisper_small_en
```

**Part 2 (Tamil, Malayalam, Telugu)**, after part 1. First, signed in as yourself, open
<https://huggingface.co/ai4bharat/indic-conformer-600m-multilingual>, read and accept its
conditions, and create a **read** token in your Hugging Face settings. Then:

```bash
cd amr-console-dev        # in the folder that holds amr-console-dev and amr-language
../amr-speech/venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple --no-cache-dir \
  --only-binary=:all: --no-deps --require-hashes -r config/speech_requirements_indic.lock
../amr-speech/venv/bin/python -m pip --isolated check
../amr-speech/venv/bin/python -I scripts/speech_download.py --approved-speech indic_conformer
```

The downloader asks for the token in a hidden prompt, uses it for this download only, never prints
or saves it. Never paste a token into a chat. It downloads only the pinned revision and checks
every file against that revision's published listing (SHA-256 for model files, git blob id for the
rest); any difference stops it. The `--approved-speech` flag is a guard against running it by
accident, not a record of approval.

## After the download

1. Stop and start the language service on the Office tasks tab. The page shows
   *Speech input: ready for English*.
2. IndicConformer brings its own loader code (`model_onnx.py`). It runs only when its SHA-256
   matches `reviewed_code_sha256` in `config/speech_models.json`: the hash of the copy that was
   read and found to load and run only the local model files. Until then the page says *not
   reviewed yet* for Tamil, Malayalam and Telugu. If the file ever changes, it is refused again.
3. Measure before relying on it: set `speech.keep_audio: true`, record 30 or more requests per
   language through the page, write `clips.csv` (file, language, reference) next to the kept
   recordings, and run
   `../amr-speech/venv/bin/python -I scripts/speech_check.py --clips <folder>` (word and
   character error, time per clip). Compare `--model whisper_small_en` for English.

## Removal

Stop the language service, then:

```bash
rm -rf -- ~/AMR_Console/amr-speech
```

The console keeps working; the page says *Speech input: not installed yet*.

## The approval

The owner's approval covered exactly the commands and pinned files above, part by part (English
first, then Tamil, Malayalam and Telugu).

## Request-file integrity (SHA-256)

- `config/speech_requirements_english.lock`: `cdf8d675b49de7d0623a47d6d7fa182108d573e77c1b9043a2fa3a48047d2f43`
- `config/speech_requirements_indic.lock`: `537a64f371655ddee366d968f26c53680ddc45e7fb8e5d57e32a10f16f75d3ae`
- `config/speech_models.json`: `76d39ef5831b3ea990dcecbf0f6dcb6cbd217f05ce7f9756bff3be56750fceef` (now including the reviewed loader hash; `e17b4b2b…` when the install was requested)
- `scripts/speech_download.py`: `cf5daa89fd1d23046a53c9df1234f4621eeab93112de265e00b0b7054d06ca0d`
