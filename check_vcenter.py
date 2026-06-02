#!/usr/bin/env python3
"""
check_vcenter.py - Icinga/Nagios plugin for VMware vCenter monitoring via pyVmomi

Supported checks:
  vmotion   - Count vMotion events within a time window
  snapshot  - Detect VM snapshots older than X hours

Usage examples:
  check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
      --check vmotion --window 1 --warning 10 --critical 50

  check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
      --check vmotion --cluster "Cluster-Prod" --window 1 --warning 10 --critical 50

  check_vcenter.py -H vcenter.example.com -u monitor@vsphere.local -p secret \
      --check snapshot --cluster "Cluster-Dev" --max-age 24 --warning 1 --critical 5

Requirements:
  pip install pyVmomi

Changelog:
  1.0 - initial implementation
  1.1 - added functionality for vMotion check
  1.2 - added --version parameter and minor refactoring
"""

import argparse
import ssl
import sys
from datetime import datetime, timezone, timedelta

try:
    from pyVmomi import vim
    from pyVim.connect import SmartConnect, Disconnect
except ImportError:
    print("[UNKNOWN]: pyVmomi is not installed. Run: pip install pyVmomi")
    sys.exit(3)


VERSION = "1.2"

# ---------------------------------------------------------------------------
# Icinga/Nagios exit codes
# ---------------------------------------------------------------------------
OK       = 0
WARNING  = 1
CRITICAL = 2
UNKNOWN  = 3


def exit_plugin(code: int, check: str, message: str, perfdata: str = "") -> None:
    """Print a proper Icinga plugin output line and exit."""
    labels = {OK: "[OK]", WARNING: "[WARNING]", CRITICAL: "[CRITICAL]", UNKNOWN: "[UNKNOWN]"}
    label = labels.get(code, "[UNKNOWN]")
    perf = f" | {perfdata}" if perfdata else ""
    print(f"{label} - {check.upper()}: {message}{perf}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# vCenter connection helper
# ---------------------------------------------------------------------------
def connect(host: str, user: str, password: str, port: int, no_ssl_verify: bool):
    """Return a ServiceInstance, bypassing SSL verification if requested."""
    ssl_context = None
    if no_ssl_verify:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        si = SmartConnect(
            host=host,
            user=user,
            pwd=password,
            port=port,
            sslContext=ssl_context,
        )
    except Exception as exc:
        print(f"[UNKNOWN] - Could not connect to vCenter {host}: {exc}")
        sys.exit(UNKNOWN)
    return si


# ---------------------------------------------------------------------------
# Helper: find a cluster object by name
# ---------------------------------------------------------------------------
def find_cluster(si, cluster_name: str):
    """
    Search the entire inventory for a ClusterComputeResource with the given name.
    Returns the managed object, or exits UNKNOWN if not found.
    Lists available clusters in the error message.
    """
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.ClusterComputeResource], True
    )
    clusters = list(container.view)
    container.Destroy()

    for cluster in clusters:
        if cluster.name == cluster_name:
            return cluster

    available = ", ".join(c.name for c in clusters) or "none found"
    print(f"[UNKNOWN] - Cluster '{cluster_name}' not found. "
          f"Available clusters: {available}")
    sys.exit(UNKNOWN)


# ---------------------------------------------------------------------------
# Helper: collect VMs — optionally scoped to a cluster
# ---------------------------------------------------------------------------
def get_vms(si, cluster_name: str = None):
    """
    Return a flat list of VirtualMachine managed objects.
    If cluster_name is given, only VMs belonging to that cluster are returned.
    """
    content = si.RetrieveContent()

    if cluster_name:
        cluster = find_cluster(si, cluster_name)
        root = cluster
    else:
        root = content.rootFolder

    container = content.viewManager.CreateContainerView(
        root, [vim.VirtualMachine], True
    )
    vms = list(container.view)
    container.Destroy()
    return vms


