import torch

a = torch.zeros(4, device="cuda:0")

# Standard Library
import argparse

parser = argparse.ArgumentParser()
parser.add_argument(
    "--headless_mode",
    type=str,
    default=None,
    help="To run headless, use one of [native, websocket], webrtc might not work.",
)
parser.add_argument(
    "--visualize_spheres",
    action="store_true",
    help="When True, visualizes robot spheres",
    default=False,
)

parser.add_argument("--robot", type=str, default="soar.yml", help="robot configuration to load")
args = parser.parse_args()

############################################################

# Third Party
from omni.isaac.kit import SimulationApp

simulation_app = SimulationApp(
    {
        "headless": args.headless_mode is not None,
        "width": "1920",
        "height": "1080",
    }
)
# Standard Library
from typing import Dict

# Third Party
import carb
import numpy as np
from helper import add_extensions, add_robot_to_scene
from omni.isaac.core import World
from omni.isaac.core.objects import cuboid, sphere

# CuRobo
from curobo.cuda_robot_model.cuda_robot_model import CudaRobotModel

# from curobo.wrap.reacher.ik_solver import IKSolver, IKSolverConfig
from curobo.geom.sdf.world import CollisionCheckerType
from curobo.geom.types import WorldConfig
from curobo.rollout.rollout_base import Goal
from curobo.types.base import TensorDeviceType
from curobo.types.math import Pose
from curobo.types.robot import JointState, RobotConfig
from curobo.types.state import JointState
from curobo.util.logger import setup_curobo_logger
from curobo.util.usd_helper import UsdHelper
from curobo.util_file import (
    get_assets_path,
    get_filename,
    get_path_of_dir,
    get_robot_configs_path,
    get_world_configs_path,
    join_path,
    load_yaml,
)
from curobo.wrap.reacher.ik_solver import IKSolver, IKSolverConfig
from curobo.wrap.reacher.motion_gen import MotionGen, MotionGenConfig, MotionGenPlanConfig
from curobo.wrap.reacher.mpc import MpcSolver, MpcSolverConfig

########### OV #################


############################################################


########### OV #################;;;;;


def get_pose_grid(n_x, n_y, n_z, max_x, max_y, max_z):
    x = np.linspace(-max_x, max_x, n_x)
    y = np.linspace(-max_y, max_y, n_y)
    z = np.linspace(0, max_z, n_z)
    x, y, z = np.meshgrid(x, y, z, indexing="ij")

    position_arr = np.zeros((n_x * n_y * n_z, 3))
    position_arr[:, 0] = x.flatten()
    position_arr[:, 1] = y.flatten()
    position_arr[:, 2] = z.flatten()
    return position_arr


def draw_points(pose, success):
    # Third Party
    from omni.isaac.debug_draw import _debug_draw

    draw = _debug_draw.acquire_debug_draw_interface()
    N = 100
    # if draw.get_num_points() > 0:
    draw.clear_points()
    cpu_pos = pose.position.cpu().numpy()
    b, _ = cpu_pos.shape
    point_list = []
    colors = []
    for i in range(b):
        # get list of points:
        point_list += [(cpu_pos[i, 0], cpu_pos[i, 1], cpu_pos[i, 2])]
        if success[i].item():
            colors += [(0, 1, 0, 0.25)]
        else:
            colors += [(1, 0, 0, 0.25)]
    sizes = [40.0 for _ in range(b)]

    draw.draw_points(point_list, colors, sizes)


