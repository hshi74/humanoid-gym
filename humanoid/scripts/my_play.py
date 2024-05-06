# SPDX-FileCopyrightText: Copyright (c) 2021 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-FileCopyrightText: Copyright (c) 2021 ETH Zurich, Nikita Rudin
# SPDX-License-Identifier: BSD-3-Clause
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Copyright (c) 2024 Beijing RobotEra TECHNOLOGY CO.,LTD. All rights reserved.

import os
import pickle
import time
from datetime import datetime

import cv2
import humanoid.envs  # noqa: F401
import numpy as np
from humanoid import LEGGED_GYM_ROOT_DIR
from humanoid.utils.helpers import export_policy_as_jit, get_args
from humanoid.utils.task_registry import task_registry
from isaacgym import gymapi
from tqdm import tqdm

from toddlerbot.sim.robot import HumanoidRobot
from toddlerbot.visualization.vis_plot import plot_joint_tracking, plot_joint_velocity


def play(args, robot, duration, exp_folder_path):
    env_cfg, train_cfg = task_registry.get_cfgs(name=args.task)
    # override some parameters for testing
    env_cfg.env.num_envs = min(env_cfg.env.num_envs, 1)
    env_cfg.sim.max_gpu_contact_pairs = 2**10
    # env_cfg.terrain.mesh_type = 'trimesh'
    env_cfg.terrain.mesh_type = "plane"
    env_cfg.terrain.num_rows = 5
    env_cfg.terrain.num_cols = 5
    env_cfg.terrain.curriculum = False
    env_cfg.terrain.max_init_terrain_level = 5
    env_cfg.noise.add_noise = True
    env_cfg.domain_rand.push_robots = False
    env_cfg.domain_rand.joint_angle_noise = 0.0
    env_cfg.noise.curriculum = False
    env_cfg.noise.noise_level = 0.5

    train_cfg.seed = 123145
    print("train_cfg.runner_class_name:", train_cfg.runner_class_name)

    # prepare environment
    env, _ = task_registry.make_env(name=args.task, args=args, env_cfg=env_cfg)
    env.set_camera(env_cfg.viewer.pos, env_cfg.viewer.lookat)

    obs = env.get_observations()

    # load policy
    train_cfg.runner.resume = True
    ppo_runner, train_cfg = task_registry.make_alg_runner(
        env=env, name=args.task, args=args, train_cfg=train_cfg
    )
    policy = ppo_runner.get_inference_policy(device=env.device)

    # export policy as a jit module (used to run it from C++)
    if EXPORT_POLICY:
        path = os.path.join(
            LEGGED_GYM_ROOT_DIR,
            "logs",
            train_cfg.runner.experiment_name,
            "exported",
            "policies",
        )
        export_policy_as_jit(ppo_runner.alg.actor_critic, path)
        print("Exported policy as jit script to: ", path)

    robot_index = 0  # which robot is used for logging
    if RENDER:
        camera_properties = gymapi.CameraProperties()
        camera_properties.width = 1920
        camera_properties.height = 1080
        h1 = env.gym.create_camera_sensor(env.envs[0], camera_properties)
        camera_offset = gymapi.Vec3(1, -1, 0.5)
        camera_rotation = gymapi.Quat.from_axis_angle(
            gymapi.Vec3(-0.3, 0.2, 1), np.deg2rad(135)
        )
        actor_handle = env.gym.get_actor_handle(env.envs[0], 0)
        body_handle = env.gym.get_actor_rigid_body_handle(env.envs[0], actor_handle, 0)
        env.gym.attach_camera_to_body(
            h1,
            env.envs[0],
            body_handle,
            gymapi.Transform(camera_offset, camera_rotation),
            gymapi.FOLLOW_POSITION,
        )

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        video_dir = os.path.join(LEGGED_GYM_ROOT_DIR, "videos")
        experiment_dir = os.path.join(
            LEGGED_GYM_ROOT_DIR, "videos", train_cfg.runner.experiment_name
        )
        dir = os.path.join(
            experiment_dir,
            datetime.now().strftime("%b%d_%H-%M-%S") + ".mp4",
        )
        if not os.path.exists(video_dir):
            os.mkdir(video_dir)
        if not os.path.exists(experiment_dir):
            os.mkdir(experiment_dir)
        video = cv2.VideoWriter(dir, fourcc, 50.0, (1920, 1080))

    time_seq_ref = []
    time_seq_dict = {}
    dof_pos_ref_dict = {}
    dof_pos_dict = {}
    dof_vel_dict = {}

    joint_ordering = list(env.cfg.init_state.default_joint_angles.keys())
    default_q = np.array(list(env.cfg.init_state.default_joint_angles.values()))

    time_ref = 0
    obs_dump = []
    for i in tqdm(range(duration)):
        actions = policy(obs.detach())  # * 0.

        if FIX_COMMAND:
            env.commands[:, 0] = 0.0  # 1.0
            env.commands[:, 1] = 0.0
            env.commands[:, 2] = 0.0
            env.commands[:, 3] = 0.0

        obs, critic_obs, rews, dones, infos = env.step(actions.detach())
        obs_dump.append(obs.detach().cpu().numpy())

        if RENDER:
            env.gym.fetch_results(env.sim, True)
            env.gym.step_graphics(env.sim)
            env.gym.render_all_camera_sensors(env.sim)
            img = env.gym.get_camera_image(env.sim, env.envs[0], h1, gymapi.IMAGE_COLOR)
            img = np.reshape(img, (1080, 1920, 4))
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            video.write(img[..., :3])

        time_seq_ref.append(time_ref)
        scaled_actions = actions[robot_index] * env.cfg.control.action_scale
        target_q = default_q + scaled_actions.detach().cpu().numpy()
        for name, pos_ref, pos, vel in zip(
            joint_ordering,
            target_q,
            env.dof_pos[robot_index],
            env.dof_vel[robot_index],
        ):
            if name not in time_seq_dict:
                time_seq_dict[name] = []
                dof_pos_ref_dict[name] = []
                dof_pos_dict[name] = []
                dof_vel_dict[name] = []

            time_seq_dict[name].append(time_ref)
            dof_pos_ref_dict[name].append(pos_ref.item())
            dof_pos_dict[name].append(pos.item())
            dof_vel_dict[name].append(vel.item())

        time_ref += env.dt

    os.makedirs(exp_folder_path, exist_ok=True)

    plot_joint_tracking(
        time_seq_dict,
        time_seq_ref,
        dof_pos_dict,
        dof_pos_ref_dict,
        save_path=exp_folder_path,
        file_suffix="",
        motor_params=robot.config.motor_params,
        colors_dict={
            "dynamixel": "cyan",
            "sunny_sky": "oldlace",
            "mighty_zap": "whitesmoke",
        },
    )

    plot_joint_velocity(
        time_seq_dict,
        dof_vel_dict,
        save_path=exp_folder_path,
        file_suffix="",
        motor_params=robot.config.motor_params,
        colors_dict={
            "dynamixel": "cyan",
            "sunny_sky": "oldlace",
            "mighty_zap": "whitesmoke",
        },
    )

    with open(os.path.join(exp_folder_path, "obs.pkl"), "wb") as f:
        pickle.dump(np.array(obs_dump), f)

    if RENDER:
        video.release()


if __name__ == "__main__":
    EXPORT_POLICY = True
    RENDER = True
    FIX_COMMAND = True

    robot_name = "toddlerbot_legs"
    robot = HumanoidRobot(robot_name)
    duration = 200

    sim_name = "isaac"
    exp_name = f"walk_{robot_name}_{sim_name}"
    time_str = time.strftime("%Y%m%d_%H%M%S")
    exp_folder_path = f"results/{time_str}_{exp_name}"

    args = get_args()
    play(args, robot, duration, exp_folder_path)