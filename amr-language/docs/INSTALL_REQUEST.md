# Model runner install (reviewed and approved)

> Record of the install as it was reviewed and approved. The commands below are still the way to
> install the model runner; for the console, run part A, then pull the 4B model as in part B.

[checked] This is the reviewed command list. Part A installs the runner and the 2B model; part B adds the 4B model (`qwen3.5:4b-q4_K_M`), which the console uses (profile `office_B`). The owner approved both parts; the approval did not cover speech downloads or console changes.

## Sizes and scope

| Component | Download | Persistent disk budget |
| --- | --- | --- |
| Ollama v0.40.12 Linux amd64 | [checked] 1.42 GB published compressed asset | [likely] reserve 5 GB extracted, plus archive; exact extracted size not measured |
| Qwen3.5 2B Q4_K_M | [checked] 1.9 GB aggregate published package, including projector | [likely] about 1.9 GB plus metadata, allow 3 GB during pull |
| Optional Qwen3.5 4B Q4_K_M | [checked] additional 3.3 GB aggregate published package | [likely] allow additional 5 GB during pull |

[likely] Reserve 12 GB for A or 18 GB for A+B including archives, extraction and temporary files. [checked] The readiness check found approximately 822 GiB free on the project filesystem, so disk space is not the current blocker. [not checked] Peak model VRAM or latency. Package sizes are rounded publisher figures, not measured allocation sizes.

[checked] Sources: [versioned release asset and SHA-256](https://github.com/ollama/ollama/releases/expanded_assets/v0.40.12), [Ollama Linux manual extraction](https://docs.ollama.com/linux), [2B package](https://ollama.com/library/qwen3.5:2b-q4_K_M), [4B package](https://ollama.com/library/qwen3.5:4b-q4_K_M).

[checked] All persistent files below live in `.runtime/` inside this module, which Git ignores. The already-installed `bwrap` creates a transient in-memory home without changing `HOME`. This prevents Ollama's default home-state writes. Startup stdout is discarded because the inspected version creates and prints a public signing identity on first launch; the setup does not read, print or keep that identity. Its transient files disappear when the namespace exits. [checked] Source for that behaviour: [`RunServer` / `initializeKeypair`](https://raw.githubusercontent.com/ollama/ollama/v0.40.12/cmd/cmd.go). [checked] The namespace-only preflight succeeds here. [not checked] The downloaded binary's GPU execution inside that namespace; if it fails, record failure and revise the proposal instead of relaxing confinement automatically.

## A. Recommended: runtime and 2B model

Run from the module folder (`amr-language/`), in the listed order, **only after approval**:

```bash
mkdir -p .runtime/downloads .runtime/ollama .runtime/models .runtime/tmp .runtime/cache
curl --fail --location --proto '=https' --tlsv1.2 --max-filesize 2000000000 --output .runtime/downloads/ollama-linux-amd64-v0.40.12.tar.zst https://github.com/ollama/ollama/releases/download/v0.40.12/ollama-linux-amd64.tar.zst
sha256sum --check data/ollama-v0.40.12.sha256
tar --zstd --extract --file .runtime/downloads/ollama-linux-amd64-v0.40.12.tar.zst --directory .runtime/ollama --no-same-owner
python3 scripts/local_runner.py check-isolation
python3 scripts/local_runner.py serve
```

The final command stays in the foreground. In a second terminal, in the same folder:

```bash
python3 scripts/local_runner.py version
python3 scripts/local_runner.py pull qwen3.5:2b-q4_K_M
python3 scripts/local_runner.py list
python3 scripts/check_system.py --output records/checks/system_after_install.json --probe-model
```

[checked] The launcher binds `127.0.0.1:11434`, disables cloud inference, limits resident models and model parallelism to one, limits the queue to one and context to 4096, reserves 4 GiB from the scheduler's GPU budget, and keeps cache/temp/model data inside `.runtime/`. [likely] The GPU reservation reduces contention; it is a scheduling hint, not an enforced per-process memory limit. [not checked] That the runner respects this estimate under load. Measure before enabling integration. The inference client itself never pulls models. No sudo, apt, pip, service installation or driver changes are proposed.

[checked] A public model pull needs registry internet access during the approved setup. Thereafter typed input uses the loopback server. [not checked] Fully disconnected inference until tested after the download. The two model tags can change upstream: after pulling, freeze the returned manifest digest and SHA-256 of every model/projector/config/template blob before calibration. A different release checksum, download larger than the cap, or changed tag requires review of that changed artifact, not a guessed hash.

## B. Optional 4B comparison (separate approval from A)

With the same server running:

```bash
python3 scripts/local_runner.py stop qwen3.5:2b-q4_K_M
python3 scripts/local_runner.py pull qwen3.5:4b-q4_K_M
python3 scripts/local_runner.py list
```

[likely] Only run one model at a time. Reject 4B for integration if its measured peak exceeds the approximately 4 GB model budget or worsens navigation responsiveness. It must not silently replace the primary model or reuse 2B calibration.

## Removal instructions (owner only)

While the contained server is running, the owner can remove the selected local model(s):

```bash
python3 scripts/local_runner.py stop qwen3.5:2b-q4_K_M
python3 scripts/local_runner.py rm qwen3.5:2b-q4_K_M
```

For B, substitute the exact `qwen3.5:4b-q4_K_M` model name. Stop the foreground server with Ctrl-C. To remove the private runtime and caches, the owner can run, from this module folder:

```bash
rm -r -- .runtime/ollama .runtime/models .runtime/tmp .runtime/cache .runtime/downloads
```

[checked] No system package is removed and no `apt autoremove` is involved. Model removal and directory deletion are documentation for the owner; they are not part of the install approval or actions taken in this stage.

## After the install

`scripts/inspect_install.py` records the installed model's files and serving identity, and
`scripts/inspect_office_model.py` checks an installed model against them; neither downloads
anything. The measurement with the model and the simulator sharing one computer is in
[SHARED_LOAD_CHECK.md](SHARED_LOAD_CHECK.md). Real-command calibration and speech setup are
separate work.
