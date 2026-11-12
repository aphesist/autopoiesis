# Running the main run on a rented server

These are the steps for the pre-registered main run: 8+8 lineages × 20 generations on the
hard stdin/stdout LiveCodeBench pool, with an $80 ledger cap on Claude API spend. The
workload is about 305 slot-hours of grading, an estimate from the pilot's 32
slot-seconds per task. Server rental is paid separately from the Claude cap.

## 0. Choosing a machine

Any x86-64 Linux server (Ubuntu 22.04 or 24.04) with Docker works. Use one evaluation slot
per **physical** core: hyperthreads share a core and make the 10-second per-case timeouts
noisy. On most clouds a vCPU is a hyperthread, so physical cores = vCPUs / 2, unless the
provider sells dedicated cores.

| Physical cores (slots) | Wall-clock (estimate) | RAM | Disk |
|---|---|---|---|
| 8 | ~38 h | ≥ 16 GB | ≥ 50 GB |
| 16 | ~19 h | ≥ 24 GB | ≥ 50 GB |
| 24 | ~13 h | ≥ 32 GB | ≥ 50 GB |
| 32 | ~10 h | ≥ 40 GB | ≥ 50 GB |

Each slot's container is capped at 768 MB of memory. Disk holds the ~4 GB LiveCodeBench
download plus the run's outputs. Check the provider's current hourly price; total rental
is roughly price × wall-clock time. Stop the server once results are copied off, so
billing stops.

## 1. One-time setup

```bash
sudo apt update && sudo apt install -y docker.io git python3-venv tmux
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/aphesist/autopoiesis && cd autopoiesis
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,data]'
docker pull python:3.12-slim
cp .env.example .env   # then edit: PROJECTS_ANTHROPIC_KEY=sk-ant-...  (never commit it)
```

## 2. Build and check

```bash
.venv/bin/python scripts/make_splits_lcb.py --config configs/pilot-lcb-hard.json \
    --difficulties hard --kinds stdio --out data/lcb-stdio-hard   # downloads ~4 GB once
git diff --stat data/          # must be empty: the committed splits are reproduced
.venv/bin/pytest -q            # all tests must pass, Docker tests included
```

## 3. Set the slot count

```bash
lscpu | grep -E "^CPU\(s\)|Thread\(s\) per core|Core\(s\) per socket|Socket"
export AUTOPOIESIS_MAX_PARALLEL_EVALS=16   # = physical cores; put it in ~/.bashrc too
```

## 4. Smoke test (about 1–2 h, about $2 of the API budget)

The cloud pilot ran every stage except detection and analysis on real data before it was
stopped. Finish that on the server first. This is the same design at 1+1 lineages × 2
generations, with its own $15 ledger in `runs/pilot/`:

```bash
.venv/bin/autopoiesis --config configs/pilot-main.json run pilot
.venv/bin/autopoiesis reeval runs/pilot --k 2
.venv/bin/autopoiesis detect runs/pilot
.venv/bin/autopoiesis analyze runs/pilot > runs/pilot/analysis.json
```

Check that `analysis.json` has E1 with `"source": "reeval"` and an E5 block with all four
detectors. Also note `eval_seconds` in `runs/pilot/lineages/*/archive.jsonl`: it gives
this machine's real speed, so the wall-clock estimate can be corrected before the main run.

## 5. Run

The pre-registration is frozen with a git tag before this step. Start the run inside
`tmux`, so it survives a dropped SSH connection:

```bash
tmux new -s autopoiesis
.venv/bin/autopoiesis --config configs/main.json run main
```

Detach with `Ctrl-b d`; reattach with `tmux attach -t autopoiesis`.

- **Progress:** `runs/main/lineages/*/archive.jsonl` gains one line per child, and
  `runs/main/ledger.jsonl` records every model call and its cost.
- **Interruptions** (reboot, crash, preemption): run the same command again. Finished
  generations are kept, the interrupted generation is redone, and `runs/main/host.jsonl`
  records each start.
- **Spending:** the ledger stops the run cleanly at $80 (`runs/main/stop.json` says why).

## 6. After the loop

```bash
.venv/bin/autopoiesis reeval runs/main           # K fresh re-runs of each lineage winner and the seed
.venv/bin/autopoiesis detect runs/main           # the four E5 detectors
.venv/bin/autopoiesis analyze runs/main > runs/main/analysis.json
.venv/bin/python scripts/export_site.py runs/main site/main-data.json
```

All three commands are resumable and spend from the same $80 ledger.

Copy `runs/main/` off the server (for example with `rsync` or `scp`) before stopping it.
`runs/` is git-ignored, so the run data are not committed by accident.