# ---------------------------------------------------------------------------
# Helper: build a set of VM MOR IDs belonging to a cluster (for event filtering)
# ---------------------------------------------------------------------------
def get_cluster_vm_refs(si, cluster_name: str):
    """
    Returns a set of VM managed object reference IDs for all VMs in the cluster.
    Used to filter vCenter-wide events down to a specific cluster.
    """
    vms = get_vms(si, cluster_name)
    return {vm._moId for vm in vms}


# ---------------------------------------------------------------------------
# Check: vMotion count
# ---------------------------------------------------------------------------
def collect_all_events(event_mgr, filter_spec):
    """
    Page through all events using EventHistoryCollector.
    QueryEvents is hard-capped at 1000 results — in busy environments this
    silently truncates the result set. The collector has no such limit.
    """
    try:
        collector = event_mgr.CreateCollectorForEvents(filter_spec)
        collector.SetCollectorPageSize(1000)
        all_events = []
        while True:
            batch = collector.ReadNextEvents(1000)
            if not batch:
                break
            all_events.extend(batch)
        collector.DestroyCollector()
        return all_events
    except Exception as exc:
        exit_plugin(UNKNOWN, "vmotion", f"Failed to collect events: {exc}")


def collect_migration_tasks(content, start, end, cluster_vm_refs=None):
    """
    Query the task manager for manually triggered migrations.

    In vSphere 8 a manual compute+storage migration (relocate) is recorded
    ONLY as a task (VirtualMachine.relocate / VirtualMachine.migrate) and
    does NOT produce a migration event. DRS migrations still go to the event
    system. We therefore query both and combine the counts.

    Task descriptionIds for migrations:
      VirtualMachine.relocate  - manual combined compute+storage vMotion
      VirtualMachine.migrate   - manual compute-only vMotion (older behavior)
    """
    MIGRATION_TASK_IDS = {"VirtualMachine.relocate", "VirtualMachine.migrate"}

    task_filter = vim.TaskFilterSpec(
        time=vim.TaskFilterSpec.ByTime(
            timeType=vim.TaskFilterSpec.TimeOption.startedTime,
            beginTime=start,
            endTime=end,
        ),
        state=[vim.TaskInfo.State.success, vim.TaskInfo.State.running],
    )

    try:
        collector = content.taskManager.CreateCollectorForTasks(task_filter)
        collector.SetCollectorPageSize(1000)
        all_tasks = []
        while True:
            batch = collector.ReadNextTasks(1000)
            if not batch:
                break
            all_tasks.extend(batch)
        collector.DestroyCollector()
    except Exception as exc:
        exit_plugin(UNKNOWN, "vmotion", f"Failed to collect tasks: {exc}")

    result = []
    for t in all_tasks:
        if t.descriptionId not in MIGRATION_TASK_IDS:
            continue
        if cluster_vm_refs is not None:
            if not hasattr(t, "entity") or t.entity._moId not in cluster_vm_refs:
                continue
        result.append(t)
    return result


