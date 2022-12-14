#!/usr/bin/env python3

# Copyright 2021 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import math
import yaml
import json
import time
import copy
import argparse
from rclpy.action import ActionClient

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_system_default

from rclpy.qos import QoSProfile
from rclpy.qos import QoSHistoryPolicy as History
from rclpy.qos import QoSDurabilityPolicy as Durability
from rclpy.qos import QoSReliabilityPolicy as Reliability

from rmf_fleet_msgs.msg import RobotState, Location, PathRequest, \
    DockSummary, RobotMode

import rmf_adapter as adpt
import rmf_adapter.vehicletraits as traits
import rmf_adapter.geometry as geometry

import numpy as np
from pyproj import Transformer

import socketio

from om_aiv_msg.msg import Location, Status#, Locationrmf, Robotstate
from om_aiv_msg.action import Action
from om_aiv_msg.srv import ArclApi

from fastapi import FastAPI
import uvicorn
from typing import Optional
from pydantic import BaseModel

import threading
app = FastAPI()


class Request(BaseModel):
    map_name: Optional[str] = None
    task: Optional[str] = None
    destination: Optional[dict] = None
    data: Optional[dict] = None
    speed_limit: Optional[float] = None
    toggle: Optional[bool] = None


class Response(BaseModel):
    data: Optional[dict] = None
    success: bool
    msg: str


# ------------------------------------------------------------------------------
# Fleet Manager
# ------------------------------------------------------------------------------
class State:
    def __init__(self, state: RobotState = None, destination: Location = None):
        self.state = state
        self.destination = destination
        self.last_path_request = None
        self.last_completed_request = None
        self.mode_teleop = False
    #    self.svy_transformer = Transformer.from_crs('EPSG:4326', 'EPSG:3414')
    #    self.gps_pos = [0, 0]

    # def gps_to_xy(self, gps_json: dict):
    #     svy21_xy = \
    #         self.svy_transformer.transform(gps_json['lat'], gps_json['lon'])
    #     self.gps_pos[0] = svy21_xy[1]
    #     self.gps_pos[1] = svy21_xy[0]

    def is_expected_task_id(self, task_id):
        if self.last_path_request is not None:
            if task_id != self.last_path_request.task_id:
                return False
        return True


