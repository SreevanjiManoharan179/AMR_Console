"""Drive the existing TB3 sandbox with LiDAR/IMU and live SLAM.

With explore:=<ours|gain|nearest> and explore_out:=<new folder> (autonomous exploration), Nav2 (navigation_launch.py with
params_file) runs on the live SLAM map and the explorer node chooses the goals and records the run.
"""

import math
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    AppendEnvironmentVariable, DeclareLaunchArgument, ExecuteProcess,
    IncludeLaunchDescription, LogInfo, OpaqueFunction,
)
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
import xacro
import yaml

EXPLORE_METHODS = ('ours', 'gain', 'nearest')     # amr_physical_ai.exploration.METHODS


def without_camera(root):
    """Omit camera links, their joints, and sensors from the reference model."""
    camera_links = {link.get('name') for link in root.findall('.//link')
                    if 'camera' in link.get('name', '')}
    for parent in root.iter():
        for child in list(parent):
            remove = child.tag == 'link' and child.get('name') in camera_links
            if child.tag == 'joint':
                for tag in ('parent', 'child'):
                    endpoint = child.find(tag)
                    if endpoint is not None:
                        remove |= (endpoint.get('link') or endpoint.text) in camera_links
            if child.tag == 'sensor':
                remove |= child.get('type') not in ('imu', 'gpu_lidar')
            if remove:
                parent.remove(child)
    return root


def map_extent(map_yaml):
    """(x0, x1, y0, y1) in the map frame of a saved map: its YAML origin and PGM size."""
    meta = yaml.safe_load(Path(map_yaml).read_text())
    resolution = float(meta['resolution'])
    x0, y0 = float(meta['origin'][0]), float(meta['origin'][1])
    with (Path(map_yaml).parent / meta['image']).open('rb') as stream:
        tokens = []
        while len(tokens) < 3:
            line = stream.readline()
            if not line:
                raise ValueError(f'{map_yaml}: incomplete PGM header')
            tokens += line.split(b'#', 1)[0].split()
    width, height = int(tokens[1]), int(tokens[2])
    return x0, x0 + width * resolution, y0, y0 + height * resolution


def gazebo_window(map_yaml, spawn, folder, home=None):
    """Gazebo window settings for a world with a saved map: the grid over the map, the camera on it.

    Gazebo's grid and camera start around world (0, 0), but the map frame's (0, 0) is the robot's
    spawn, so part of a world can sit off the grid. This copies the user's own Gazebo window file
    (~/.gz/sim/8/gui.config), replaces any grid in it with one centred on the saved map on whole
    map metres (the same 1 m lines as the console's RViz grid), points the camera at the map, and
    writes the result to folder/gui.config. Works for any world with a saved map: world = map +
    spawn. Display only: the world, robot, sensors and physics are unchanged. Returns None, so
    Gazebo keeps its own settings, when the map or the user's file is missing.
    """
    base = Path(home or Path.home()) / '.gz/sim/8/gui.config'
    if not Path(map_yaml).is_file() or not base.is_file():
        return None
    x0, x1, y0, y1 = map_extent(map_yaml)
    to_world_x, to_world_y = float(spawn[0]), float(spawn[1])
    center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
    grid_x, grid_y = math.floor(center_x + .5), math.floor(center_y + .5)     # whole map metres
    reach = max((x1 - x0) / 2 + abs(center_x - grid_x), (y1 - y0) / 2 + abs(center_y - grid_y))
    count = 2 * math.ceil(reach)                                              # covers the whole map
    text = base.read_text()
    if text.lstrip().startswith('<?xml'):
        text = text.split('?>', 1)[1]
    root = ET.fromstring(f'<gui_config>{text}</gui_config>')
    for plugin in root.findall('plugin'):
        if plugin.get('filename') == 'GridConfig':
            root.remove(plugin)
    scene = next((p for p in root.findall('plugin') if p.get('filename') == 'MinimalScene'), None)
    if scene is not None:
        # Looking north at the office centre from the south, 0.9 rad down, far enough to see it all.
        distance = 1.9 * (max(x1 - x0, y1 - y0) / 2 + 1)
        pose = scene.find('camera_pose')
        if pose is None:
            pose = ET.SubElement(scene, 'camera_pose')
        pose.text = (f'{center_x + to_world_x:.2f} {center_y + to_world_y - distance * math.cos(.9):.2f} '
                     f'{distance * math.sin(.9):.2f} 0 0.9 1.5708')
    grid = ET.SubElement(root, 'plugin', {'filename': 'GridConfig', 'name': 'Grid config'})
    gui = ET.SubElement(grid, 'gz-gui')
    for key, kind, value in (('resizable', 'bool', 'false'), ('width', 'double', '5'), ('height', 'double', '5'),
                             ('state', 'string', 'floating'), ('showTitleBar', 'bool', 'false')):
        ET.SubElement(gui, 'property', {'key': key, 'type': kind}).text = value
    insert = ET.SubElement(grid, 'insert')
    for tag, value in (('horizontal_cell_count', str(count)), ('vertical_cell_count', '0'),
                       ('cell_length', '1'),
                       ('pose', f'{grid_x + to_world_x:g} {grid_y + to_world_y:g} 0.005 0 0 0'),
                       ('color', '0.45 0.45 0.45 1')):
        ET.SubElement(insert, tag).text = value
    target = Path(folder) / 'gui.config'
    body = ''.join(ET.tostring(child, encoding='unicode') for child in root)
    target.write_text('<?xml version="1.0"?>\n' + body + '\n')
    return target


