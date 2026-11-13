#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["rich>=13"]
# ///
"""Brev watchdog: poll running instances, collect hardware usage over SSH
(via `brev exec`), raise an alarm through pluggable adapters when an
instance stays idle.

Usage:
    uv run watchdog.py -c config.toml            # loop forever
    uv run watchdog.py -c config.toml --once     # single poll, then exit
    uv run watchdog.py -c config.toml --once --dry-run   # print metrics only

Reuses the existing `brev login` session; no credentials handled here.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

log = logging.getLogger("brev-watchdog")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULTS = {
    "interval_seconds": 300,
    "orgs": [],  # empty = active org only
    "probe_seconds": 1,  # sampling window per poll; metrics are PEAKS over it
    "exec_timeout_seconds": 60,
    "ssh_key": "~/.ssh/id_ed25519",  # key authorized on instances; direct SSH used when set and target known
    "api_base": "https://brevapi.us-west-2-prod.control-plane.brev.dev",  # REST enrichment
    "max_parallel": 4,
    "ignore": [],  # instance names never alarmed
    "idle": {
        "idle_minutes": 30,  # must stay idle this long before alarm
        "realert_minutes": 120,  # repeat alarm interval while still idle
        "cpu_percent_below": 5.0,
        "ram_percent_below": None,  # unset = not checked
        "gpu_util_percent_below": 5.0,  # max across GPUs; skipped if no GPU
        "gpu_mem_percent_below": None,
    },
    "alerts": {"log": {"type": "log"}},
}


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if path.exists():
        user = tomllib.loads(path.read_text())
        idle = user.pop("idle", {})
        cfg.update(user)
        cfg["idle"].update(idle)
    else:
        log.warning("config %s not found, using defaults", path)
    return cfg


# --------------------------------------------------------------------------
# Brev access
# --------------------------------------------------------------------------


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@dataclass
class Instance:
    name: str
    org: str | None
    status: str
    raw: dict = field(repr=False, default_factory=dict)

    # Optional enrichment: shown as n/a unless brev's JSON carries them.
    @property
    def machine(self) -> str:
        return str(self.raw.get("instance_type") or "?")

    @property
    def gpu(self) -> str:
        g = self.raw.get("gpu")
        return "" if g in (None, "", "-") else str(g)

    @property
    def ssh_target(self) -> tuple[str, int, str] | None:
        """(host, port, user) from REST fields; None when unknown."""
        host, port, user = self.raw.get("dns"), self.raw.get("sshPort"), self.raw.get("sshUser")
        if host and port and user:
            return str(host), int(port), str(user)
        return None

    @property
    def expects_gpu(self) -> bool:
        return self.raw.get("instance_kind") == "gpu" or bool(self.gpu)

    @property
    def creator(self) -> str | None:
        for k in ("created_by", "createdBy", "created_by_user", "creator", "user"):
            v = self.raw.get(k)
            if isinstance(v, dict):
                v = v.get("email") or v.get("name") or v.get("id")
            if v:
                return str(v)
        return None

    @property
    def created_at(self) -> datetime | None:
        for k in ("created_at", "createdAt", "created"):
            v = self.raw.get(k)
            if not v:
                continue
            try:
                if isinstance(v, (int, float)):
                    return datetime.fromtimestamp(v, timezone.utc)
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                continue
        return None

    @property
    def credits_used(self) -> float | None:
        """Estimated compute spend (USD) = hourly base price x age. Excludes storage/egress."""
        try:
            price = float(self.raw["instanceTypeInfo"]["base_price"]["amount"])
        except (KeyError, TypeError, ValueError):
            return None
        if not self.created_at:
            return None
        hours = (datetime.now(timezone.utc) - self.created_at).total_seconds() / 3600
        return price * max(hours, 0)


def resolve_org(org: str, timeout: int) -> tuple[str, str | None]:
    """Accept org name or org-ID (brev -o only takes names). Returns (name, id)."""
    p = run(["brev", "ls", "orgs", "--json", "--no-check-latest"], timeout)
    if p.returncode != 0:
        raise RuntimeError(f"brev ls orgs failed: {p.stderr.strip()}")
    for o in json.loads(p.stdout):
        if org in (o.get("id"), o.get("name")):
            return o["name"], o.get("id")
    raise RuntimeError(f"org {org} not found")


# --------------------------------------------------------------------------
# Brev REST API (read-only enrichment: creator, created time, price, balance)
# Auth: reuses the token brev CLI stores after `brev login`.
# --------------------------------------------------------------------------

CRED_FILE = Path.home() / ".brev" / "credentials.json"


class BrevAPI:
    def __init__(self, base: str, timeout: int = 20):
        self.base, self.timeout = base.rstrip("/"), timeout
        self._users: dict[str, str | None] = {}

    def _token(self) -> str:
        return json.loads(CRED_FILE.read_text())["access_token"]

    def get(self, path: str):
        for attempt in (0, 1):
            req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self._token()}"})
            try:
                return json.load(urllib.request.urlopen(req, timeout=self.timeout))
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    run(["brev", "ls", "orgs", "--no-check-latest"], self.timeout)  # CLI refreshes token
                    continue
                raise

    def workspaces(self, org_id: str) -> list[dict]:
        return self.get(f"/api/organizations/{org_id}/workspaces") or []

    def balance(self, org_id: str) -> float | None:
        try:
            return float(self.get(f"/api/organizations/{org_id}/credits")["balance_usd"])
        except Exception:  # noqa: BLE001
            return None

    def user_label(self, uid: str) -> str | None:
        if uid not in self._users:
            try:
                u = self.get(f"/api/users/{uid}")
                self._users[uid] = u.get("email") or u.get("username") or u.get("name")
            except Exception as e:  # noqa: BLE001
                log.debug("user lookup %s failed: %s", uid, e)
                self._users[uid] = None
        return self._users[uid]


def list_running(org: str | None, timeout: int, api: BrevAPI | None = None, org_id: str | None = None) -> list[Instance]:
    cmd = ["brev", "ls", "--all", "--json", "--no-check-latest"]
    if org:
        cmd += ["-o", org]
    p = run(cmd, timeout)
    if p.returncode != 0:
        raise RuntimeError(f"brev ls failed: {p.stderr.strip() or p.stdout.strip()}")
    data = json.loads(p.stdout)
    items = data.get("workspaces") or []  # null when empty
    extra: dict[str, dict] = {}
    if api and org_id and items:
        try:
            extra = {w["id"]: w for w in api.workspaces(org_id)}
        except Exception as e:  # noqa: BLE001
            log.warning("REST enrichment failed (creator/uptime/cost unavailable): %s", e)
    out = []
    for w in items:
        status = str(w.get("status") or w.get("Status") or "").upper()
        name = w.get("name") or w.get("Name")
        if extra.get(w.get("id")):
            w = {**w, **extra[w["id"]]}
            if api and w.get("createdByUserId"):
                w["created_by"] = api.user_label(w["createdByUserId"]) or w["createdByUserId"]
            w["status"] = status  # keep CLI casing
            w["instance_type"] = w.get("instance_type") or w.get("instanceType")
        if name and status == "RUNNING":
            out.append(Instance(name=name, org=org, status=status, raw=w))
    return out


# Remote probe: plain POSIX sh, key=value lines. CPU = delta of two /proc/stat
# samples 1s apart. GPU lines only when nvidia-smi exists.
# Remote probe: POSIX sh, key=value lines. Samples once per second for __N__ seconds and
# reports PEAK cpu / gpu util / gpu mem over the window, so short spikes are not missed.
_PROBE_TMPL = r"""
N=__N__
read_cpu() { awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8, $5+$6}' /proc/stat; }
HAVE_GPU=0
if command -v nvidia-smi >/dev/null 2>&1; then HAVE_GPU=1; fi
set -- $(read_cpu); t1=$1; i1=$2
maxc=0; G=""; k=0
while [ $k -lt $N ]; do
  sleep 1
  set -- $(read_cpu); t2=$1; i2=$2
  c=$(awk -v t1=$t1 -v t2=$t2 -v i1=$i1 -v i2=$i2 'BEGIN{d=t2-t1; v=(d>0) ? 100*(1-(i2-i1)/d) : 0; printf "%.1f", v}')
  maxc=$(awk -v a=$maxc -v b=$c 'BEGIN{printf "%.1f", (b>a) ? b : a}')
  t1=$t2; i1=$i2
  if [ $HAVE_GPU = 1 ]; then
    G="$G
