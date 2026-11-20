"""Mapping sessions always hand SLAM Toolbox a settings file.

This launch declares its own slam_params_file argument (empty by default). Included launch files
inherit launch arguments, so an empty value reached SLAM Toolbox's launch and the node ran on its
compiled-in values (live log: "maximum laser range setting (25.0 m)"; the shipped file says 20 m).
The exploration benchmark always passed the installed file; console mapping now does too.
"""
import ast
from pathlib import Path

LAUNCH = Path(__file__).resolve().parents[1] / 'launch/tb3_mapping.launch.py'


def start_session_source():
    tree = ast.parse(LAUNCH.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'start_session')
    return ast.get_source_segment(LAUNCH.read_text(), function)


def test_empty_argument_falls_back_to_the_installed_settings_file():
    source = start_session_source()
    assert "or str(slam / 'config/mapper_params_online_async.yaml')" in source
    assert 'is_file()' in source


def test_the_settings_file_is_always_passed_to_slam_toolbox():
    source = start_session_source()
    line = next(text for text in source.splitlines() if text.strip().startswith('slam_arguments ='))
    assert "'slam_params_file': slam_params_file" in line
    assert 'if slam_params_file:' not in source


def test_the_session_log_names_the_settings_file():
    assert "f'SLAM settings: {slam_params_file}.'" in start_session_source()