def check_vmotion(si, args):
    """
    Count vMotion migrations in the last --window hours.
    If --cluster is given, only migrations for VMs in that cluster are counted.
    Raises WARNING/CRITICAL if the count exceeds thresholds.

    vSphere 8 records migrations in two separate systems:
      - DRS-triggered migrations  --> event system (DrsVmMigratedEvent)
      - Manual migrations         --> task system  (VirtualMachine.relocate)
      - Encrypted VM migrations   --> EventEx      (VmHotMigratingWithEncryptionEvent)
    All three sources are queried and combined into a single count.
    """
    window_hours = args.window
    warn  = args.warning
    crit  = args.critical
    cluster_name = args.cluster

    now   = datetime.now(tz=timezone.utc)
    start = now - timedelta(hours=window_hours)

    vcontent  = si.RetrieveContent()
    event_mgr = vcontent.eventManager

    cluster_vm_refs = get_cluster_vm_refs(si, cluster_name) if cluster_name else None

    # --- Source 1: event system (DRS + encrypted vMotions) ---
    time_filter = vim.event.EventFilterSpec.ByTime(beginTime=start, endTime=now)
    filter_spec = vim.event.EventFilterSpec(time=time_filter)
    all_events  = collect_all_events(event_mgr, filter_spec)

    CLASSIC_VMOTION_TYPES = (
        vim.event.VmMigratedEvent,
        vim.event.VmRelocatedEvent,
        vim.event.DrsVmMigratedEvent,
    )
    EVENTEX_VMOTION_IDS = {
        "com.vmware.vc.vm.VmHotMigratingWithEncryptionEvent",
    }

    def is_vmotion_event(e):
        if isinstance(e, vim.event.EventEx):
            return getattr(e, "eventTypeId", "") in EVENTEX_VMOTION_IDS
        return isinstance(e, CLASSIC_VMOTION_TYPES)

    vmotion_events = [e for e in all_events if is_vmotion_event(e)]
    if cluster_vm_refs is not None:
        vmotion_events = [
            e for e in vmotion_events
            if hasattr(e, "vm") and e.vm.vm._moId in cluster_vm_refs
        ]

    # --- Source 2: task system (manual relocate/migrate in vSphere 8) ---
    manual_tasks = collect_migration_tasks(vcontent, start, now, cluster_vm_refs)

    # --- Combine and build breakdown ---
    type_counts = {}
    for e in vmotion_events:
        tid = getattr(e, "eventTypeId", None) or type(e).__name__
        type_counts[tid] = type_counts.get(tid, 0) + 1
    for t in manual_tasks:
        type_counts[t.descriptionId] = type_counts.get(t.descriptionId, 0) + 1

    count = len(vmotion_events) + len(manual_tasks)
    scope_label = f"cluster '{cluster_name}'" if cluster_name else "all clusters"
    perfdata = (
        f"vmotion_count={count};{warn};{crit};0 "
        f"vmotion_manual={type_counts.get('VirtualMachine.relocate', 0) + type_counts.get('VirtualMachine.migrate', 0)};; "
        f"vmotion_drs={type_counts.get('vim.event.DrsVmMigratedEvent', 0)};; "
        f"vmotion_encrypted={type_counts.get('com.vmware.vc.vm.VmHotMigratingWithEncryptionEvent', 0)};; "
        f"vmotion_classic={type_counts.get('vim.event.VmMigratedEvent', 0) + type_counts.get('vim.event.VmRelocatedEvent', 0)};;"
    )

    breakdown = ", ".join(f"{k}:{v}" for k, v in type_counts.items()) or "none"
    msg = (f"{count} vMotion(s) in the last {window_hours}h on {scope_label} "
           f"[{breakdown}]")

    if crit is not None and count >= crit:
        exit_plugin(CRITICAL, "vmotion", msg, perfdata)
    if warn is not None and count >= warn:
        exit_plugin(WARNING, "vmotion", msg, perfdata)
    exit_plugin(OK, "vmotion", msg, perfdata)


