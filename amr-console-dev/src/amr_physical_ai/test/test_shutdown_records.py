"""Crash exceptions must never conceal another failure or a survivor."""
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shutdown_records import classify_shutdown, build_record, write_checksheet, SHUTDOWN_MARKER

CLEANUP = {'launch_returncode': 0, 'live_survivors': [],
           'tracked_processes': [{'pid': 123, 'start_ticks': 456}]}
BOND = """[planner_server-6] terminate called after throwing an instance of 'rclcpp::exceptions::RCLError'
[planner_server-6] what(): failed to publish message: failed to publish data
[planner_server-6] bond::Bond::publishStatus()
[ERROR] [planner_server-6]: process has died [pid 123, exit code -6, cmd 'planner_server'].
"""


def audit(text, survivors=None, cleanup=None):
    return classify_shutdown(text, cleanup or CLEANUP, survivors or [], {'complete': True})


def test_clean_is_crash_free_not_just_no_survivors():
    assert audit(SHUTDOWN_MARKER)['clean']
    result = audit(SHUTDOWN_MARKER+'\n'+BOND)
    assert result['bond_abort'] and result['continue_allowed']
    assert not result['clean'] and result['isolation_pass']


def test_fault_before_shutdown_and_unknown_abort_stop():
    assert not audit(BOND+'\n'+SHUTDOWN_MARKER)['continue_allowed']
    result = audit(SHUTDOWN_MARKER+'\n'+BOND.replace('[planner_server-6] bond::Bond::publishStatus()\n', ''))
    assert not result['continue_allowed']
    assert result['bond_unconfirmed_candidates']


def test_bond_never_waives_other_fault_or_survivor():
    for addition in ('[parameter_bridge-3] corrupted double-linked list\n',
                     '[ERROR] [rviz2-9]: process has died [pid 567, exit code -11, cmd rviz2].\n',
                     '[planner_server-6] free(): invalid pointer\n'):
        assert not audit(SHUTDOWN_MARKER+'\n'+BOND+addition)['continue_allowed']
    for pid in (123, 999):
        result = audit(SHUTDOWN_MARKER+'\n'+BOND, [{'pid': pid, 'start_ticks': 456}])
        assert not result['continue_allowed'] and not result['isolation_pass']


def test_incomplete_inventory_or_missing_marker_stops():
    assert not audit('')['continue_allowed']
    assert not classify_shutdown(SHUTDOWN_MARKER, CLEANUP, None, {'complete': True})['continue_allowed']
    assert not audit(SHUTDOWN_MARKER, cleanup={**CLEANUP, 'launch_returncode': -2})['continue_allowed']


def test_record_errors_and_verdict_survive_regeneration(tmp_path):
    row = build_record({'result': 'SUCCEEDED', 'final_map_x': 1.0, 'final_map_y': 2.0,
                       'final_true_x': 1.3, 'final_true_y': 2.4, 'final_pos_error_m': .2},
                      audit(SHUTDOWN_MARKER), map_name='objective1', controller='mppi',
                      tier='medium', goal_id='medium_01', trial_id='01',
                      trial_dir='mppi/medium/trial_01', transport={'ROS_DOMAIN_ID': '42', 'GZ_PARTITION': 'x'},
                      artifacts={'screenshots': {'start': ['mppi/medium/trial_01/start.png']}})
    assert abs(row['amcl_error_m']-.5) < 1e-10
    write_checksheet(tmp_path, [row])
    path = tmp_path/'CHECKSHEET.md'
    text = path.read_text().replace('| mppi/medium/01 |  |  |  |  |  |', '| mppi/medium/01 | yes | no | no | no | looked clear |')
    path.write_text(text)
    write_checksheet(tmp_path, [row])
    assert 'looked clear' in path.read_text()
    assert json.loads((tmp_path/'records.json').read_text())[0]['verdict']['Notes'] == 'looked clear'