class FleetManager(Node):
    def __init__(self, config, nav_path):
        self.debug = False
        self.config = config
        self.fleet_name = self.config["rmf_fleet"]["name"]

        self.gps = False
        self.offset = [0, 0]
        if 'reference_coordinates' in self.config and \
                'offset' in self.config['reference_coordinates']:
            assert len(self.config['reference_coordinates']['offset']) > 1, \
                ('Please ensure that the offset provided is valid.')
        #     self.gps = True
            self.offset = self.config['reference_coordinates']['offset']

        super().__init__(f'{self.fleet_name}_fleet_manager')

        self.robots = {}  # Map robot name to state
        self.docks = {}  # Map dock name to waypoints

        for robot_name, robot_config in self.config["robots"].items():
            self.robots[robot_name] = State()
        assert(len(self.robots) > 0)

        profile = traits.Profile(geometry.make_final_convex_circle(
            self.config['rmf_fleet']['profile']['footprint']),
            geometry.make_final_convex_circle(
                self.config['rmf_fleet']['profile']['vicinity']))
        self.vehicle_traits = traits.VehicleTraits(
            linear=traits.Limits(
                *self.config['rmf_fleet']['limits']['linear']),
            angular=traits.Limits(
                *self.config['rmf_fleet']['limits']['angular']),
            profile=profile)
        self.vehicle_traits.differential.reversible =\
            self.config['rmf_fleet']['reversible']

        self.sio = socketio.Client()

        self.create_subscription(
            Status,
            'Rstate',
            self.robot_state_cb,
            10)
         # Action client to navigate the robot
        self._action_client = ActionClient(self, Action, 'action_server')
        
        transient_qos = QoSProfile(
            history=History.KEEP_LAST,
            depth=1,
            reliability=Reliability.RELIABLE,
            durability=Durability.TRANSIENT_LOCAL)

        # self.create_subscription(
        #     DockSummary,
        #     'dock_summary',
        #     self.dock_summary_cb,
        #     qos_profile=transient_qos)

        # self.path_pub = self.create_publisher(
        #     PathRequest,
        #     'robot_path_requests',
        #     qos_profile=qos_profile_system_default)

        @app.get('/open-rmf/rmf_demos_fm/status/',
                 response_model=Response)
        async def status(robot_name: Optional[str] = None):
            response = {
                'data': {},
                'success': False,
                'msg': ''
            }
            if robot_name is None:
                response['data']['all_robots'] = []
                for robot_name in self.robots:
                    state = self.robots.get(robot_name)
                    if state is None or state.state is None:
                        return response
                    response['data']['all_robots'].append(
                        self.get_robot_state(state, robot_name))
            else:
                state = self.robots.get(robot_name)
                if state is None or state.state is None:
                    return response
                response['data'] = self.get_robot_state(state, robot_name)
            response['success'] = True
            return response

        @app.post('/open-rmf/rmf_demos_fm/start_task/',
                  response_model=Response)
        async def start_process(robot_name: str, cmd_id: int, task: Request):
            response = {'success': False, 'msg': ''}
            if (robot_name not in self.robots or
                    len(task.task) < 1 or
                    task.task not in self.docks):
                return response

            robot = self.robots[robot_name]
            #to be checked

            path_request = PathRequest()
            cur_loc = robot.state.location
            cur_x = cur_loc.x
            cur_y = cur_loc.y
            cur_yaw = cur_loc.theta
            previous_wp = [cur_x, cur_y, cur_yaw]
            target_loc = Location()
            path_request.path.append(cur_loc)
            for wp in self.docks[task.task]:
                target_loc = wp
                path_request.path.append(target_loc)
                previous_wp = [wp.x, wp.y, wp.yaw]

            path_request.fleet_name = self.fleet_name
            path_request.robot_name = robot_name
            path_request.task_id = str(cmd_id)
            #self.path_pub.publish(path_request)

            if self.debug:
                print(f'Sending process request for {robot_name}: {cmd_id}')
            robot.last_path_request = path_request
            robot.destination = target_loc

            response['success'] = True
            return response

        @app.post('/open-rmf/rmf_demos_fm/toggle_action/',
                  response_model=Response)
        async def toggle_teleop(robot_name: str, mode: Request):
            response = {'success': False, 'msg': ''}
            if (robot_name not in self.robots):
                return response
            # Toggle action mode
            self.robots[robot_name].mode_teleop = mode.toggle
            response['success'] = True
            return response

    def robot_state_cb(self, msg):
        if (msg.name in self.robots):
            robot = self.robots[msg.name]
            robot.state = msg

    def dock_summary_cb(self, msg):
        for fleet in msg.docks:
            if(fleet.fleet_name == self.fleet_name):
                for dock in fleet.params:
                    self.docks[dock.start] = dock.path


    def get_robot_state(self, robot: State, robot_name):
        data = {}
        position = [robot.state.location.x, robot.state.location.y]
        angle = robot.state.location.theta
        data['robot_name'] = robot_name
        #-------------------------------------------------------------------------------------
        data['map_name'] = "L1"
        #-------------------------------------------------------------------------------------

        data['position'] =\
            {'x': position[0], 'y': position[1], 'theta': angle}
        data['battery'] = robot.state.state_of_charge
        #duration = format(float(robot.state.time_to_goal),".2f")
        
        #self.get_logger().info(f"duration0000------------------- [{duration}]")
        
        #-------------------------------------------------------------------------------------
        # if (robot.destination is not None
        #         and robot.last_path_request is not None):
        #     destination = robot.destination
        #     cmd_id = int(robot.last_path_request.task_id)
        #     duration = robot.state.time_to_goal
        #     data['destination_arrival'] = {
        #         'cmd_id': cmd_id,
        #         'duration': duration
        #     }
        # else:
        #     data['destination_arrival'] = None    


        # if (robot.destination is not None
        #         and robot.last_path_request is not None):
        #     destination = robot.destination
        #     # remove offset for calculation if using gps coords
        #     if self.gps:
        #         position[0] -= self.offset[0]
        #         position[1] -= self.offset[1]
        #     # calculate arrival estimate
        #     dist_to_target =\
        #         self.disp(position, [destination.x, destination.y])
        #     ori_delta = abs(abs(angle) - abs(destination.yaw))
        #     if ori_delta > np.pi:
        #         ori_delta = ori_delta - (2 * np.pi)
        #     if ori_delta < -np.pi:
        #         ori_delta = (2 * np.pi) + ori_delta
        #     duration = (dist_to_target /
        #                 self.vehicle_traits.linear.nominal_velocity +
        #                 ori_delta /
        #                 self.vehicle_traits.rotational.nominal_velocity)
        #     cmd_id = int(robot.last_path_request.task_id)
        #     data['destination_arrival'] = {
        #         'cmd_id': cmd_id,
        #         'duration': duration
        #     }
        # else:
        #     data['destination_arrival'] = None

        # data['last_completed_request'] = robot.last_completed_request
        # if (
        #     robot.state.mode.mode == RobotMode.MODE_WAITING
        #     or robot.state.mode.mode == RobotMode.MODE_ADAPTER_ERROR
        # ):
        #     # The name of MODE_WAITING is not very intuitive, but the slotcar
        #     # plugin uses it to indicate when another robot is blocking its
        #     # path.
        #     #
        #     # MODE_ADAPTER_ERROR means the robot received a plan that
        #     # didn't make sense, i.e. the plan expected the robot was starting
        #     # very far from its real present location. When that happens we
        #     # should replan, so we'll set replan to true in that case as well.
        #     data['replan'] = True
        # else:
        #     data['replan'] = False
        #-------------------------------------------------------------------------------------
        return data


    def disp(self, A, B):
        return math.sqrt((A[0]-B[0])**2 + (A[1]-B[1])**2)


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
def main(argv=sys.argv):
    # Init rclpy and adapter
    rclpy.init(args=argv)
    adpt.init_rclcpp()
    args_without_ros = rclpy.utilities.remove_ros_args(argv)

    parser = argparse.ArgumentParser(
        prog="fleet_adapter",
        description="Configure and spin up the fleet adapter")
    parser.add_argument("-c", "--config_file", type=str, required=True,
                        help="Path to the config.yaml file")
    parser.add_argument("-n", "--nav_graph", type=str, required=True,
                        help="Path to the nav_graph for this fleet adapter")
    args = parser.parse_args(args_without_ros[1:])
    print(f"Starting fleet manager...")

    with open(args.config_file, "r") as f:
        config = yaml.safe_load(f)

    fleet_manager = FleetManager(config, args.nav_graph)

    spin_thread = threading.Thread(target=rclpy.spin, args=(fleet_manager,))
    spin_thread.start()

    uvicorn.run(app,
                host=config['rmf_fleet']['fleet_manager']['ip'],
                port=config['rmf_fleet']['fleet_manager']['port'],
                log_level='warning')


if __name__ == '__main__':
    main(sys.argv)