# ---------------------------------------------------------------------------
# Check: old snapshots
# ---------------------------------------------------------------------------
def check_snapshot(si, args):
    """
    Find VM snapshots older than --max-age hours.
    If --cluster is given, only VMs in that cluster are checked.
    Reports WARNING/CRITICAL based on how many snapshots are too old.
    """
    max_age_hours = args.max_age
    warn = args.warning
    crit = args.critical
    cluster_name = args.cluster

    now       = datetime.now(tz=timezone.utc)
    threshold = now - timedelta(hours=max_age_hours)

    vms = get_vms(si, cluster_name)
    old_snapshots = []  # list of (vm_name, snapshot_name, age_hours)

    def walk_snapshots(snapshot_list, vm_name):
        for snap in snapshot_list:
            create_time = snap.createTime
            if create_time.tzinfo is None:
                create_time = create_time.replace(tzinfo=timezone.utc)

            if create_time < threshold:
                age_h = (now - create_time).total_seconds() / 3600
                old_snapshots.append((vm_name, snap.name, round(age_h, 1)))

            if snap.childSnapshotList:
                walk_snapshots(snap.childSnapshotList, vm_name)

    for vm in vms:
        if vm.snapshot and vm.snapshot.rootSnapshotList:
            walk_snapshots(vm.snapshot.rootSnapshotList, vm.name)

    count     = len(old_snapshots)
    vm_count  = len({s[0] for s in old_snapshots})
    scope_label = f"cluster '{cluster_name}'" if cluster_name else "all clusters"
    perfdata  = f"old_snapshots={count};{warn};{crit};0"

    if count == 0:
        msg = f"No snapshots older than {max_age_hours}h found on {scope_label}"
        exit_plugin(OK, "snapshot", msg, perfdata)

    details = ", ".join(
        f"{vm}[{snap}]({age}h)" for vm, snap, age in old_snapshots[:10]
    )
    if count > 10:
        details += f" ... and {count - 10} more"

    msg = (f"{count} snapshot(s) on {vm_count} VM(s) older than {max_age_hours}h "
           f"on {scope_label}: {details}")

    if crit is not None and count >= crit:
        exit_plugin(CRITICAL, "snapshot", msg, perfdata)
    if warn is not None and count >= warn:
        exit_plugin(WARNING, "snapshot", msg, perfdata)
    exit_plugin(OK, "snapshot", msg, perfdata)


# ---------------------------------------------------------------------------
# Check: ESXi host alarms
# ---------------------------------------------------------------------------
def check_host_alarms(si, args):
    """
    Report triggered alarms on ESXi hosts.
    If --cluster is given, only hosts in that cluster are checked.
    If --alarm-filter is given, only alarms whose name contains that string
    are reported (case-insensitive), e.g. 'ssh' or 'cpu'.

    By default any alarm in RED (critical) state raises CRITICAL,
    and any alarm in YELLOW (warning) state raises WARNING.
    --warning / --critical thresholds control how many alarms trigger each
    state (default: any single alarm is enough).
    """
    cluster_name  = args.cluster
    alarm_filter  = getattr(args, "alarm_filter", None)
    warn          = args.warning
    crit          = args.critical

    vcontent = si.RetrieveContent()

    if cluster_name:
        cluster = find_cluster(si, cluster_name)
        container = vcontent.viewManager.CreateContainerView(
            cluster, [vim.HostSystem], True
        )
    else:
        container = vcontent.viewManager.CreateContainerView(
            vcontent.rootFolder, [vim.HostSystem], True
        )
    hosts = list(container.view)
    container.Destroy()

    RED    = vim.ManagedEntity.Status.red
    YELLOW = vim.ManagedEntity.Status.yellow

    triggered_red    = []
    triggered_yellow = []

    for host in hosts:
        if not host.triggeredAlarmState:
            continue
        for alarm_state in host.triggeredAlarmState:
            try:
                alarm_name = alarm_state.alarm.info.name
            except Exception:
                alarm_name = "unknown"

            if alarm_filter and alarm_filter.lower() not in alarm_name.lower():
                continue

            if alarm_state.overallStatus == RED:
                triggered_red.append((host.name, alarm_name))
            elif alarm_state.overallStatus == YELLOW:
                triggered_yellow.append((host.name, alarm_name))

    total_red    = len(triggered_red)
    total_yellow = len(triggered_yellow)
    total        = total_red + total_yellow
    scope_label  = f"cluster '{cluster_name}'" if cluster_name else "all clusters"
    filter_label = f" (filter: '{alarm_filter}')" if alarm_filter else ""
    perfdata     = (
        f"alarms_total={total};{warn};{crit};0 "
        f"alarms_critical={total_red};; "
        f"alarms_warning={total_yellow};;"
    )

    def fmt(items, limit=5):
        out = ", ".join(f"{h}[{a}]" for h, a in items[:limit])
        if len(items) > limit:
            out += f" ... and {len(items) - limit} more"
        return out

    if total == 0:
        msg = f"No triggered host alarms on {scope_label}{filter_label}"
        exit_plugin(OK, "host_alarms", msg, perfdata)

    parts = []
    if triggered_red:
        parts.append(f"RED: {fmt(triggered_red)}")
    if triggered_yellow:
        parts.append(f"YELLOW: {fmt(triggered_yellow)}")
    msg = f"{total} alarm(s) on {scope_label}{filter_label} — " + "; ".join(parts)

    if crit is not None and total_red >= crit:
        exit_plugin(CRITICAL, "host_alarms", msg, perfdata)
    if warn is not None and total_yellow >= warn:
        exit_plugin(WARNING, "host_alarms", msg, perfdata)
    if total_red > 0:
        exit_plugin(WARNING, "host_alarms", msg, perfdata)
    exit_plugin(OK, "host_alarms", msg, perfdata)


