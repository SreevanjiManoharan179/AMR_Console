# Security

## Reporting a problem

Please report security problems privately, not in a public issue: use **Report a
vulnerability** on this repository's **Security** tab (GitHub private vulnerability reporting).
If that button is not there, contact the owner through the GitHub profile. Say what you
found, how to reproduce it and what it affects.

## What the software is built to protect

The console is a local tool for one computer running a simulator. It is not built to be exposed
to a network.

- The console and the Speech Lab listen on `127.0.0.1` only, and their API requests need the
  one-time token in the link printed when each server starts (`setup/run_console.sh` opens it in
  the browser instead, or prints it when `AMR_CONSOLE_NO_BROWSER=1`). Do not forward the port or
  publish the link.
- Only fixed, prepared commands are started; nothing typed on the page is run as a shell
  command. Records are read from an allow-listed part of `data/`, with symbolic links refused.
- The language-model runner starts inside a `bubblewrap` sandbox on `127.0.0.1`, with cloud
  inference turned off. The runner, the speech packages and the speech models are downloaded at
  pinned versions and checked against published hashes. A speech model's own code is loaded only
  when its hash matches the reviewed one (`amr-console-dev/config/speech_models.json`).
- The typed-command parser can send a command to the OpenAI API, but only when a program asks
  for that mode (or sets `OPENAI_API_KEY` and asks for `auto`); its default is rule-based, and the
  console always uses the rule-based mode.
- No credentials or tokens are stored in the repository. The speech download asks for a Hugging
  Face token in a hidden prompt and does not save it.

A real robot connection does not exist yet; anything that would move real hardware needs its
own safety review first.

## Supported versions

Only the latest commit of the default branch is maintained.
