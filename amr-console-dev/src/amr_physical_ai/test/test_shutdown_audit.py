"""Regression for a bridge abort hidden behind launch's final SIGKILL code."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shutdown_audit import audit_shutdown


# Reduced from the user's 2026-10-03 MPPI/easy/01 log, in original order.
MASKED_ABORT = (
    '[parameter_bridge-3] corrupted double-linked list\n'
    "[ERROR] [parameter_bridge-3]: process[parameter_bridge-3] failed to terminate "
    "'10.0' seconds after receiving 'SIGTERM', escalating to 'SIGKILL'\n"
    '[ERROR] [parameter_bridge-3]: process has died [pid 12297, exit code -9].\n'
)


def cleanup(code=0, survivors=None):
    return {'live_survivors': survivors or [], 'launch_returncode': code}


def test_abort_followed_by_sigkill_is_not_clean_even_without_survivors():
    audit = audit_shutdown(MASKED_ABORT, cleanup(-2))
    assert not audit['clean']
    assert audit['application_crashes'] == [MASKED_ABORT.splitlines()[0]]
    assert len(audit['abnormal_process_exits']) == 1
    assert audit['live_owned_survivors'] == []


@pytest.mark.parametrize('code', [-6, -11, -9, 1])
def test_nonzero_child_exit_alone_stops_run(code):
    audit = audit_shutdown(f'[ERROR] [node-1]: process has died [pid 42, exit code {code}].', cleanup())
    assert not audit['clean']
    assert len(audit['abnormal_process_exits']) == 1


@pytest.mark.parametrize('code', [-2, None])
def test_interrupted_or_unfinished_launch_cannot_certify_clean_shutdown(code):
    assert not audit_shutdown('', cleanup(code))['clean']


def test_owned_survivor_stops_even_with_clean_log():
    audit = audit_shutdown('', cleanup(survivors=[{'pid': 42}]))
    assert not audit['clean']


def test_clean_shutdown_accepts_benign_warnings_and_completed_launch():
    log = ('[WARN] [rviz2]: Stereo is NOT SUPPORTED\n'
           '[bt_navigator]: Goal was aborted\n'
           '[rclcpp]: signal_handler(SIGINT/SIGTERM)\n'
           '[INFO] [node-1]: process has finished cleanly [pid 42]\n')
    assert audit_shutdown(log, cleanup())['clean']


