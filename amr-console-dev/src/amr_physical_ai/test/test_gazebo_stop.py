"""Gazebo shutdown transport boundaries, without processes or ROS contexts."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from gazebo_stop import stop_gazebo

TARGET = {'pid': 123, 'start_ticks': 45, 'cmd': 'gz sim -s world.sdf', 'owned': True}
ENV = {'GZ_PARTITION': 'amr_vis_objective1_test_001', 'ROS_DOMAIN_ID': '36'}


def test_ack_waits_for_same_pid_incarnation_and_never_signals():
    owned = Mock()
    # Reuse of the numeric PID must not keep the old Gazebo identity alive.
    owned.refresh.side_effect = [[TARGET], [dict(TARGET, start_ticks=99)]]
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='data: true', stderr=''))
    report = stop_gazebo(owned, ENV, inventory=lambda _: [TARGET], run=run,
                         sleep=Mock(), clock=lambda: 1.)
    assert report['accepted'] and report['complete']
    assert report['remaining'] == [] and report['targets'] == [TARGET]
    assert run.call_args.kwargs['env'] is ENV
    assert run.call_args.args[0][3] == '/server_control'
    owned._signal.assert_not_called()


@pytest.mark.parametrize('result', [
    SimpleNamespace(returncode=1, stdout='', stderr='transport failed'),
    SimpleNamespace(returncode=0, stdout='data: false', stderr=''),
])
def test_unacknowledged_request_is_failure_even_without_later_survivors(result):
    owned = Mock()
    owned.refresh.return_value = []
    report = stop_gazebo(owned, ENV, inventory=lambda _: [TARGET],
                         run=Mock(return_value=result))
    assert not report['accepted'] and not report['complete']
    assert 'not acknowledged' in report['error']
    owned.refresh.assert_not_called()


def test_acknowledged_request_times_out_with_same_identity_still_live():
    owned = Mock()
    owned.refresh.return_value = [TARGET]
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='data: true', stderr=''))
    report = stop_gazebo(owned, ENV, inventory=lambda _: [TARGET], run=run,
                         timeout=0, clock=lambda: 1., sleep=Mock())
    assert report['accepted'] and not report['complete']
    assert report['remaining'] == [TARGET]
    assert 'did not exit' in report['error']
    owned._signal.assert_not_called()


@pytest.mark.parametrize('failure', [
    subprocess.TimeoutExpired(['gz', 'service'], 6), OSError('gz unavailable')])
def test_transport_exception_is_a_failed_report(failure):
    report = stop_gazebo(Mock(), ENV, inventory=lambda _: [TARGET],
                         run=Mock(side_effect=failure))
    assert not report['accepted'] and not report['complete']
    assert 'request failed' in report['error']


def test_foreign_process_refuses_request_without_signalling_anyone():
    owned, run = Mock(), Mock()
    foreign = dict(TARGET, pid=999, owned=False)
    report = stop_gazebo(owned, ENV, inventory=lambda _: [TARGET, foreign], run=run)
    assert not report['accepted'] and not report['complete']
    assert report['unowned'] == [foreign] and 'refused' in report['error']
    run.assert_not_called()
    owned._signal.assert_not_called()


def test_no_owned_gazebo_never_requests_a_transport_stop():
    run = Mock()
    report = stop_gazebo(Mock(), ENV, inventory=lambda _: [], run=run)
    assert report['complete'] and not report['accepted']
    assert report['targets'] == []
    run.assert_not_called()


def test_foreign_process_appearing_at_request_boundary_refuses_stop():
    run = Mock()
    foreign = dict(TARGET, pid=999, owned=False)
    inventory = Mock(side_effect=[[TARGET], [TARGET, foreign]])
    report = stop_gazebo(Mock(), ENV, inventory=inventory, run=run)
    assert not report['complete'] and report['unowned'] == [foreign]
    run.assert_not_called()


def test_old_target_exiting_before_request_sends_nothing():
    run = Mock()
    report = stop_gazebo(Mock(), ENV, inventory=Mock(side_effect=[[TARGET], []]), run=run)
    assert report['complete'] and not report['accepted']
    run.assert_not_called()


def test_reused_target_pid_is_foreign_at_request_boundary():
    run = Mock()
    reused = dict(TARGET, start_ticks=99, owned=False)
    report = stop_gazebo(Mock(), ENV,
                         inventory=Mock(side_effect=[[TARGET], [reused]]), run=run)
    assert not report['complete'] and report['unowned'] == [reused]
    run.assert_not_called()


def test_changed_identity_is_refused_even_if_new_process_is_owned():
    run = Mock()
    changed = dict(TARGET, start_ticks=99)
    report = stop_gazebo(Mock(), ENV,
                         inventory=Mock(side_effect=[[TARGET], [changed]]), run=run)
    assert not report['complete'] and report['changed_identities'] == [changed]
    run.assert_not_called()
