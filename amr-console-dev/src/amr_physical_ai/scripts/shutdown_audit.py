"""Offline shutdown audit; an empty survivor list alone is not a clean exit."""
import re


# A crash can be reported as SIGKILL if launch times out while the process is
# handling a fatal error. Keep the diagnostic even when the final code is -9.
FATAL_DIAGNOSTIC = re.compile(
    r'corrupted (?:double-linked list|size vs\.? prev_size)|'
    r'double free or corruption|(?:malloc|free|munmap_chunk)\(\):|'
    r'terminate called|segmentation fault|'
    r'(?:^|\]\s+)aborted(?: \(core dumped\))?\s*$', re.IGNORECASE)
EXIT_CODE = re.compile(r'exit code (-?\d+)\b')
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*m')


def audit_shutdown(log, cleanup):
    """Conservative clean-shutdown gate; does not waive the Nav2 bond abort or infer unowned cleanup.

    Launch output supplies application diagnostics and exit codes. A nonzero
    launch-leader exit is also a failure, since its child reporting may have
    been interrupted. Original log lines are retained as evidence.
    """
    crashes = []
    abnormal_exits = []
    for raw in log.splitlines():
        line = ANSI_ESCAPE.sub('', raw)
        code = EXIT_CODE.search(line) if 'process has died' in line else None
        if code and int(code.group(1)) != 0:
            abnormal_exits.append(raw)
        if FATAL_DIAGNOSTIC.search(line) or (code and int(code.group(1)) in (-6, -11)):
            crashes.append(raw)
    survivors = cleanup['live_survivors']
    launch_code = cleanup['launch_returncode']
    return {
        'application_crashes': crashes,
        'abnormal_process_exits': abnormal_exits,
        'live_owned_survivors': survivors,
        'launch_returncode': launch_code,
        'clean': not (crashes or abnormal_exits or survivors) and launch_code == 0,
        'scope': 'Owned cleanup and launch-log audit; no unowned process audit.',
    }
