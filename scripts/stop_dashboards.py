"""Stop every running dashboard, so a new one cannot be shadowed by an old one.

Called by `Start MOP Reco.bat` before it launches. This exists as Python rather
than as inline PowerShell inside the batch file because the batch version's
nested quoting silently did nothing: it ran, printed nothing, killed nothing,
and a process started two days earlier went on serving stale modules on port
8501. Batch escaping is the fragile part; Python is already a requirement.

Why it matters at all: Streamlit re-runs page scripts on every interaction but
keeps imported modules in `sys.modules`, so a process started before a code
change serves the NEW page script against the OLD module — a raw AttributeError
partway down a page, or a change that simply is not there. See CLAUDE.md §9a.

Run directly to see what is running without stopping anything:
    python scripts/stop_dashboards.py --list
"""

from __future__ import annotations

import os
import subprocess
import sys

POWERSHELL = "powershell"

# The app's fixed entrypoint (CLAUDE.md §6.14: "app/Summary.py is the
# Streamlit entrypoint"). Matching on this rather than on the words
# "streamlit"/"run" is deliberate — see the two bugs below.
ENTRYPOINT = "Summary.py"

# One WMI query, one line per process: "<pid>|<started>|<commandline>".
#
# TWO bugs found here on 2026-09-11, both about what counts as "a dashboard
# process":
#
# 1. NOT filtered by Name any more. A `streamlit run app/Summary.py` launch
#    produces TWO processes, and neither satisfied the old
#    `Name like '%python%' + CommandLine like '*streamlit run*'` filter at
#    once — streamlit.exe's Name has no "python" in it, and the python.exe
#    child's actual command line reads "...streamlit-script.py run ..."
#    (no literal "streamlit run" substring). The filter matched NEITHER
#    process and silently found nothing, while a stale dashboard went on
#    serving old modules.
#
# 2. Whatever literal substring the filter searches for will appear inside
#    the query's OWN command line too, because the query is itself launched
#    as `powershell -Command "<this text>"` and the pattern text is part of
#    that text — first tried with the words "streamlit"/"run" (self-matched
#    on those), then with the entrypoint filename alone (still self-matched,
#    since the pattern `'*Summary.py*'` itself contains the substring
#    "Summary.py"). There is no substring choice that escapes this — the
#    fix is `-and $_.ProcessId -ne $PID`, PowerShell's own automatic
#    variable for the CURRENT session's process id, which excludes the
#    querying process from its own result set regardless of what text it
#    searched for.
LIST_COMMAND = (
    "Get-CimInstance Win32_Process | "
    f"Where-Object {{ $_.CommandLine -like '*{ENTRYPOINT}*' -and "
    "$_.ProcessId -ne $PID } | "
    "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate)|$($_.CommandLine)\" }"
)


def running() -> list:
    """[(pid, started, command_line)] for every Streamlit process."""
    try:
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-Command", LIST_COMMAND],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  could not list processes ({exc}); nothing was stopped")
        return []

    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            found.append((int(parts[0]), parts[1], parts[2]))
    return found


def stop(pid: int) -> bool:
    """taskkill rather than PowerShell Stop-Process: it is on every Windows
    install and its exit code is unambiguous."""
    result = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True, text=True)
    return result.returncode == 0


def main() -> int:
    processes = running()
    keep = os.environ.get("MOP_RECO_KEEP_PID")
    keep = int(keep) if keep and keep.isdigit() else None
    mine = os.getpid()

    if not processes:
        print("  No dashboard is running.")
        return 0

    if "--list" in sys.argv:
        for pid, started, command in processes:
            port = "?"
            if "--server.port" in command:
                port = command.split("--server.port")[1].split()[0]
            print(f"  PID {pid}  started {started}  port {port}")
        return 0

    stopped = 0
    for pid, started, _ in processes:
        if pid in (mine, keep):
            continue
        print(f"  stopping the older dashboard: PID {pid}, started {started}")
        if stop(pid):
            stopped += 1
        else:
            print(f"    could not stop PID {pid} — close it by hand, or the "
                  "dashboard you are about to open may show stale figures")

    left = [p for p, _, _ in running() if p != mine]
    if left:
        print(f"  WARNING: {len(left)} dashboard process(es) are still "
              f"running: {left}")
        return 1
    print(f"  Stopped {stopped}. No dashboard is running now.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
