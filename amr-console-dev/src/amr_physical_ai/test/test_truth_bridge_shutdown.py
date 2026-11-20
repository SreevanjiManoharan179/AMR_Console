"""Bridge shutdown abort: only a ros_gz_bridge process's post-shutdown heap abort may be waived."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from shutdown_records import classify_shutdown, build_record, write_checksheet, SHUTDOWN_MARKER

CLEANUP = {'launch_returncode': 0, 'live_survivors': [],
           'tracked_processes': [{'pid': 123, 'start_ticks': 456}, {'pid': 39673, 'start_ticks': 789}]}
TRUTH_CMD = ('/opt/ros/jazzy/lib/ros_gz_bridge/parameter_bridge '
             '/amr/ground_truth@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V '
             '--ros-args -r __node:=amr_truth_bridge --params-file /tmp/launch_params_4z3lqwtz')
MAIN_CMD = '/opt/ros/jazzy/lib/ros_gz_bridge/parameter_bridge --ros-args --params-file /tmp/launch_params_uyk23s4p'
# Lines 528, 531 and 589 of run_20261012_125529 mppi/medium/trial_02/launch.log.
SIGNAL = '[parameter_bridge-3] [INFO] [1791790298.479269741] [rclcpp]: signal_handler(SIGINT/SIGTERM)\n'
HEAP = '[parameter_bridge-3] corrupted double-linked list\n'


def death(command=TRUTH_CMD, code=-6, pid=39673):
    return f"[ERROR] [parameter_bridge-3]: process has died [pid {pid}, exit code {code}, cmd '{command}'].\n"


TRUTH_ABORT = SIGNAL + HEAP + death()
BOND = """[planner_server-6] terminate called after throwing an instance of 'rclcpp::exceptions::RCLError'
[planner_server-6] what(): failed to publish message: failed to publish data
[planner_server-6] bond::Bond::publishStatus()
[ERROR] [planner_server-6]: process has died [pid 123, exit code -6, cmd 'planner_server'].
"""


def audit(text, survivors=None, cleanup=None):
    return classify_shutdown(text, cleanup or CLEANUP, survivors or [], {'complete': True})


def test_real_trial_02_truth_bridge_abort_continues_but_is_never_clean():
    result = audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT)
    assert result['continue_allowed'] and result['allowed_exception']
    assert result['truth_bridge_abort'] and not result['bond_abort']
    assert not result['clean'] and result['isolation_pass']
    assert result['unwaived_events'] == [] and result['stop_reasons'] == []
    assert result['truth_bridge_abort_processes'][0]['pid'] == 39673


def test_abort_before_shutdown_marker_stops():
    result = audit(TRUTH_ABORT + SHUTDOWN_MARKER)
    assert not result['continue_allowed'] and not result['truth_bridge_abort']
    split = audit(HEAP + SHUTDOWN_MARKER + '\n' + death())
    assert not split['continue_allowed'] and not split['truth_bridge_abort']


# Lines 540 and 589 of run_20261012_133755 mppi/hard/trial_03/launch.log (main bridge).
MAIN_HEAP = '[parameter_bridge-2] corrupted double-linked list\n'
MAIN_DEATH = ("[ERROR] [parameter_bridge-2]: process has died [pid 54799, exit code -6, cmd '"
              + MAIN_CMD + "'].\n")


def test_real_trial_hard_03_main_bridge_abort_continues_and_names_the_bridge():
    result = audit(SHUTDOWN_MARKER + '\n' + MAIN_HEAP + MAIN_DEATH)
    assert result['continue_allowed'] and result['truth_bridge_abort'] and not result['clean']
    assert result['truth_bridge_abort_processes'][0]['bridge'] == 'main bridge'
    assert result['truth_bridge_abort_processes'][0]['pid'] == 54799
    truth = audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT)
    assert truth['truth_bridge_abort_processes'][0]['bridge'] == 'ground-truth bridge'


def test_other_exit_codes_and_non_bridge_commands_are_not_waived():
    for line in (death(code=-11), death(code=1),
                 death(command='/opt/ros/jazzy/lib/nav2_amcl/amcl --ros-args')):
        result = audit(SHUTDOWN_MARKER + '\n' + SIGNAL + HEAP + line)
        assert not result['continue_allowed'] and not result['truth_bridge_abort']


def test_abort_without_heap_diagnostic_or_with_other_diagnostic_stops():
    assert not audit(SHUTDOWN_MARKER + '\n' + SIGNAL + death())['continue_allowed']
    other = "[parameter_bridge-3] terminate called after throwing an instance of 'std::runtime_error'\n"
    result = audit(SHUTDOWN_MARKER + '\n' + SIGNAL + HEAP + other + death())
    assert not result['continue_allowed'] and not result['truth_bridge_abort']


def test_truth_bridge_abort_never_hides_another_fault_survivor_or_missing_marker():
    for addition in ('[ERROR] [rviz2-19]: process has died [pid 567, exit code -11, cmd rviz2].\n',
                     '[controller_server-8] free(): invalid pointer\n'):
        assert not audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT + addition)['continue_allowed']
    survived = audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT, [{'pid': 999, 'start_ticks': 1}])
    assert not survived['continue_allowed'] and not survived['isolation_pass']
    assert not audit(TRUTH_ABORT)['continue_allowed']
    assert not audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT,
                     cleanup={**CLEANUP, 'launch_returncode': -2})['continue_allowed']


def test_two_bridge_deaths_are_not_waived():
    result = audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT + death(pid=40000))
    assert not result['continue_allowed']


def test_bond_and_truth_bridge_exceptions_combine():
    result = audit(SHUTDOWN_MARKER + '\n' + BOND + TRUTH_ABORT)
    assert result['continue_allowed'] and result['bond_abort'] and result['truth_bridge_abort']
    assert not result['clean']


def test_record_and_checksheet_count_bridge_aborts_separately(tmp_path):
    row = build_record({'result': 'SUCCEEDED'}, audit(SHUTDOWN_MARKER + '\n' + TRUTH_ABORT),
                       map_name='objective1', controller='mppi', tier='medium', goal_id='medium_02',
                       trial_id='02', trial_dir='mppi/medium/trial_02',
                       transport={'ROS_DOMAIN_ID': '28', 'GZ_PARTITION': 'x'})
    assert row['truth_bridge_abort'] and not row['shutdown_clean'] and row['continue_allowed']
    summary = write_checksheet(tmp_path, [row])
    assert summary['truth_bridge_abort_count'] == 1 and summary['bond_abort_count'] == 0
    text = (tmp_path / 'CHECKSHEET.md').read_text()
    assert 'crash-free shutdowns: 0/1' in text
    assert 'proven bridge shutdown abort trials: 1' in text
    assert '| Bridge shutdown abort |' in text
