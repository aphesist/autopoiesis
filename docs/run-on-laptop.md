# Running the main run on a laptop

These are the steps for the pre-registered main run: 8+8 lineages × 20 generations on the
hard stdin/stdout LiveCodeBench pool, with an $80 ledger cap. The run was sized for an
Intel i9-13900H (14 cores / 20 threads) with 12 evaluation slots, about 25 hours of
wall-clock time (an estimate from the pilot's 32 slot-seconds per task).

## 1. One-time setup (Windows → WSL2)

1. Install WSL2 with Ubuntu: run `wsl --install -d Ubuntu` in an administrator PowerShell,
   then reboot.
2. Give WSL enough resources. In `%UserProfile%\.wslconfig` put:
   ```
   [wsl2]
   processors=14
   memory=12GB
   ```
   then run `wsl --shutdown` to apply it.
3. Install Docker inside Ubuntu, for example Docker Engine:
   `sudo apt install docker.io && sudo usermod -aG docker $USER`, then reopen the
   terminal. Docker Desktop with WSL integration also works.
4. Clone into the Linux filesystem, **not** `/mnt/c`. The worker proxy is a Unix socket,
   and sockets do not work on the Windows mount.
   ```bash
   cd ~ && git clone https://github.com/aphesist/autopoiesis && cd autopoiesis
   python3 -m venv .venv && .venv/bin/pip install -e '.[dev,data]'
   docker pull python:3.12-slim
   ```
5. Put the API key in a local `.env`, which git ignores. Never commit it.
   ```bash
   cp .env.example .env   # then edit: PROJECTS_ANTHROPIC_KEY=sk-ant-...
   ```

## 2. Build and check

```bash
.venv/bin/python scripts/make_splits_lcb.py --config configs/pilot-lcb-hard.json \
    --difficulties hard --kinds stdio --out data/lcb-stdio-hard   # downloads ~4 GB once
git diff --stat data/          # must be empty: the committed splits are reproduced
.venv/bin/pytest -q            # all tests must pass, Docker tests included
```

## 3. Laptop settings for a ~25-hour run

- Keep it plugged in. Set Windows power mode to "Best performance", and set sleep and
  lid-close to "Never" while on power.
- Keep it ventilated. Sustained load on a laptop CPU throttles, which slows the run but
  does not change results: every per-case timeout is 10 s against a p99 of 0.18 s for
  honest start-up and I/O.
- Close heavy applications. The 12 evaluation slots assume the machine is otherwise
  mostly idle.

## 4. Run

The pre-registration is frozen with a git tag before this step. Start the run inside
`tmux`, so it survives a closed terminal:

```bash
tmux new -s autopoiesis
.venv/bin/autopoiesis --config configs/main.json run main
```

Detach with `Ctrl-b d`; reattach with `tmux attach -t autopoiesis`.

- **Progress:** `runs/main/lineages/*/archive.jsonl` gains one line per child, and
  `runs/main/ledger.jsonl` records every model call and its cost.
- **Interruptions** (reboot, crash, sleep): run the same command again. Finished
  generations are kept, the interrupted generation is redone, and `runs/main/host.jsonl`
  records each start.
- **Spending:** the ledger stops the run cleanly at $80 (`runs/main/stop.json` says why).

## 5. After the loop

```bash
.venv/bin/autopoiesis reeval runs/main           # K fresh re-runs of each lineage winner and the seed
.venv/bin/autopoiesis detect runs/main           # the four E5 detectors
.venv/bin/autopoiesis analyze runs/main > runs/main/analysis.json
.venv/bin/python scripts/export_site.py runs/main site/main-data.json
```

All three commands are resumable and spend from the same $80 ledger.