# ---------------------------------------------------------------------------
# Check: ESXi host configuration issues (the yellow warning icon in vCenter)
# ---------------------------------------------------------------------------
def check_host_issues(si, args):
    """
    Report configuration issues on ESXi hosts — these are the built-in vSphere
    health warnings shown as yellow icons in the UI, e.g.:
      - SSH service is enabled
      - Shell service is enabled
      - NTP not configured
      - Host not in a domain
      - Scratch partition not configured

    These are NOT alarms — they live in host.configIssue and host.overallStatus
    and are separate from the triggered alarm system.

    --issue-filter limits results to issues whose message contains a string,
    e.g. 'ssh' or 'ntp'.
    """
    cluster_name  = args.cluster
    issue_filter  = getattr(args, "issue_filter", None)
    warn          = args.warning
    crit          = args.critical

    vcontent = si.RetrieveContent()

    if cluster_name:
        cluster = find_cluster(si, cluster_name)
        container = vcontent.viewManager.CreateContainerView(
            cluster, [vim.HostSystem], True
        )
    else:
        container = vcontent.viewManager.CreateContainerView(
            vcontent.rootFolder, [vim.HostSystem], True
        )
    hosts = list(container.view)
    container.Destroy()

    issues_red    = []
    issues_yellow = []

    for host in hosts:
        for issue in (host.configIssue or []):
            msg_text = getattr(issue, "fullFormattedMessage", None) \
                    or getattr(issue, "message", "unknown issue")

            if issue_filter and issue_filter.lower() not in msg_text.lower():
                continue

            status = getattr(host, "overallStatus", None)
            if status == vim.ManagedEntity.Status.red:
                issues_red.append((host.name, msg_text))
            else:
                issues_yellow.append((host.name, msg_text))

    total_red    = len(issues_red)
    total_yellow = len(issues_yellow)
    total        = total_red + total_yellow
    scope_label  = f"cluster '{cluster_name}'" if cluster_name else "all clusters"
    filter_label = f" (filter: '{issue_filter}')" if issue_filter else ""
    perfdata     = (
        f"issues_total={total};{warn};{crit};0 "
        f"issues_red={total_red};; "
        f"issues_yellow={total_yellow};;"
    )

    def fmt(items, limit=5):
        out = ", ".join(f"{h}: {m}" for h, m in items[:limit])
        if len(items) > limit:
            out += f" ... and {len(items) - limit} more"
        return out

    if total == 0:
        msg = f"No configuration issues on {scope_label}{filter_label}"
        exit_plugin(OK, "host_issues", msg, perfdata)

    parts = []
    if issues_red:
        parts.append(f"RED: {fmt(issues_red)}")
    if issues_yellow:
        parts.append(f"YELLOW: {fmt(issues_yellow)}")
    msg = f"{total} issue(s) on {scope_label}{filter_label} — " + "; ".join(parts)

    if crit is not None and total_red >= crit:
        exit_plugin(CRITICAL, "host_issues", msg, perfdata)
    if warn is not None and total >= warn:
        exit_plugin(WARNING, "host_issues", msg, perfdata)
    exit_plugin(OK, "host_issues", msg, perfdata)