def start_session(context):
    sim = Path(get_package_share_directory('nav2_minimal_tb3_sim'))
    own = Path(get_package_share_directory('amr_physical_ai'))
    headless = LaunchConfiguration('headless').perform(context).lower() == 'true'
    navigation = LaunchConfiguration('navigation').perform(context).lower() == 'true'
    explore = LaunchConfiguration('explore').perform(context).strip()
    explore_out = LaunchConfiguration('explore_out').perform(context).strip()
    if explore and explore not in EXPLORE_METHODS:
        raise ValueError(f'explore must be one of {", ".join(EXPLORE_METHODS)}, not {explore!r}.')
    if explore and navigation:
        raise ValueError('explore maps with online SLAM; it cannot be combined with navigation:=true.')
    if explore and not explore_out:
        raise ValueError('explore needs explore_out, a new folder for the run records.')
    rviz_config = LaunchConfiguration('rviz_config').perform(context)
    if not rviz_config:
        rviz_config = str(own / ('rviz/tb3_navigation.rviz' if navigation or explore else 'rviz/tb3_mapping.rviz'))
    world_file = LaunchConfiguration('world').perform(context)
    world = ET.fromstring(xacro.process_file(
        world_file,
        mappings={'headless': str(headless).lower()}).toxml())
    robot = without_camera(ET.fromstring(xacro.process_file(
        str(sim / 'urdf/gz_waffle.sdf.xacro'), mappings={'namespace': ''}).toxml()))
    model = robot.find('model')
    pose = model.find('pose')
    if pose is None:
        pose = ET.SubElement(model, 'pose')
    spawn = [float(LaunchConfiguration(name).perform(context))
             for name in ('spawn_x', 'spawn_y', 'spawn_yaw')]
    pose.text = f'{spawn[0]} {spawn[1]} 0.01 0 0 {spawn[2]}'
    ground_truth = LaunchConfiguration('ground_truth').perform(context).lower() == 'true'
    if ground_truth:
        publisher = ET.SubElement(model, 'plugin', {
            'filename': 'gz-sim-pose-publisher-system',
            'name': 'gz::sim::systems::PosePublisher'})
        for name, value in {
            'publish_model_pose': 'true', 'publish_nested_model_pose': 'true',
            'publish_link_pose': 'false', 'publish_visual_pose': 'false',
            'publish_collision_pose': 'false', 'publish_sensor_pose': 'false',
            'use_pose_vector_msg': 'true', 'update_frequency': '50',
            'topic': '/amr/ground_truth'}.items():
            ET.SubElement(publisher, name).text = value
    world.find('world').append(model)
    # Preserve generated input for troubleshooting; installed reference files stay intact.
    generated = Path(tempfile.mkdtemp(prefix='amr_tb3_mapping_')) / 'world.sdf'
    ET.ElementTree(world).write(generated, encoding='utf-8', xml_declaration=True)
    urdf = without_camera(ET.parse(sim / 'urdf/turtlebot3_waffle.urdf').getroot())
    # The installed URDF points at models/*.dae, but this package stores meshes
    # in models/turtlebot3_model/meshes. Resolve the actual installed files.
    for mesh in urdf.findall('.//mesh'):
        filename = Path(mesh.get('filename')).name
        installed_mesh = sim / 'models/turtlebot3_model/meshes' / filename
        if not installed_mesh.is_file():
            raise FileNotFoundError(f'TB3 visual mesh is missing: {installed_mesh}')
        mesh.set('filename', 'package://nav2_minimal_tb3_sim/'
                 f'models/turtlebot3_model/meshes/{filename}')
    description = ET.tostring(urdf, encoding='unicode')
    gz_args = ['gz', 'sim', '-r', '-v', '2']
    window = None
    if headless:
        gz_args += ['-s', '--headless-rendering']
    elif LaunchConfiguration('gazebo_view').perform(context).lower() == 'true':
        stem = Path(world_file).name.split('.')[0]
        window = gazebo_window(own / 'maps' / f'{stem}.yaml', spawn, generated.parent)
        if window:
            gz_args += ['--gui-config', str(window)]
    gz_args.append(str(generated))
    slam = Path(get_package_share_directory('slam_toolbox'))
    # Empty: SLAM Toolbox's installed settings file (mapper_params_online_async.yaml), the same file
    # the exploration benchmark uses. It is always passed explicitly: this launch's own empty
    # slam_params_file argument would otherwise reach SLAM Toolbox's launch and leave the node on its
    # compiled-in values (for example a 25 m laser range instead of the file's 20 m).
    slam_params_file = (LaunchConfiguration('slam_params_file').perform(context).strip()
                        or str(slam / 'config/mapper_params_online_async.yaml'))
    if not Path(slam_params_file).is_file():
        raise FileNotFoundError(f'slam_params_file does not exist: {slam_params_file}')
    slam_arguments = {'use_sim_time': 'true', 'autostart': 'true', 'slam_params_file': slam_params_file}
    bringup = Path(get_package_share_directory('nav2_bringup'))
    stack = (IncludeLaunchDescription(PythonLaunchDescriptionSource(
        str(bringup / 'launch/bringup_launch.py')),
        launch_arguments={'map': LaunchConfiguration('map'),
                          'params_file': LaunchConfiguration('params_file'),
                          'slam': 'False', 'use_sim_time': 'true',
                          'autostart': 'true',
                          'use_composition': LaunchConfiguration('use_composition')}.items())
        if navigation else IncludeLaunchDescription(PythonLaunchDescriptionSource(
            str(slam / 'launch/online_async_launch.py')),
            launch_arguments=slam_arguments.items()))
    exploration = []
    if explore:
        # Nav2 without map_server/AMCL: SLAM Toolbox provides /map and map->odom.
        params_file = LaunchConfiguration('params_file').perform(context)
        exploration = [
            IncludeLaunchDescription(PythonLaunchDescriptionSource(
                str(bringup / 'launch/navigation_launch.py')),
                launch_arguments={'params_file': params_file, 'use_sim_time': 'true', 'autostart': 'true',
                                  'use_composition': LaunchConfiguration('use_composition')}.items()),
            ExecuteProcess(name='amr_explorer', output='screen', cmd=[
                'python3', '-m', 'amr_physical_ai.explore_node', '--method', explore, '--out', explore_out,
                '--world', Path(world_file).name.split('.')[0], '--spawn', *[str(v) for v in spawn],
                '--nav2-params', params_file, '--ros-args', '-p', 'use_sim_time:=true']),
        ]
    return [
        LogInfo(msg=f'TB3 mapping world: {generated}; camera omitted; spawn {spawn}.'),
        LogInfo(msg=f'Gazebo window: {window or "Gazebo defaults"}.'),
        LogInfo(msg=('SLAM settings: navigation uses the saved map.' if navigation
                     else f'SLAM settings: {slam_params_file}.')),
        ExecuteProcess(cmd=gz_args, output='screen'),
        Node(package='ros_gz_bridge', executable='parameter_bridge', output='screen',
             parameters=[{'config_file': str(sim / 'configs/turtlebot3_waffle_bridge.yaml'),
                          'use_sim_time': True}]),
        Node(package='ros_gz_bridge', executable='parameter_bridge', output='screen',
             name='amr_truth_bridge',
             arguments=['/amr/ground_truth@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V'],
             parameters=[{'use_sim_time': True}],
             condition=IfCondition(LaunchConfiguration('ground_truth'))),
        Node(package='robot_state_publisher', executable='robot_state_publisher',
             output='screen', parameters=[{'robot_description': description,
                                          'use_sim_time': True}]),
        stack,
        *exploration,
        Node(package='rviz2', executable='rviz2', output='screen',
             arguments=['-d', rviz_config],
             parameters=[{'use_sim_time': True}],
             condition=IfCondition(LaunchConfiguration('use_rviz'))),
    ]


