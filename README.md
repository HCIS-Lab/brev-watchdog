# Brev watchdog

A standalone script that watches the running instances of one or more NVIDIA Brev organizations, measures how busy they are, and raises an alarm when an instance has been idle for too long.

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [SSH access requirement and workaround (read this first)](#ssh-access-requirement-and-workaround-read-this-first)
- [Setup](#setup)
- [Usage](#usage)
- [Example output](#example-output)
- [Idle rule](#idle-rule)
- [Config reference](#config-reference)
- [Alert adapters](#alert-adapters)
- [Estimated cost](#estimated-cost)
- [Known limitations](#known-limitations)

## What it does

Every `interval_seconds` the watchdog:

1. Lists running instances with `brev ls --all --json`, once per org in `orgs` (org name or org-ID).
2. Enriches them via the Brev REST API (creator, created time/age, estimated compute spend, org credit balance), using the token the `brev` CLI stores after login.
3. Collects hardware stats over SSH: CPU, RAM, disk, and GPU utilization / memory. CPU and GPU values are **peaks** over a per-poll sampling window of `probe_seconds` (one sample per second).
4. Decides whether each instance is idle according to the `[idle]` config.
5. Raises alarms through pluggable adapters (`log`, `webhook`, `command`).

The Rich console shows three things: a **Running instances** table, an **Alarms** panel, and an **Alert channels** table. It only alarms; it never stops or deletes anything.

## Requirements

- [`uv`](https://docs.astral.sh/uv/). The script is a single file with PEP 723 inline dependencies (`rich`), so `uv run` handles the environment.
- The `brev` CLI, logged in (`brev login`).
- SSH access to the instances (see the next section; this is the hard part).
- On the instances: Linux with `/proc` and a POSIX `sh`. `nvidia-smi` is used when present.

## SSH access requirement and workaround (read this first)

**The requirement.** To collect stats, the watchdog needs SSH access to every instance in the organization. Brev, however, only grants SSH access to the instance **creator**: the API's `ssh_access` lists only the creator, and `brev exec` cannot reach instances created by other users. Out of the box, the watchdog therefore sees only your own instances; everyone else's show up as `NO METRICS` / `BLIND`.

**The workaround: a Brev Launchable.** An organization admin configures a launchable whose setup script appends the public key(s) of the authorized watchdog machine(s) to the instance user's `authorized_keys` (user is `ubuntu`). All users in the organization **must** launch their instances through that launchable. Create the launchable in the Brev console; see the Brev launchables documentation for how to define a launchable and its setup script.

The example script is `testbed/setup.sh`. It runs at instance boot and is idempotent (the key is only appended if not already present):

```bash
#!/bin/bash
KEY='ssh-ed25519 AAAA... watchdog-host'
install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
touch /home/ubuntu/.ssh/authorized_keys
grep -qF "$KEY" /home/ubuntu/.ssh/authorized_keys || echo "$KEY" >> /home/ubuntu/.ssh/authorized_keys
chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys
chmod 600 /home/ubuntu/.ssh/authorized_keys
```

Replace `KEY` with the public key of your watchdog host (one `KEY` line per authorized host if you have several).

> **Disclaimer.**
> - This is a **workaround, not an official Brev monitoring feature.**
> - Instances launched any other way (CLI `brev create` without the startup script, other launchables, the console) **will not be monitored**; they appear as `NO METRICS` / `BLIND`.
> - It grants the watchdog host(s) **login access to every instance**. Treat that private key as a high-value secret: restrict who can use the host, use a **dedicated key pair** rather than a personal key, and get organization/security approval before rolling it out.
> - Rotating keys requires updating the launchable **and** re-launching or manually patching existing instances.

## Setup

1. Install `uv` and the `brev` CLI.
2. Log in: `brev login`.
3. On the watchdog host, generate a dedicated key (no personal key):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/brev_watchdog -C brev-watchdog
   ```
4. Put the contents of `~/.ssh/brev_watchdog.pub` into the launchable setup script (see above), and have all users launch through that launchable.
5. Edit `config.toml`: set `ssh_key = "~/.ssh/brev_watchdog"` and `orgs = ["<org name or org-ID>"]`. Adjust the `[idle]` thresholds and `[alerts.*]` tables.
6. Test with a single dry run (collects and prints, sends no alarms):
   ```bash
   uv run watchdog.py -c config.toml --once --dry-run
   ```
7. Run continuously. Under a terminal multiplexer (e.g. `tmux`) or a service manager (e.g. a systemd unit running the command below); when stdout is not a terminal it re-prints the tables each cycle instead of using the live view.

`config.template.toml` has the same content as `config.toml`; keep one as the pristine template.

## Usage

```bash
uv run watchdog.py -c config.toml [--once] [--dry-run] [-v]
```

| Flag | Meaning |
| --- | --- |
| `-c`, `--config` | Path to the TOML config (default `config.toml`). A missing file falls back to defaults with a warning. |
| `--once` | Poll a single time, print the tables, exit. |
| `--dry-run` | Collect and display metrics but never track idleness or send alarms. |
| `-v`, `--verbose` | Debug logging. |

## Example output

The console is a live view (refreshed every 0.5s, re-polled every `interval_seconds`); with `--once` or a non-TTY it prints a single frame. Status colors: `active` green, `idle N/Mm` yellow (idle for N of the M minutes required), `ALARM` white on red, `NO METRICS` magenta. CPU/GPU values under their idle threshold show yellow, disk at 90% or more shows red.

The frames below are **illustrative renders with mocked data** (names, creators, balance and numbers are invented), produced by the real renderer and shown without colors.

#### All instances active
```text
Brev watchdog  every 60s | idle = cpu<15% gpu_util<5% gpu_mem<5% for 5m | next poll in 40s
credit balance  an-org: $182.40
Running instances
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━┳━━━━━┳━━━━━━┳━━━━━┳━━━━━━━━━┳━━━━━━━━┓
┃ Instance    ┃ Org                ┃ Machine              ┃ Creator           ┃   Age ┃ Est. $ ┃ CPU ┃ RAM ┃ Disk ┃ GPU ┃ GPU mem ┃ Status ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━╇━━━━━╇━━━━━━╇━━━━━╇━━━━━━━━━╇━━━━━━━━┩
│ dev-box     │ an-org  │ m8g.large            │ alice@example.com │ 1d 6h │   1.16 │ 41% │ 55% │  37% │   - │       - │ active │
│ train-a100  │ an-org  │ a2-highgpu-1g (A100) │ bob@example.com   │ 6h 0m │  22.02 │ 72% │ 61% │  52% │ 96% │     88% │ active │
└─────────────┴────────────────────┴──────────────────────┴───────────────────┴───────┴────────┴─────┴─────┴──────┴─────┴─────────┴────────┘
╭─ Alarms ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ no alarms                                                                                                                                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Alert channels
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Name                ┃ Type                ┃ Target                    ┃ Sent                ┃ Last               ┃ State                 ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ log                 │ log                 │ log                       │ 0                   │ -                  │ ok                    │
└─────────────────────┴─────────────────────┴───────────────────────────┴─────────────────────┴────────────────────┴───────────────────────┘
```

#### Idle instance raised an alarm (sandbox-7 is idle but still inside the 5 min window)
```text
Brev watchdog  every 60s | idle = cpu<15% gpu_util<5% gpu_mem<5% for 5m | next poll in 40s
credit balance  an-org: $182.40
Running instances
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━┳━━━━━┳━━━━━━┳━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┓
┃ Instance   ┃ Org               ┃ Machine             ┃ Creator           ┃   Age ┃ Est. $ ┃ CPU ┃ RAM ┃ Disk ┃ GPU ┃ GPU mem ┃ Status    ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━╇━━━━━╇━━━━━━╇━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
│ train-a100 │ an-org │ a2-highgpu-1g       │ bob@example.com   │ 6h 0m │  22.02 │  3% │ 22% │  52% │  0% │      1% │ ALARM     │
│            │                   │ (A100)              │                   │       │        │     │     │      │     │         │           │
│ dev-box    │ an-org │ m8g.large           │ alice@example.com │ 1d 6h │   1.16 │ 41% │ 55% │  37% │   - │       - │ active    │
│ sandbox-7  │ an-org │ m8g.medium          │ carol@example.com │ 2h 0m │   0.08 │  9% │ 24% │   8% │   - │       - │ idle 3/5m │
└────────────┴───────────────────┴─────────────────────┴───────────────────┴───────┴────────┴─────┴─────┴──────┴─────┴─────────┴───────────┘
╭─ Alarms (1 idle, 0 unmonitored) ─────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ IDLE  train-a100  idle 18m  last alert 7m ago  check and close                                                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Alert channels
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ Name             ┃ Type                  ┃ Target                                   ┃ Sent          ┃ Last              ┃ State          ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ log              │ log                   │ log                                      │ 3             │ 7m ago            │ ok             │
│ slack            │ webhook               │ hooks.slack.com                          │ 3             │ 7m ago            │ ok             │
└──────────────────┴───────────────────────┴──────────────────────────────────────────┴───────────────┴───────────────────┴────────────────┘
```

#### Instance launched outside the launchable (no SSH access)
```text
Brev watchdog  every 60s | idle = cpu<15% gpu_util<5% gpu_mem<5% for 5m | next poll in 40s
credit balance  an-org: $182.40
Running instances
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━┳━━━━━┳━━━━━━┳━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Instance      ┃ Org                ┃ Machine     ┃ Creator            ┃    Age ┃  Est. $ ┃ CPU ┃ RAM ┃ Disk ┃ GPU ┃ GPU mem ┃ Status     ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━╇━━━━━╇━━━━━━╇━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━┩
│ dev-box       │ an-org  │ m8g.large   │ alice@example.com  │  1d 6h │    1.16 │ 41% │ 55% │  37% │   - │       - │ active     │
│ other-launch  │ an-org  │ m8g.medium  │ dave@example.com   │  1h 0m │    0.04 │ n/a │ n/a │  n/a │   - │       - │ NO METRICS │
└───────────────┴────────────────────┴─────────────┴────────────────────┴────────┴─────────┴─────┴─────┴──────┴─────┴─────────┴────────────┘
╭─ Alarms (0 idle, 1 unmonitored) ─────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ BLIND  other-launch  Permission denied (publickey); instance not found    metrics unavailable                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Alert channels
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Name                ┃ Type                ┃ Target                    ┃ Sent                ┃ Last               ┃ State                 ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ log                 │ log                 │ log                       │ 0                   │ -                  │ ok                    │
└─────────────────────┴─────────────────────┴───────────────────────────┴─────────────────────┴────────────────────┴───────────────────────┘
```

Columns: `Age` is time since the instance was created, `Est. $` is hourly base price x age (compute only), `-` in a GPU column means a CPU-only instance, `n/a` means the data is unavailable. In the third frame the last row shows `NO METRICS` and the alarms panel lists it as `BLIND` (unmonitored, never alarmed).

## Idle rule

- A poll is **idle** only if **all enabled** thresholds are satisfied by the **peaks** in that poll's sampling window. A threshold set to nothing (commented out) is not checked; if no threshold can be checked, the poll is not idle.
- **Any non-idle poll resets the idle timer.** The alarm fires after `idle_minutes` of consecutive idle polls, and repeats every `realert_minutes` while the instance stays idle.
- **GPU rules are skipped on CPU-only instances.**
- A **GPU instance whose GPU stats failed** (e.g. driver down) is **never judged idle**.
- An **unreachable instance is unknown, not idle**: it is shown as `NO METRICS` in the table and `BLIND` in the alarms panel, its idle state is left untouched, and it is never alarmed.
- Disk is displayed (red at 90% or more) but never used for idle decisions.

Noise baseline: the peak of 1s CPU samples on an **unloaded 1-vCPU instance measured 5-11%** (the probe itself plus Brev agents), which is why the sample config uses `cpu_percent_below = 15`. Lower it on larger machines.

Gap caveat: sampling happens only during each probe window. Spikes that fall between probe windows can be missed. Keep `interval_seconds` small relative to `idle_minutes` and make `probe_seconds` as large as is practical (coverage is roughly `probe_seconds / interval_seconds`).

## Config reference

Top-level keys (defaults from `DEFAULTS` in `watchdog.py`):

| Key | Default | Meaning |
| --- | --- | --- |
| `interval_seconds` | `300` | Time between polls. |
| `orgs` | `[]` | Orgs to watch, by name or org-ID. Empty means the active org only (and then REST enrichment/balance is skipped, since no org-ID is resolved). |
| `probe_seconds` | `1` | Sampling window per poll; CPU/GPU metrics are peaks over it. |
| `exec_timeout_seconds` | `60` | Timeout for brev/SSH calls. Raised to `probe_seconds + 30` if it is within 10s of `probe_seconds`. |
| `ssh_key` | `~/.ssh/id_ed25519` | Private key used for direct SSH when the instance's host/port/user are known from the REST API. If direct SSH is not possible or fails, `brev exec` is tried as a fallback. |
| `api_base` | `https://brevapi.us-west-2-prod.control-plane.brev.dev` | Brev REST base URL used for enrichment. Set to empty to disable. |
| `max_parallel` | `4` | Instances probed concurrently. |
| `ignore` | `[]` | Instance names that are never alarmed (they are also not probed or listed). |

`[idle]` table:

| Key | Default | Meaning |
| --- | --- | --- |
| `idle_minutes` | `30` | How long an instance must stay idle before alarming. |
| `realert_minutes` | `120` | Repeat the alarm this often while still idle. |
| `cpu_percent_below` | `5.0` | Peak CPU must be below this. |
| `ram_percent_below` | unset | Peak RAM used must be below this. Unset means not checked. |
| `gpu_util_percent_below` | `5.0` | Max GPU utilization across GPUs must be below this. Skipped on CPU-only instances. |
| `gpu_mem_percent_below` | `None` | Max GPU memory used percentage must be below this. Unset means not checked. Skipped on CPU-only instances. |

`[alerts.<name>]` tables: each needs `type` (`log`, `webhook`, `command`) and may set `enabled = false`. Default is a single `[alerts.log]`. See below.

Note that `config.toml` overrides these defaults (for example `interval_seconds = 60`, `probe_seconds = 15`, `idle_minutes = 5`, `cpu_percent_below = 15.0`, `gpu_mem_percent_below = 5.0`), so check it before relying on the values above.

## Alert adapters

| Type | Keys | Behavior |
| --- | --- | --- |
| `log` | none | Logs an `ALARM` line at error level. |
| `webhook` | `url`, `payload_key` (default `text`), `headers` (optional table) | POSTs JSON `{payload_key: message}`. The console shows only the host, never the secret path. |
| `command` | `command` | Runs the command in a shell with the alert JSON (`instance`, `org`, `idle_minutes`, `metrics`) on stdin; message in `$WATCHDOG_MESSAGE`, instance name in `$WATCHDOG_INSTANCE`. 60s timeout. |

Example:

```toml
[alerts.slack]
type = "webhook"
url = "https://hooks.slack.com/services/XXX"
payload_key = "text"

[alerts.notify]
type = "command"
command = "notify-send \"$WATCHDOG_MESSAGE\""
```

A failing adapter is logged and shown as `error: ...` in the Alert channels table; other adapters still run.

**Adding a new adapter.** In `watchdog.py`, subclass `AlertAdapter`, decorate it with `@register("type")`, implement `send(self, alert)` (and optionally `target()` for a short secret-free description in the console), then configure it with a TOML table whose `type` is the registered name:

```python
@register("mytype")
class MyAdapter(AlertAdapter):
    def send(self, alert: Alert) -> None:
        do_something(self.cfg["foo"], alert.text())

    def target(self) -> str:
        return "mytype"
```

```toml
[alerts.mine]
type = "mytype"
foo = "bar"
```

An unknown `type` aborts startup with the list of known types.

## Estimated cost

The "Est. $" column is **compute only**: the instance type's hourly base price multiplied by its age. It excludes storage, egress and anything else. "Age" is measured **since creation, not since the last start**, so a stopped-and-restarted instance overstates its spend. Treat it as a rough indicator, not a bill.

## Known limitations

- `api_base` points at an **undocumented endpoint** used by the `brev` CLI and may change without notice. If enrichment fails, a warning is logged and creator/age/cost show as `n/a`.
- The API token is read from `~/.brev/credentials.json` (written by `brev login`). On a 401 the script runs a `brev` command to trigger a token refresh and retries once.
- **Alarm only**: there is no auto-stop or auto-delete.
- **State is in memory**: restarting the watchdog resets all idle timers.
- Stats require Linux with `/proc` and a POSIX `sh` on the instance.
- Monitoring coverage depends entirely on SSH access (see the SSH section); instances without it are `BLIND`.
