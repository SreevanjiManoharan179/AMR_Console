# Documents

| Document | For | What it covers |
|---|---|---|
| [../README.md](../README.md) | everyone | what the module does and how it has been checked |
| [INTEGRATION.md](INTEGRATION.md) | engineers | the contract with the AMR Console: settings, events, plan data, and what changed in each version (newest first) |
| [OFFICE_DESIGN.md](OFFICE_DESIGN.md) | engineers | the design decisions behind the plan format and the fixed sentences |
| [REHEARSAL_RULES.md](REHEARSAL_RULES.md) | engineers | how a conversation is played through without a robot |
| [OWNER_CHECK_HOWTO.md](OWNER_CHECK_HOWTO.md) | owner | how to rehearse and label requests yourself |
| [INSTALL_REQUEST.md](INSTALL_REQUEST.md) | owner | the approved install of the model runner (Ollama, pinned version and hashes) |
| [SHARED_LOAD_CHECK.md](SHARED_LOAD_CHECK.md) | engineers | the model and the simulator sharing one computer (3 Nov measurement) |
| `history/` | record | dated reports of earlier versions: installs, task 3 and task 4 measurements, the translation-model comparison, legacy interface. Kept in the development repository; the public release leaves them out |

Documents in `history/` describe the module as it was on their date. Where they disagree with
`INTEGRATION.md`, `INTEGRATION.md` is right. The reports and records that `INTEGRATION.md` names
(`TASK4_HYBRID_CHECK.md`, `records/...` and the like) are in `history/` and `../records/`.