$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null)"
  fi
  k=$((k+1))
done
echo "cpu_percent=$maxc"
awk '/^MemTotal/{t=$2}/^MemAvailable/{a=$2}END{v=(t>0) ? 100*(t-a)/t : 0; printf "ram_percent=%.1f\n", v}' /proc/meminfo
df -P / | awk 'NR==2{gsub("%","",$5); print "disk_percent=" $5}'
if [ $HAVE_GPU = 1 ]; then
  n=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ')
  printf '%s\n' "$G" |
  awk -F', *' -v n="$n" '$1 ~ /^[0-9.]+$/ && $2 ~ /^[0-9.]+$/ && $3 ~ /^[0-9.]+$/ {u=$1+0; if(u>mu)mu=u; mem=($3>0)?100*$2/$3:0; if(mem>mm)mm=mem; r++}
    END{if(r>0 && n>0) printf "gpu_count=%d\ngpu_util_percent=%.1f\ngpu_mem_percent=%.1f\n", n, mu, mm}'
fi
"""


def build_probe(seconds: int = 1) -> str:
    return _PROBE_TMPL.replace("__N__", str(max(1, int(seconds))))


PROBE = build_probe(1)


def _brev_exec(inst: Instance, timeout: int) -> subprocess.CompletedProcess:
    cmd = ["brev", "exec", "--no-check-latest"]
    if inst.org:
        cmd += ["-o", inst.org]
    return run(cmd + [inst.name, PROBE], timeout)


def _direct_ssh(inst: Instance, key: str, timeout: int) -> subprocess.CompletedProcess:
    host, port, user = inst.ssh_target  # type: ignore[misc]
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={min(timeout, 20)}", "-i", str(Path(key).expanduser()),
        "-p", str(port), f"{user}@{host}", "sh -s",
    ]
    return subprocess.run(cmd, input=PROBE, capture_output=True, text=True, timeout=timeout)


def collect(inst: Instance, timeout: int, key: str | None = None) -> dict[str, float]:
    """Direct SSH (key from config) when target known, else/then `brev exec`."""
    attempts: list[Callable[[], subprocess.CompletedProcess]] = []
    if key and inst.ssh_target:
        attempts.append(lambda: _direct_ssh(inst, key, timeout))
    attempts.append(lambda: _brev_exec(inst, timeout))
    errs: list[str] = []
    p = None
    for attempt in attempts:
        try:
            p = attempt()
        except subprocess.TimeoutExpired:
            errs.append(f"timed out after {timeout}s")
            continue
        if p.returncode == 0 and "cpu_percent=" in p.stdout:
            break
        errs.append((p.stderr.strip() or p.stdout.strip() or f"exit {p.returncode}").splitlines()[-1][:160])
        p = None
    if p is None:
        raise RuntimeError("; ".join(errs))
    m: dict[str, float] = {}
    for line in p.stdout.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k in {
            "cpu_percent", "ram_percent", "disk_percent",
            "gpu_count", "gpu_util_percent", "gpu_mem_percent",
        }:
            try:
                m[k] = float(v)
            except ValueError:
                pass
    if "cpu_percent" not in m:
        raise RuntimeError(f"unparseable probe output: {p.stdout[:200]!r}")
    return m


# --------------------------------------------------------------------------
# Idle logic
# --------------------------------------------------------------------------

# idle-config key -> metric key
RULES = {
    "cpu_percent_below": "cpu_percent",
    "ram_percent_below": "ram_percent",
    "gpu_util_percent_below": "gpu_util_percent",
    "gpu_mem_percent_below": "gpu_mem_percent",
}


def is_idle(metrics: dict[str, float], idle_cfg: dict, expects_gpu: bool = False) -> bool:
    """Idle = every configured threshold satisfied. GPU rules skipped on CPU-only."""
    if expects_gpu and "gpu_util_percent" not in metrics:
        return False  # GPU instance but GPU stats failed: cannot confirm idle
    checked = 0
    for rule, key in RULES.items():
        thr = idle_cfg.get(rule)
        if thr is None or key not in metrics:
            continue
        checked += 1
        if metrics[key] >= thr:
            return False
    return checked > 0


@dataclass
class State:
    idle_since: float | None = None
    last_alert: float | None = None


# --------------------------------------------------------------------------
# Alert adapters  (extend: subclass AlertAdapter + @register("type"))
# --------------------------------------------------------------------------


@dataclass
class Alert:
    instance: str
    org: str | None
    idle_minutes: float
    metrics: dict[str, float]

    def text(self) -> str:
        m = ", ".join(f"{k}={v}" for k, v in sorted(self.metrics.items()))
        org = f" (org {self.org})" if self.org else ""
        return (
            f"Brev instance '{self.instance}'{org} idle for "
            f"{self.idle_minutes:.0f} min, still running. Check and close it. [{m}]"
        )


class AlertAdapter:
    kind = "?"

    def __init__(self, name: str, cfg: dict):
        self.name, self.cfg = name, cfg
        self.last_sent: float | None = None
        self.last_error: str | None = None
        self.sent_count = 0

    def target(self) -> str:
        """Short, secret-free description for the console."""
        return ""

    def send(self, alert: Alert) -> None:
        raise NotImplementedError


ADAPTERS: dict[str, type[AlertAdapter]] = {}


def register(kind: str) -> Callable[[type[AlertAdapter]], type[AlertAdapter]]:
    def deco(cls: type[AlertAdapter]) -> type[AlertAdapter]:
        cls.kind = kind
        ADAPTERS[kind] = cls
        return cls
    return deco


@register("log")
class LogAdapter(AlertAdapter):
    def send(self, alert: Alert) -> None:
        log.error("ALARM %s", alert.text())

    def target(self) -> str:
        return "log"


@register("webhook")
class WebhookAdapter(AlertAdapter):
    """POST JSON. `payload_key` (default "text") fits Slack/Mattermost/Teams-style hooks."""

    def send(self, alert: Alert) -> None:
        body = {
            self.cfg.get("payload_key", "text"): alert.text(),
        }
        req = urllib.request.Request(
            self.cfg["url"],
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **self.cfg.get("headers", {})},
        )
        urllib.request.urlopen(req, timeout=15).read()

    def target(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.cfg.get("url", "")).netloc  # host only, never the secret path


@register("command")
class CommandAdapter(AlertAdapter):
    """Run a command; alert JSON on stdin, text in $WATCHDOG_MESSAGE."""

    def send(self, alert: Alert) -> None:
        import os

        env = {**os.environ, "WATCHDOG_MESSAGE": alert.text(), "WATCHDOG_INSTANCE": alert.instance}
        subprocess.run(
            self.cfg["command"], shell=True, env=env, timeout=60, check=True,
            input=json.dumps({"instance": alert.instance, "org": alert.org,
                              "idle_minutes": alert.idle_minutes, "metrics": alert.metrics}),
            text=True,
        )

    def target(self) -> str:
        return str(self.cfg.get("command", "")).split(" ")[0]


def build_adapters(cfg: dict) -> list[AlertAdapter]:
    out = []
    for name, acfg in cfg["alerts"].items():
        if not acfg.get("enabled", True):
            continue
        kind = acfg.get("type")
        if kind not in ADAPTERS:
            raise SystemExit(f"alert '{name}': unknown type {kind!r}; known: {sorted(ADAPTERS)}")
        out.append(ADAPTERS[kind](name, acfg))
    return out




# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


@dataclass
class Row:
    inst: Instance
    metrics: dict[str, float] | None
    error: str | None
    idle: bool = False
    idle_minutes: float = 0.0
    alarmed: bool = False  # alarm raised and instance still idle
    last_alert: float | None = None


@dataclass
class UI:
    """Shared state the Live view renders from."""

    rows: list[Row] = field(default_factory=list)
    polling: bool = True
    last_poll: float | None = None
    next_poll: float | None = None
    list_errors: list[str] = field(default_factory=list)
    balances: dict[str, float | None] = field(default_factory=dict)


def poll(cfg: dict, states: dict[tuple, State], adapters: list[AlertAdapter], dry: bool, ui: UI) -> None:
    to = cfg["exec_timeout_seconds"]
    instances: list[Instance] = []
    errors: list[str] = []
    api = BrevAPI(cfg["api_base"]) if cfg.get("api_base") else None
    ui.balances = {}
    for org in cfg["orgs"] or [None]:
        try:
            org_id = None
            if org:
                org, org_id = resolve_org(org, to)
                if api and org_id:
                    ui.balances[org] = api.balance(org_id)
            instances += list_running(org, to, api, org_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"list failed (org={org}): {e}")
            log.error(errors[-1])
    instances = [i for i in instances if i.name not in cfg["ignore"]]
    log.info("%d running instance(s)", len(instances))

    def probe(i: Instance):
        try:
            return i, collect(i, to, cfg.get("ssh_key")), None
        except Exception as e:  # noqa: BLE001
            return i, None, e

    with ThreadPoolExecutor(max_workers=cfg["max_parallel"]) as ex:
        results = list(ex.map(probe, instances))

    now = time.time()
    seen = set()
    idle_cfg = cfg["idle"]
    rows: list[Row] = []
    for inst, metrics, err in results:
        key = (inst.org, inst.name)
        seen.add(key)
        st = states.setdefault(key, State())
        if err or metrics is None:
            log.warning("%s: metrics unavailable: %s", inst.name, str(err).splitlines()[0] if err else "")
            rows.append(Row(inst, None, str(err).strip().splitlines()[0] if err else "unknown",
                            last_alert=st.last_alert, alarmed=st.last_alert is not None))
            continue  # unknown != idle; keep state untouched
        if inst.expects_gpu and "gpu_util_percent" not in metrics:
            log.warning("%s: GPU instance but no GPU stats (driver down?); not judged idle", inst.name)
        idle = is_idle(metrics, idle_cfg, inst.expects_gpu)
        log.info("%s: %s idle=%s", inst.name, metrics, idle)
        row = Row(inst, metrics, None, idle=idle)
        rows.append(row)
        if dry:
            continue
        if not idle:
            st.idle_since = st.last_alert = None
            continue
        st.idle_since = st.idle_since or now
        row.idle_minutes = (now - st.idle_since) / 60
        due = st.last_alert is None or (now - st.last_alert) / 60 >= idle_cfg["realert_minutes"]
        if row.idle_minutes >= idle_cfg["idle_minutes"] and due:
            alert = Alert(inst.name, inst.org, row.idle_minutes, metrics)
            for a in adapters:
                try:
                    a.send(alert)
                    a.last_sent, a.last_error = now, None
                    a.sent_count += 1
                except Exception as e:  # noqa: BLE001
                    a.last_error = str(e)
                    log.error("adapter %s failed: %s", a.name, e)
            st.last_alert = now
        row.last_alert = st.last_alert
        row.alarmed = st.last_alert is not None
    for key in set(states) - seen:  # instance stopped/deleted
        del states[key]
    ui.rows, ui.list_errors, ui.last_poll = rows, errors, now


# --------------------------------------------------------------------------
# Rich rendering
# --------------------------------------------------------------------------

NA = Text("n/a", style="dim")


def fmt_pct(v: float | None, idle_below: float | None = None, warn_above: float | None = None) -> Text:
    if v is None:
        return NA
    style = "green"
    if idle_below is not None and v < idle_below:
        style = "yellow"  # below idle threshold: contributes to idleness
    if warn_above is not None and v >= warn_above:
        style = "red"
    return Text(f"{v:.0f}%", style=style)


def fmt_dur(seconds: float) -> str:
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def status_cell(row: Row, idle_cfg: dict) -> Text:
    if row.error:
        return Text("NO METRICS", style="bold magenta")
    if row.alarmed:
        return Text("ALARM", style="bold white on red")
    if row.idle:
        return Text(f"idle {row.idle_minutes:.0f}/{idle_cfg['idle_minutes']}m", style="yellow")
    return Text("active", style="green")


def instances_table(ui: UI, idle_cfg: dict) -> Table:
    t = Table(title="Running instances", title_justify="left", expand=True, header_style="bold cyan")
    for col, kw in [
        ("Instance", {}), ("Org", {}), ("Machine", {}), ("Creator", {}), ("Age", {"justify": "right"}),
        ("Est. $", {"justify": "right"}), ("CPU", {"justify": "right"}), ("RAM", {"justify": "right"}),
        ("Disk", {"justify": "right"}), ("GPU", {"justify": "right"}), ("GPU mem", {"justify": "right"}),
        ("Status", {}),
    ]:
        t.add_column(col, **kw)
    if not ui.rows:
        t.add_row(Text("no running instances", style="dim"))
    now = datetime.now(timezone.utc)
    for r in sorted(ui.rows, key=lambda r: (not r.alarmed, r.inst.name)):
        i, m = r.inst, r.metrics or {}
        machine = i.machine + (f" ({i.gpu})" if i.gpu else "")
        up = Text(fmt_dur((now - i.created_at).total_seconds())) if i.created_at else NA
        cred = Text(f"{i.credits_used:.2f}") if i.credits_used is not None else NA
        has_gpu = "gpu_util_percent" in m
        t.add_row(
            Text(i.name, style="bold"), i.org or "(active)", machine,
            Text(i.creator) if i.creator else NA, up, cred,
            fmt_pct(m.get("cpu_percent"), idle_cfg.get("cpu_percent_below")),
            fmt_pct(m.get("ram_percent"), idle_cfg.get("ram_percent_below")),
            fmt_pct(m.get("disk_percent"), warn_above=90),
            fmt_pct(m.get("gpu_util_percent"), idle_cfg.get("gpu_util_percent_below")) if has_gpu else Text("-", style="dim"),
            fmt_pct(m.get("gpu_mem_percent"), idle_cfg.get("gpu_mem_percent_below")) if has_gpu else Text("-", style="dim"),
            status_cell(r, idle_cfg),
        )
    return t


def alarms_panel(ui: UI, idle_cfg: dict) -> Panel:
    alarmed = [r for r in ui.rows if r.alarmed and not r.error]
    blind = [r for r in ui.rows if r.error]
    if not alarmed and not blind:
        return Panel(Text("no alarms", style="green"), title="Alarms", title_align="left", border_style="green")
    t = Table.grid(padding=(0, 2))
    for r in sorted(alarmed, key=lambda r: -r.idle_minutes):
        ago = fmt_dur(time.time() - r.last_alert) + " ago" if r.last_alert else "-"
        t.add_row(Text("IDLE", style="bold red"), Text(r.inst.name, style="bold"),
                  f"idle {fmt_dur(r.idle_minutes * 60)}", f"last alert {ago}", "check and close")
    for r in blind:
        t.add_row(Text("BLIND", style="bold magenta"), Text(r.inst.name, style="bold"),
                  Text(r.error or "", style="dim", overflow="ellipsis", no_wrap=True), "", "metrics unavailable")
    return Panel(t, title=f"Alarms ({len(alarmed)} idle, {len(blind)} unmonitored)",
                 title_align="left", border_style="red" if alarmed else "magenta")


def channels_table(adapters: list[AlertAdapter], cfg: dict) -> Table:
    t = Table(title="Alert channels", title_justify="left", expand=True, header_style="bold cyan")
    for c in ("Name", "Type", "Target", "Sent", "Last", "State"):
        t.add_column(c)
    enabled = {a.name: a for a in adapters}
    for name, acfg in cfg["alerts"].items():
        a = enabled.get(name)
        if a is None:
            t.add_row(name, str(acfg.get("type")), "", "", "", Text("disabled", style="dim"))
            continue
        last = fmt_dur(time.time() - a.last_sent) + " ago" if a.last_sent else "-"
        state = Text(f"error: {a.last_error}", style="red") if a.last_error else Text("ok", style="green")
        t.add_row(name, a.kind, a.target(), str(a.sent_count), last, state)
    return t


def render(ui: UI, cfg: dict, adapters: list[AlertAdapter]) -> Group:
    idle = cfg["idle"]
    rules = " ".join(f"{k.replace('_percent_below', '')}<{v:g}%" for k, v in idle.items() if k.endswith("_below") and v is not None)
    if ui.polling:
        status = Text("polling...", style="bold yellow")
    elif ui.next_poll:
        status = Text(f"next poll in {max(0, int(ui.next_poll - time.time()))}s", style="dim")
    else:
        status = Text("")
    head = Text.assemble(
        ("Brev watchdog  ", "bold"),
        (f"every {cfg['interval_seconds']}s | idle = {rules} for {idle['idle_minutes']}m | ", "dim"),
        status,
    )
    bal = "  ".join(f"{o}: ${b:,.2f}" for o, b in ui.balances.items() if b is not None)
    if bal:
        head.append(f"\ncredit balance  {bal}", style="dim")
    parts = [head, instances_table(ui, idle), alarms_panel(ui, idle), channels_table(adapters, cfg)]
    for e in ui.list_errors:
        parts.append(Text(e, style="red"))
    return Group(*parts)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", type=Path, default=Path("config.toml"))
    ap.add_argument("--once", action="store_true", help="single poll then exit")
    ap.add_argument("--dry-run", action="store_true", help="collect and print, no alarms")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    console = Console()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s", datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    if not shutil.which("brev"):
        log.error("brev CLI not found in PATH")
        return 2
    cfg = load_config(args.config)
    adapters = build_adapters(cfg)
    global PROBE
    PROBE = build_probe(cfg["probe_seconds"])
    if cfg["exec_timeout_seconds"] <= cfg["probe_seconds"] + 10:
        cfg["exec_timeout_seconds"] = cfg["probe_seconds"] + 30  # SSH setup headroom
    states: dict[tuple, State] = {}
    ui = UI()

    def cycle():
        ui.polling = True
        try:
            poll(cfg, states, adapters, args.dry_run, ui)
        except Exception:  # noqa: BLE001
            log.exception("poll failed")
        ui.polling = False
        ui.next_poll = time.time() + cfg["interval_seconds"]

    if args.once or not console.is_terminal:
        cycle()
        console.print(render(ui, cfg, adapters))
        if args.once:
            return 0
        while True:  # non-tty: plain re-print each cycle
            time.sleep(cfg["interval_seconds"])
            cycle()
            console.print(render(ui, cfg, adapters))

    with Live(get_renderable=lambda: render(ui, cfg, adapters), console=console, refresh_per_second=2):
        while True:
            cycle()
            while time.time() < (ui.next_poll or 0):
                time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
