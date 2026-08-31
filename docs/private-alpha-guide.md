# AutoTLDR {{VERSION}} private-alpha guide

Support for this build: **{{SUPPORT}}**

AutoTLDR is pre-alpha local software. Commands, configuration, and output schemas may
change between builds. This preview is for observed first-user sessions; it is not a
stable release and should be used only with non-sensitive material you are permitted to
process.

## Before you begin

You need:

- a Unix-like terminal (Linux, macOS, or WSL);
- Python 3.12;
- [`uv`](https://docs.astral.sh/uv/);
- LM Studio listening locally at `http://127.0.0.1:1234`; and
- one generation model that you intentionally loaded in LM Studio.

AutoTLDR does not download, load, unload, or silently select a model. Source evidence sent
for prose generation stays on the loopback-local LM Studio endpoint. URL inputs are the
exception: they deliberately make bounded outbound HTTP(S) requests.

## Install this exact build

Run these commands from the directory containing this guide and the `packages/` folder:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --find-links packages "autotldr[all]=={{VERSION}}"
source .venv/bin/activate
autotldr --version
```

The final command must print `{{VERSION}}`. The adjacent `SHA256SUMS` file records the
exact files in this bundle.

## Connect the local model

If LM Studio has exactly one active generation model:

```bash
autotldr setup
autotldr doctor
```

If setup reports multiple active generation models, choose the one intended for this
preview and rerun the exact command it shows:

```bash
autotldr setup --model EXACT-ACTIVE-MODEL-ID
autotldr doctor
```

`doctor` sends one small synthetic conformance prompt; it does not read your artifact and
does not change model residency. Continue only when doctor reports ready.

## Try one real artifact

Quote the path and put `--` before it so a filename cannot become an option:

```bash
autotldr -- "/path/to/your/artifact"
```

The ordinary command returns a local-model prose TLDR with citations, supporting native
evidence, and explicit gaps. Choose a detail level only when it helps your task:

```bash
autotldr --detail brief -- "/path/to/your/artifact"
autotldr --detail standard -- "/path/to/your/artifact"
autotldr --detail deep -- "/path/to/your/artifact"
```

For a machine-readable agent artifact, set an exact complete-output budget:

```bash
autotldr --detail standard --out json --budget 131072 -- "/path/to/your/artifact"
```

If the complete cited result cannot fit, AutoTLDR writes no partial artifact and reports
the measured minimum. Increase the budget rather than truncating the output yourself.

## If something fails

These commands report the installed product state without changing your sources or model:

```bash
autotldr doctor
autotldr formats
autotldr config show
autotldr config paths
```

Common outcomes are intentionally specific:

- exit `2`: invalid command usage;
- exit `3`: named unsupported format or tier;
- exit `4`: input not found; and
- exit `5`: the complete addressable output cannot fit the requested budget.

Do not send source files, source excerpts, credentials, or generated reports through a
public issue. Send the command, AutoTLDR version, platform, exit status, and a redacted
error message to **{{SUPPORT}}**. Suspected security boundary failures should use a private
GitHub security advisory as described in `SECURITY.md`.

## Data boundary

Generated results may contain source paths, extracted content, schema and formula details,
hashes, citations, gaps, and model-run metadata. Store and share them with the same care as
the source. AutoTLDR is not a sandbox; run it with operating-system permissions limited to
the sources it should read.