def generate_launch_description():
    sim = Path(get_package_share_directory('nav2_minimal_tb3_sim'))
    own = Path(get_package_share_directory('amr_physical_ai'))
    return LaunchDescription([
        DeclareLaunchArgument('headless', default_value='false',
                              description='Run the server without the Gazebo window'),
        DeclareLaunchArgument('use_rviz', default_value='true'),
        DeclareLaunchArgument('gazebo_view', default_value='true',
                              description='Lay the Gazebo grid and camera over the world\'s saved map (display only)'),
        DeclareLaunchArgument('slam_params_file', default_value='',
                              description='SLAM Toolbox settings file; empty = its installed mapper_params_online_async.yaml'),
        DeclareLaunchArgument('world', default_value=str(sim / 'worlds/tb3_sandbox.sdf.xacro')),
        DeclareLaunchArgument('spawn_x', default_value='-2.0'),
        DeclareLaunchArgument('spawn_y', default_value='-0.5'),
        DeclareLaunchArgument('spawn_yaw', default_value='0.0'),
        DeclareLaunchArgument('rviz_config', default_value='',
                              description='Optional per-run RViz display configuration'),
        DeclareLaunchArgument('use_composition', default_value='False',
                              description='Run Nav2 in its supported component container'),
        DeclareLaunchArgument('navigation', default_value='false',
                              description='Use saved-map AMCL/Nav2 instead of online SLAM'),
        DeclareLaunchArgument('ground_truth', default_value='false',
                              description='Publish Gazebo model pose on a separate measurement topic'),
        DeclareLaunchArgument('explore', default_value='',
                              description='Explore with ours, gain or nearest: Nav2 on the live SLAM map'),
        DeclareLaunchArgument('explore_out', default_value='',
                              description='New folder for the exploration records; required with explore'),
        DeclareLaunchArgument('map', default_value=str(own / 'maps/proposed_system.yaml')),
        DeclareLaunchArgument('params_file', default_value=str(own / 'config/nav2_mppi.yaml')),
        AppendEnvironmentVariable('GZ_SIM_RESOURCE_PATH', str(sim / 'models')),
        AppendEnvironmentVariable('GZ_SIM_RESOURCE_PATH', str(sim.parent)),
        OpaqueFunction(function=start_session),
    ])