def main():
    # assuming obstacles are in objects_path:
    my_world = World(stage_units_in_meters=1.0)
    stage = my_world.stage

    xform = stage.DefinePrim("/World", "Xform")
    stage.SetDefaultPrim(xform)
    stage.DefinePrim("/curobo", "Xform")
    # my_world.stage.SetDefaultPrim(my_world.stage.GetPrimAtPath("/World"))
    stage = my_world.stage
    # stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))

    # Make a target to follow
    target = cuboid.VisualCuboid(
        "/World/target",
        position=np.array([0.5, 0, 0.5]),
        orientation=np.array([0, 1, 0, 0]),
        color=np.array([1.0, 0, 0]),
        size=0.05,
    )

    setup_curobo_logger("warn")
    past_pose = None
    n_obstacle_cuboids = 30
    n_obstacle_mesh = 10

    # warmup curobo instance
    usd_help = UsdHelper()
    target_pose = None

    tensor_args = TensorDeviceType()

    robot_cfg = load_yaml(join_path(get_robot_configs_path(), args.robot))["robot_cfg"]

    j_names = robot_cfg["kinematics"]["cspace"]["joint_names"]
    default_config = robot_cfg["kinematics"]["cspace"]["retract_config"]

    robot, robot_prim_path = add_robot_to_scene(robot_cfg, my_world)

    articulation_controller = robot.get_articulation_controller()

    world_cfg_table = WorldConfig.from_dict(
        load_yaml(join_path(get_world_configs_path(), "collision_table.yml"))
    )
    world_cfg_table.cuboid[0].pose[2] -= 0.002
    world_cfg1 = WorldConfig.from_dict(
        load_yaml(join_path(get_world_configs_path(), "collision_table.yml"))
    ).get_mesh_world()
    world_cfg1.mesh[0].name += "_mesh"
    world_cfg1.mesh[0].pose[2] = -10.5

    world_cfg = WorldConfig(cuboid=world_cfg_table.cuboid, mesh=world_cfg1.mesh)

    ik_config = IKSolverConfig.load_from_robot_config(
        robot_cfg,
        world_cfg,
        rotation_threshold=0.05,
        position_threshold=0.005,
        num_seeds=20,
        self_collision_check=True,
        self_collision_opt=True,
        tensor_args=tensor_args,
        use_cuda_graph=True,
        collision_checker_type=CollisionCheckerType.MESH,
        collision_cache={"obb": n_obstacle_cuboids, "mesh": n_obstacle_mesh},
        # use_fixed_samples=True,
    )
    ik_solver = IKSolver(ik_config)

    print("IKSolver configured.")  # デバッグメッセージ

    # get pose grid:
    position_grid_offset = tensor_args.to_device(get_pose_grid(17, 17, 9, 0.8, 0.8, 0.8))

    # バッチ処理に分割
    batch_size = 100  # 各バッチのサイズ
    total_size = position_grid_offset.shape[0]
    
    # total_size を batch_size の倍数に調整する
    if total_size % batch_size != 0:
        additional_size = batch_size - (total_size % batch_size)
        position_grid_offset = torch.cat([position_grid_offset, position_grid_offset[-1].repeat(additional_size, 1)], dim=0)

    total_size = position_grid_offset.shape[0]
    num_batches = (total_size + batch_size - 1) // batch_size  # バッチの数

    # read current ik pose and warmup?
    fk_state = ik_solver.fk(ik_solver.get_retract_config().view(1, -1))

    all_results = []
    for batch_idx in range(num_batches):
        start_idx = batch_idx * batch_size
        end_idx = min((batch_idx + 1) * batch_size, total_size)
        current_batch = position_grid_offset[start_idx:end_idx]

        current_goal_position = fk_state.ee_pose.position.view(1, -1).repeat(batch_size, 1) + current_batch
        current_goal_quaternion = fk_state.ee_pose.quaternion.view(1, -1).repeat(batch_size, 1)

        goal_pose = Pose(position=current_goal_position, quaternion=current_goal_quaternion)  # goal_poseを定義

        print(f"Starting IK solve batch {batch_idx + 1}/{num_batches}...")  # デバッグメッセージ
        result = ik_solver.solve_batch(goal_pose)
        print(f"IK solve batch {batch_idx + 1}/{num_batches} completed. Success: {torch.any(result.success)}")  # デバッグメッセージ
        print(f"Solve time: {result.solve_time} seconds")  # デバッグメッセージ

        all_results.append(result)
        torch.cuda.empty_cache()  # メモリを解放

    print("Curobo is Ready")
    add_extensions(simulation_app, args.headless_mode)

    usd_help.load_stage(my_world.stage)
    usd_help.add_world_to_stage(world_cfg, base_frame="/World")

    cmd_plan = None
    cmd_idx = 0
    my_world.scene.add_default_ground_plane()
    i = 0
    spheres = None
    while simulation_app.is_running():
        my_world.step(render=True)
        if not my_world.is_playing():
            continue
        step_index = my_world.current_time_step_index
        if step_index <= 2:
            my_world.reset()
            idx_list = [robot.get_dof_index(x) for x in j_names]
            robot.set_joint_positions(default_config, idx_list)

            robot._articulation_view.set_max_efforts(
                values=np.array([5000 for i in range(len(idx_list))]), joint_indices=idx_list
            )
        if step_index < 20:
            continue

        if step_index == 50 or step_index % 500 == 0.0:  # and cmd_plan is None:
            print("Updating world, reading w.r.t.", robot_prim_path)
            obstacles = usd_help.get_obstacles_from_stage(
                # only_paths=[obstacles_path],
                reference_prim_path=robot_prim_path,
                ignore_substring=[
                    robot_prim_path,
                    "/World/target",
                    "/World/defaultGroundPlane",
                    "/curobo",
                ],
            ).get_collision_check_world()
            ik_solver.update_world(obstacles)
            print("Updated World")

        # position and orientation of target virtual cube:
        cube_position, cube_orientation = target.get_world_pose()

        if past_pose is None:
            past_pose = cube_position
        if target_pose is None:
            target_pose = cube_position
        sim_js = robot.get_joints_state()
        sim_js_names = robot.dof_names
        cu_js = JointState(
            position=tensor_args.to_device(sim_js.positions),
            velocity=tensor_args.to_device(sim_js.velocities) * 0.0,
            acceleration=tensor_args.to_device(sim_js.velocities) * 0.0,
            jerk=tensor_args.to_device(sim_js.velocities) * 0.0,
            joint_names=sim_js_names,
        )
        cu_js = cu_js.get_ordered_joint_state(ik_solver.kinematics.joint_names)

        if args.visualize_spheres and step_index % 2 == 0:
            sph_list = ik_solver.kinematics.get_robot_as_spheres(cu_js.position)

            if spheres is None:
                spheres = []
                # create spheres:

                for si, s in enumerate(sph_list[0]):
                    sp = sphere.VisualSphere(
                        prim_path="/curobo/robot_sphere_" + str(si),
                        position=np.ravel(s.position),
                        radius=float(s.radius),
                        color=np.array([0, 0.8, 0.2]),
                    )
                    spheres.append(sp)
            else:
                for si, s in enumerate(sph_list[0]):
                    spheres[si].set_world_pose(position=np.ravel(s.position))
                    spheres[si].set_radius(float(s.radius))
        # print(sim_js.velocities)
        if (
            np.linalg.norm(cube_position - target_pose) > 1e-3
            and np.linalg.norm(past_pose - cube_position) == 0.0
            and np.linalg.norm(sim_js.velocities) < 0.2
        ):
            # Set EE teleop goals, use cube for simple non-vr init:
            ee_translation_goal = cube_position
            ee_orientation_teleop_goal = cube_orientation

            # compute curobo solution:
            ik_goal = Pose(
                position=tensor_args.to_device(ee_translation_goal),
                quaternion=tensor_args.to_device(ee_orientation_teleop_goal),
            )

            # 新しい目標をバッチごとに処理
            for batch_idx in range(num_batches):
                start_idx = batch_idx * batch_size
                end_idx = min((batch_idx + 1) * batch_size, total_size)
                current_batch = position_grid_offset[start_idx:end_idx]

                current_goal_position = ik_goal.position.view(1, -1).repeat(batch_size, 1) + current_batch
                current_goal_quaternion = ik_goal.quaternion.view(1, -1).repeat(batch_size, 1)

                goal_pose = Pose(position=current_goal_position, quaternion=current_goal_quaternion)

                print(f"Starting IK solve batch for new goal {batch_idx + 1}/{num_batches}...")  # デバッグメッセージ
                result = ik_solver.solve_batch(goal_pose)
                print(f"IK solve batch completed for new goal {batch_idx + 1}/{num_batches}. Success: {torch.any(result.success)}")  # デバッグメッセージ
                print(f"Solve time: {result.solve_time} seconds")  # デバッグメッセージ

                succ = torch.any(result.success)
                print(f"IK completed: Poses: {goal_pose.position.shape[0]} Time(s): {result.solve_time}")  # デバッグメッセージ
                # get spheres and flags:
                draw_points(goal_pose, result.success)

                if succ:
                    # get all solutions:

                    cmd_plan = result.js_solution[result.success]
                    # get only joint names that are in both:
                    idx_list = []
                    common_js_names = []
                    for x in sim_js_names:
                        if x in cmd_plan.joint_names:
                            idx_list.append(robot.get_dof_index(x))
                            common_js_names.append(x)

                    cmd_plan = cmd_plan.get_ordered_joint_state(common_js_names)
                    cmd_idx = 0

                else:
                    carb.log_warn("Plan did not converge to a solution.  No action is being taken.")
                target_pose = cube_position
        past_pose = cube_position
        if cmd_plan is not None and step_index % 20 == 0 and True:
            cmd_state = cmd_plan[cmd_idx]

            robot.set_joint_positions(cmd_state.position.cpu().numpy(), idx_list)

            # set desired joint angles obtained from IK:
            # articulation_controller.apply_action(art_action)
            cmd_idx += 1
            if cmd_idx >= len(cmd_plan.position):
                cmd_idx = 0
                cmd_plan = None
                my_world.step(render=True)
                robot.set_joint_positions(default_config, idx_list)
    simulation_app.close()


if __name__ == "__main__":
    main()
