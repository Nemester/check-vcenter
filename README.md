# check_vcenter.py

Icinga/Nagios plugin for VMware vCenter monitoring via [pyVmomi](https://github.com/vmware/pyvmomi).

Developed and tested against **vSphere 8.x**. Most checks are compatible with vSphere 6.5+ but the vMotion check has vSphere 8-specific logic (see [Notes on vMotion detection](#notes-on-vmotion-detection)).

---

## Requirements

- Python 3.6+
- pyVmomi

```bash
pip install pyVmomi
```

---

## Installation

```bash
cp check_vcenter.py /usr/lib/nagios/plugins/
chmod +x /usr/lib/nagios/plugins/check_vcenter.py
```

---

## Usage

```
check_vcenter.py -H <vcenter> -u <user> -p <password> --check <check> [options]
```

### Global options

| Option | Default | Description |
|---|---|---|
| `-H`, `--host` | — | vCenter hostname or IP (**required**) |
| `-u`, `--user` | — | vCenter username (**required**) |
| `-p`, `--password` | — | vCenter password (**required**) |
| `--port` | `443` | HTTPS port |
| `--no-ssl-verify` | off | Disable SSL certificate verification |
| `--check` | — | Check to run (see below) (**required**) |
| `--cluster` | all | Limit check to a named cluster |
| `-w`, `--warning` | varies | Warning threshold (count) |
| `-c`, `--critical` | varies | Critical threshold (count) |
| `-V`, `--version` | — | Print version and exit |

---

## Checks

### `vmotion` — vMotion count

Counts VM migrations in a rolling time window. Alerts if the number of migrations exceeds the warning or critical threshold, which is useful for detecting unexpected DRS storms or runaway automation.

**vSphere 8 note:** migrations are recorded in two separate systems depending on how they were triggered. This check queries both and combines the results — see [Notes on vMotion detection](#notes-on-vmotion-detection).

| Option | Default | Description |
|---|---|---|
| `--window` | `1.0` | Look-back window in hours |
| `-w` | `30` | Warning threshold (total migrations) |
| `-c` | `50` | Critical threshold (total migrations) |

```bash
# All clusters, last hour
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check vmotion --window 1 -w 10 -c 50

# Specific cluster
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check vmotion --cluster "Cluster-Prod" --window 1 -w 10 -c 50
```

**Output:**
```
[OK] - VMOTION: 6 vMotion(s) in the last 1.0h on cluster 'Cluster-Prod' [VirtualMachine.relocate:2, vim.event.DrsVmMigratedEvent:3, com.vmware.vc.vm.VmHotMigratingWithEncryptionEvent:1]
```

**Perfdata:**

| Metric | Description |
|---|---|
| `vmotion_count` | Total migrations (all types combined) |
| `vmotion_manual` | Manually triggered migrations (task system) |
| `vmotion_drs` | DRS-triggered migrations |
| `vmotion_encrypted` | Encrypted VM migrations (vSphere 8 EventEx) |
| `vmotion_classic` | Classic unencrypted vMotion events |

---

### `snapshot` — old snapshots

Finds VM snapshots older than a given age. Useful for catching forgotten snapshots that grow and consume datastore space over time.

| Option | Default | Description |
|---|---|---|
| `--max-age` | `24.0` | Maximum snapshot age in hours |
| `-w` | `1` | Warning threshold (number of old snapshots) |
| `-c` | `5` | Critical threshold (number of old snapshots) |

```bash
# Alert on any snapshot older than 48h
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check snapshot --max-age 48 -w 1 -c 5

# Scoped to a cluster
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check snapshot --cluster "Cluster-Dev" --max-age 24
```

**Output:**
```
[WARNING] - SNAPSHOT: 2 snapshot(s) on 2 VM(s) older than 24h on cluster 'Cluster-Dev': vm1[Before patch](26.3h), vm2[pre-upgrade](31.1h)
```

**Perfdata:**

| Metric | Description |
|---|---|
| `old_snapshots` | Number of snapshots exceeding `--max-age` |

---

### `host_alarms` — ESXi host alarms

Reports triggered vCenter alarms on ESXi hosts. These are explicitly configured alarms (e.g. CPU usage threshold, datastore connectivity) that appear in the vCenter Alarms view.

Use `--alarm-filter` to scope a dedicated Icinga service check to a single alarm category rather than catching everything in one noisy check.

| Option | Default | Description |
|---|---|---|
| `--alarm-filter` | none | Only report alarms whose name contains this string (case-insensitive) |
| `-w` | `1` | Warning threshold (number of yellow alarms) |
| `-c` | `1` | Critical threshold (number of red alarms) |

```bash
# All triggered alarms on all hosts
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check host_alarms

# Only CPU-related alarms on a specific cluster
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check host_alarms --cluster "Cluster-Prod" --alarm-filter "cpu" -w 1 -c 3
```

**Output:**
```
[CRITICAL] - HOST_ALARMS: 1 alarm(s) on cluster 'Cluster-Prod' — RED: esxi01.example.com[Host CPU usage]
```

**Perfdata:**

| Metric | Description |
|---|---|
| `alarms_total` | Total triggered alarms |
| `alarms_critical` | Alarms in RED state |
| `alarms_warning` | Alarms in YELLOW state |

---

### `host_issues` — ESXi host configuration issues

Reports built-in vSphere configuration warnings shown as yellow icons on ESXi hosts in the vCenter UI. These are **not** alarms — they are health notices generated by vSphere itself and stored in `host.configIssue`. Common examples:

- SSH service is enabled
- ESXi Shell service is enabled
- NTP client not configured or not running
- Scratch partition not configured
- Host not connected to a domain

| Option | Default | Description |
|---|---|---|
| `--issue-filter` | none | Only report issues whose message contains this string (case-insensitive) |
| `-w` | `1` | Warning threshold |
| `-c` | `1` | Critical threshold (issues on hosts in RED overall state) |

```bash
# All config issues on all hosts
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check host_issues

# SSH warnings only
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check host_issues --cluster "Cluster-Prod" --issue-filter "ssh"

# NTP issues only
check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
    --check host_issues --issue-filter "ntp"
```

**Output:**
```
[WARNING] - HOST_ISSUES: 2 issue(s) on cluster 'Cluster-Prod' (filter: 'ssh') — YELLOW: esxi01.example.com: SSH service is enabled on the host, esxi02.example.com: SSH service is enabled on the host
```

**Perfdata:**

| Metric | Description |
|---|---|
| `issues_total` | Total configuration issues |
| `issues_red` | Issues on hosts in RED overall state |
| `issues_yellow` | Issues on hosts in YELLOW overall state |

---

## Icinga2 integration

### Command definition

```
object CheckCommand "check_vcenter" {
  command = [ "/usr/lib/nagios/plugins/check_vcenter.py" ]
  arguments = {
    "-H"               = "$vcenter_host$"
    "-u"               = "$vcenter_user$"
    "-p"               = "$vcenter_password$"
    "--check"          = "$vcenter_check$"
    "--cluster"        = { value = "$vcenter_cluster$"; skip_key = false; set_if = "$vcenter_cluster$" }
    "--warning"        = "$vcenter_warning$"
    "--critical"       = "$vcenter_critical$"
    "--window"         = "$vcenter_window$"
    "--max-age"        = "$vcenter_max_age$"
    "--alarm-filter"   = "$vcenter_alarm_filter$"
    "--issue-filter"   = "$vcenter_issue_filter$"
    "--no-ssl-verify"  = { set_if = "$vcenter_no_ssl_verify$" }
  }
  vars.vcenter_no_ssl_verify = false
}
```

### Example service definitions

```
apply Service "vcenter-vmotions-prod" {
  check_command = "check_vcenter"
  vars.vcenter_host    = "vcenter.example.com"
  vars.vcenter_user    = "monitor@vsphere.local"
  vars.vcenter_password = "secret"
  vars.vcenter_check   = "vmotion"
  vars.vcenter_cluster = "Cluster-Prod"
  vars.vcenter_window  = "1"
  vars.vcenter_warning = "10"
  vars.vcenter_critical = "50"
  assign where host.name == "vcenter.example.com"
}

apply Service "vcenter-snapshots" {
  check_command = "check_vcenter"
  vars.vcenter_host     = "vcenter.example.com"
  vars.vcenter_user     = "monitor@vsphere.local"
  vars.vcenter_password = "secret"
  vars.vcenter_check    = "snapshot"
  vars.vcenter_max_age  = "48"
  vars.vcenter_warning  = "1"
  vars.vcenter_critical = "5"
  assign where host.name == "vcenter.example.com"
}

apply Service "vcenter-host-ssh" {
  check_command = "check_vcenter"
  vars.vcenter_host         = "vcenter.example.com"
  vars.vcenter_user         = "monitor@vsphere.local"
  vars.vcenter_password     = "secret"
  vars.vcenter_check        = "host_issues"
  vars.vcenter_issue_filter = "ssh"
  assign where host.name == "vcenter.example.com"
}
```

---

## vCenter permissions

The monitoring account needs read-only access at minimum. The following privileges are used:

| Privilege | Required by |
|---|---|
| Read-only role on root or cluster | All checks |
| Global > Browse Diagnostics | `vmotion` (event/task history) |
| Sessions > Validate Session | connection |

A dedicated read-only service account scoped to the relevant clusters is recommended over using an administrator account.

---

## Notes on vMotion detection

In vSphere 8 the migration event model differs significantly from earlier versions:

| Migration type | Recorded as |
|---|---|
| Manual compute+storage (encrypted VM) | `EventEx`: `com.vmware.vc.vm.VmHotMigratingWithEncryptionEvent` |
| Manual compute+storage (unencrypted) | Task: `VirtualMachine.relocate` |
| DRS-triggered compute migration | Event: `vim.event.DrsVmMigratedEvent` |
| Classic compute-only migration | Event: `vim.event.VmMigratedEvent` |

Because VMware's `QueryEvents` API is hard-capped at 1000 results, this plugin uses `EventHistoryCollector` to page through all events without truncation. In very busy environments with many login/logout events, the 1000-event cap would otherwise silently hide all migration events.

---

## Changelog

| Version | Changes |
|---|---|
| 1.0 | Initial implementation (snapshot, host_alarms, host_issues) |
| 1.1 | Added vMotion check with vSphere 8 support |
| 1.2 | Added `--version` / `-V` parameter, minor refactoring |