# ---------------------------------------------------------------------------
# Check registry — add new checks here
# ---------------------------------------------------------------------------
CHECKS = {
    "vmotion":     check_vmotion,
    "snapshot":    check_snapshot,
    "host_alarms": check_host_alarms,
    "host_issues": check_host_issues,
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Icinga/Nagios plugin for VMware vCenter (pyVmomi)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {VERSION}",
        help="Show version and exit",
    )

    # Connection
    conn = parser.add_argument_group("Connection")
    conn.add_argument("-H", "--host",     required=False, default=None, help="vCenter hostname or IP")
    conn.add_argument("-u", "--user",     required=False, default=None, help="vCenter username")
    conn.add_argument("-p", "--password", required=False, default=None, help="vCenter password")
    conn.add_argument("--port",           type=int, default=443, help="HTTPS port (default: 443)")
    conn.add_argument("--no-ssl-verify",  action="store_true",
                      help="Disable SSL certificate verification")

    # Check selector
    parser.add_argument(
        "--check",
        required=False,
        default=None,
        choices=list(CHECKS.keys()),
        metavar="CHECK",
        help=f"Check to run: {', '.join(CHECKS.keys())}",
    )

    # Scope
    scope = parser.add_argument_group("Scope")
    scope.add_argument(
        "--cluster",
        default=None,
        metavar="CLUSTER_NAME",
        help="Limit check to a specific cluster (default: all clusters). "
             "Use the exact cluster name as shown in vCenter.",
    )

    # Thresholds
    thresh = parser.add_argument_group("Thresholds")
    thresh.add_argument("-w", "--warning",  type=int, default=None,
                        help="Warning threshold (count)")
    thresh.add_argument("-c", "--critical", type=int, default=None,
                        help="Critical threshold (count)")

    # Check-specific options
    specific = parser.add_argument_group("Check-specific options")
    specific.add_argument(
        "--window", type=float, default=1.0,
        help="[vmotion] Time window in hours to look back (default: 1)",
    )
    specific.add_argument(
        "--max-age", type=float, default=24.0, dest="max_age",
        help="[snapshot] Maximum snapshot age in hours before alerting (default: 24)",
    )
    specific.add_argument(
        "--alarm-filter", default=None, dest="alarm_filter",
        metavar="STRING",
        help="[host_alarms] Only report alarms whose name contains STRING "
             "(case-insensitive), e.g. 'ssh' or 'cpu'",
    )
    specific.add_argument(
        "--issue-filter", default=None, dest="issue_filter",
        metavar="STRING",
        help="[host_issues] Only report config issues whose message contains STRING "
             "(case-insensitive), e.g. 'ssh' or 'ntp'",
    )

    args = parser.parse_args()

    # --check is required when not just printing version
    if args.check is None:
        parser.error("Missing argument, the following arguments are required: --check")
    if args.host is None:
        parser.error("Missing argument, the following arguments are required: -H/--host")
    if args.user is None:
        parser.error("Missing argument, the following arguments are required: -u/--user")
    if args.password is None:
        parser.error("Missing argument, the following arguments are required: -p/--password")

    # Sensible threshold defaults per check type
    if args.check == "vmotion":
        if args.warning  is None: args.warning  = 30
        if args.critical is None: args.critical = 50
    elif args.check == "snapshot":
        if args.warning  is None: args.warning  = 1
        if args.critical is None: args.critical = 5
    elif args.check == "host_alarms":
        if args.warning  is None: args.warning  = 1
        if args.critical is None: args.critical = 1
    elif args.check == "host_issues":
        if args.warning  is None: args.warning  = 1
        if args.critical is None: args.critical = 1

    return args


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    si = connect(args.host, args.user, args.password, args.port, args.no_ssl_verify)
    try:
        check_fn = CHECKS[args.check]
        check_fn(si, args)
    finally:
        Disconnect(si)


if __name__ == "__main__":
    main()
