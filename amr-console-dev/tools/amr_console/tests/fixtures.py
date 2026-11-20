"""Isolated repository fixtures and a process-free session supervisor."""
from pathlib import Path
import shutil
import tempfile


REAL_ROOT = Path(__file__).resolve().parents[3]


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.epoch = 1791160000.0

    def __call__(self):
        return self.value

    def wall(self):
        return self.epoch + self.value

    def advance(self, seconds):
        self.value += seconds


class FixtureRepo:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='amr_console_unit_')
        self.root = Path(self.directory.name)
        for relative in ('scripts/check.sh', 'scripts/build.sh',
                         'config/worlds.yaml',
                         'tools/amr_console/typed_commands.py',
                         'src/amr_physical_ai/amr_physical_ai/command_pipeline.py',
                         'tools/amr_console/web/index.html',
                         'src/amr_physical_ai/scripts/window_capture.py',
                         'src/amr_physical_ai/scripts/gazebo_stop.py',
                         'src/amr_physical_ai/scripts/transport_isolation.py',
                         'src/amr_physical_ai/scripts/shutdown_records.py',
                         'src/amr_physical_ai/scripts/shutdown_audit.py',
                         'src/amr_physical_ai/scripts/process_ownership.py',
                         'src/amr_physical_ai/scripts/nav2_shutdown.py',
                         'src/amr_physical_ai/scripts/rviz_windows.py',
                         'src/amr_physical_ai/launch/tb3_mapping.launch.py',
                         'src/amr_physical_ai/launch/tb3_navigation.launch.py',
                         'src/amr_physical_ai/rviz/tb3_mapping.rviz',
                         'src/amr_physical_ai/rviz/tb3_navigation.rviz',
                         'src/amr_physical_ai/config/nav2_mppi.yaml',
                         'src/amr_physical_ai/config/nav2_dwb.yaml',
                         'src/amr_physical_ai/config/nav2_rpp.yaml',
                         'src/amr_physical_ai/config/named_locations_proposed_system.yaml',
                         'src/amr_physical_ai/maps/tb3_sandbox.yaml',
                         'src/amr_physical_ai/maps/tb3_sandbox.pgm'):
            source = REAL_ROOT / relative
            if source.is_file():
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        # Delivery commands stay locked unless a test approves them.
        self.write('tools/amr_console/approvals.yaml', 'proposed_system_places: false\n')
        locations = self.root / 'src/amr_physical_ai/config/named_locations_proposed_system.yaml'
        locations.write_text('\n'.join('approval_status: confirmed' if line.startswith('approval_status:')
                                       else line for line in locations.read_text().splitlines()) + '\n')
        self.write('data/environment.txt', 'Local fixture: Gazebo Sim 8.15.0\n')
        self.write('install/setup.bash', '# fixture only\n')

    def copy_from_repo(self, *relatives):
        """Copy more product files into the fixture (for example a world's map and world file)."""
        for relative in relatives:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REAL_ROOT / relative, target)

    def write(self, relative, text):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def close(self):
        self.directory.cleanup()


class FakeSupervisor:
    def __init__(self, spec, on_event):
        self.spec = spec
        self.on_event = on_event
        self.pid = 12345
        self.started = False
        self.controls = []
        self.respond_to_operations = True
        self.stop_report = {'survivors': 0, 'forced': False, 'bond_abort': False,
                            'clean': True, 'exit_code': 0,
                            'report': f"data/logs/console/{spec['run_id']}/cleanup.json"}

    def start(self):
        self.started = True
        self.on_event({'type': 'ready', 'pid_group': self.pid})

    def control(self, kind, **kwargs):
        self.controls.append((kind, kwargs))
        if kind == 'operation' and self.respond_to_operations:
            self.complete_operation(kwargs['request_id'])

    def complete_operation(self, request_id, ok=True, result=None):
        self.on_event({'type': 'operation_result', 'request_id': request_id,
                       'ok': ok, 'result': result or {'ok': True}})

    def stop(self):
        self.on_event({'type': 'finished', 'cleanup': self.stop_report,
                       'exit_code': self.stop_report['exit_code']})
        return self.stop_report


class FakeSupervisors:
    def __init__(self):
        self.instances = []

    def __call__(self, spec, on_event):
        supervisor = FakeSupervisor(spec, on_event)
        self.instances.append(supervisor)
        return supervisor

    @property
    def last(self):
        return self.instances[-1]


ENVIRONMENT = {
    'ros_distro': 'jazzy', 'rmw': 'rmw_cyclonedds_cpp', 'gazebo': '8.15.0',
    'commit': 'unit-fixture-commit', 'session_type': 'x11',
    'arrange_supported': False, 'arrange_reason': 'No window arranger is installed.',
    'recording_supported': False, 'recording_reason': 'ffmpeg is not installed.',
}
