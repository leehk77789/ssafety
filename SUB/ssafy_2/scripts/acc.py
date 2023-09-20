#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import rospy
import os
import rospkg
from math import cos, sin, pi, sqrt, pow, atan2
from geometry_msgs.msg import Point, PoseWithCovarianceStamped
from nav_msgs.msg import Odometry, Path
from morai_msgs.msg import CtrlCmd, EgoVehicleStatus, ObjectStatusList, GetTrafficLightStatus
import numpy as np
import tf
from tf.transformations import euler_from_quaternion,quaternion_from_euler

# ACC(Adaptive Cruise Control): 차량 경로상의 장애물을 탐색하여 탐색된 차량과의 속도 차이 계산

# 노드 실행 순서 
# 0. 필수 학습 지식
# 1. subscriber, publisher 선언
# 2. 속도 비례 Look Ahead Distance 값 설정
# 3. 좌표 변환 행렬 생성
# 4. Steering 각도 계산
# 5. PID 제어 생성
# 6. 도로의 곡률 계산
# 7. 곡률 기반 속도 계획
# 8. 경로상의 장애물 유무 확인 (차량, 사람, 정지선 신호)
# 9. 장애물과의 속도와 거리 차이를 이용하여 ACC 를 진행 목표 속도를 설정
# 10. 제어입력 메세지 Publish

#TODO: (0) 필수 학습 지식
'''
# 앞선 NPC 차량 인식하여 일정한 간격을 유지하여 주행하도록 하는 Car-Following 알고리즘
# 전방 차량 "위치 좌표"와 "속도 값"을 이용하여 "상대 거리"와 "상대 속도"를 측정
'''
class pure_pursuit :
    def __init__(self):
        rospy.init_node('pure_pursuit', anonymous=True)

        #TODO: (1) subscriber, publisher 선언 - global/local path & Ego Status & Object(NPC)
        rospy.Subscriber("/global_path", Path, self.global_path_callback)
        rospy.Subscriber("/local_path", Path, self.path_callback)
        rospy.Subscriber("/odom", Odometry, self.odom_callback)
        rospy.Subscriber("/Ego_topic" ,EgoVehicleStatus,self.status_callback)
        rospy.Subscriber("/Object_topic", ObjectStatusList,self.object_info_callback)
        # rospy.Subscriber("/GetTrafficLightStatus", GetTrafficLightStatus, self.get_traffic_callback)  # 교통 상황 - 신호등
        self.ctrl_cmd_pub = rospy.Publisher('ctrl_cmd',CtrlCmd, queue_size=1)


        self.ctrl_cmd_msg = CtrlCmd()
        self.ctrl_cmd_msg.longlCmdType = 1

        self.is_path = False
        self.is_odom = False
        self.is_status = False
        self.is_global_path = False

        self.is_look_forward_point = False
        
        self.forward_point = Point()
        self.current_postion = Point()

        #========================================================#
        # 교통 상황 - 신호등 데이터
        self.is_get_traffic = False
        #========================================================#

        self.vehicle_length = 2.6
        self.lfd = 8
        self.min_lfd=5
        self.max_lfd=30
        self.lfd_gain = 0.78
        self.target_velocity = 60

        # 종 방향 속도 제어 - PID 제어
        self.pid = pidControl()
        # 장애물과의 거리로 속도 조절 - ACC
        self.adaptive_cruise_control = AdaptiveCruiseControl(velocity_gain = 0.5, distance_gain = 1, time_gap = 0.8, vehicle_length = 2.7, current_postion = self.current_postion)
        # 횡 방향 속도 계획
        self.vel_planning = velocityPlanning(self.target_velocity/3.6, 0.15)

        while True:
            if self.is_global_path == True:
                self.velocity_list = self.vel_planning.curvedBaseVelocity(self.global_path, 50)
                break
            else:
                rospy.loginfo('Waiting global path data')

        rate = rospy.Rate(30) # 30hz
        while not rospy.is_shutdown():

            if self.is_path == True and self.is_odom == True and self.is_status == True:

                # global_obj,local_obj
                result = self.calc_vaild_obj([self.current_postion.x, self.current_postion.y, self.vehicle_yaw], self.object_data)
                
                global_npc_info = result[0]
                local_npc_info = result[1]
                global_ped_info = result[2]
                local_ped_info = result[3]
                global_obs_info = result[4]
                local_obs_info = result[5]
                
                self.current_waypoint = self.get_current_waypoint([self.current_postion.x, self.current_postion.y], self.global_path)
                self.target_velocity = self.velocity_list[self.current_waypoint] * 3.6

                # 조향각 계산 - steering = theta
                steering = self.calc_pure_pursuit()
                if self.is_look_forward_point :
                    self.ctrl_cmd_msg.steering = steering
                else : 
                    rospy.loginfo("no found forward point")
                    self.ctrl_cmd_msg.steering = 0.0

                self.adaptive_cruise_control.check_object(self.path ,global_npc_info, local_npc_info
                                                                    ,global_ped_info, local_ped_info
                                                                    ,global_obs_info, local_obs_info)
                self.target_velocity = self.adaptive_cruise_control.get_target_velocity(local_npc_info, local_ped_info, local_obs_info,
                                                                                        self.status_msg.velocity.x, self.target_velocity/3.6)

                output = self.pid.pid(self.target_velocity, self.status_msg.velocity.x*3.6)

                if output > 0.0:
                    self.ctrl_cmd_msg.accel = output
                    self.ctrl_cmd_msg.brake = 0.0
                else:
                    self.ctrl_cmd_msg.accel = 0.0
                    self.ctrl_cmd_msg.brake = -output

                #TODO: (10) 제어입력 메세지 Publish - ctrl_cmd
                self.ctrl_cmd_pub.publish(self.ctrl_cmd_msg)

            rate.sleep()

    def path_callback(self,msg):
        self.is_path = True
        self.path = msg

    def odom_callback(self,msg):
        self.is_odom = True
        odom_quaternion = (msg.pose.pose.orientation.x, msg.pose.pose.orientation.y, msg.pose.pose.orientation.z, msg.pose.pose.orientation.w)
        _,_,self.vehicle_yaw = euler_from_quaternion(odom_quaternion)
        self.current_postion.x = msg.pose.pose.position.x
        self.current_postion.y = msg.pose.pose.position.y

    def status_callback(self,msg): ## Vehicl Status Subscriber 
        self.is_status = True
        self.status_msg = msg    

    def global_path_callback(self,msg):
        self.global_path = msg
        self.is_global_path = True

    def object_info_callback(self,data): ## Object information Subscriber
        self.is_object_info = True
        self.object_data = data 

    #========================================================#
    # 교통 상황 - 신호등 데이터
    # def get_traffic_callback(self, msg):
    #     self.traffic = msg
    #     self.is_get_traffic = True
    #     os.system('clear')
    #     rospy.loginfo('-------------------- Traffic Light Vehicle --------------------')
    #     rospy.loginfo("Traffic Light Idx    : {}".format(msg.trafficLightIndex))
    #     rospy.loginfo("Traffic Light Status : {}".format(msg.trafficLightStatus))
    #     rospy.loginfo("Traffic Light Type   : {}".format(msg.trafficLightType))
    #========================================================#

    def get_current_waypoint(self,ego_status,global_path):
        min_dist = float('inf')
        currnet_waypoint = -1

        ego_pose_x = ego_status[0]
        ego_pose_y = ego_status[1]

        for i,pose in enumerate(global_path.poses):
            dx = ego_pose_x - pose.pose.position.x
            dy = ego_pose_y - pose.pose.position.y

            dist = sqrt(pow(dx,2)+pow(dy,2))
            if min_dist > dist:
                min_dist = dist
                currnet_waypoint = i
        return currnet_waypoint

    def calc_vaild_obj(self,status_msg,object_data):
        
        self.all_object = object_data        
        ego_pose_x = status_msg[0]
        ego_pose_y = status_msg[1]
        ego_heading = status_msg[2]
        
        global_npc_info = []
        local_npc_info  = []
        global_ped_info = []
        local_ped_info  = []
        global_obs_info = []
        local_obs_info  = []

        num_of_object = self.all_object.num_of_npcs + self.all_object.num_of_obstacle + self.all_object.num_of_pedestrian        


        if num_of_object > 0:
            #translation
            tmp_theta = ego_heading
            tmp_translation = [ego_pose_x, ego_pose_y]
            tmp_t=np.array([    [cos(tmp_theta), -sin(tmp_theta), tmp_translation[0]],
                                [sin(tmp_theta),  cos(tmp_theta), tmp_translation[1]],
                                [0             ,               0,                  1]   ])
            tmp_det_t=np.array([[tmp_t[0][0], tmp_t[1][0], -(tmp_t[0][0] * tmp_translation[0] + tmp_t[1][0]*tmp_translation[1])],
                                [tmp_t[0][1], tmp_t[1][1], -(tmp_t[0][1] * tmp_translation[0] + tmp_t[1][1]*tmp_translation[1])],
                                [          0,           0,                                                                    1]    ])

            # npc vehicle ranslation - NPC 차량
            for npc_list in self.all_object.npc_list:
                global_result = np.array([[npc_list.position.x], [npc_list.position.y], [1]])
                local_result = tmp_det_t.dot(global_result)
                if local_result[0][0] > 0:
                    global_npc_info.append([npc_list.type, npc_list.position.x, npc_list.position.y, npc_list.velocity.x])
                    local_npc_info.append([npc_list.type, local_result[0][0], local_result[1][0], npc_list.velocity.x])

            #ped translation - 보행자
            for ped_list in self.all_object.pedestrian_list:
                global_result = np.array([[ped_list.position.x], [ped_list.position.y], [1]])
                local_result = tmp_det_t.dot(global_result)
                if local_result[0][0] > 0:
                    global_ped_info.append([ped_list.type, ped_list.position.x, ped_list.position.y, ped_list.velocity.x])
                    local_ped_info.append([ped_list.type, local_result[0][0], local_result[1][0], ped_list.velocity.x])


                if local_result[0][0]> 0 :
                    global_obs_info.append([obs_list.type,obs_list.position.x,obs_list.position.y,obs_list.velocity.x])
                    local_obs_info.append([obs_list.type,local_result[0][0],local_result[1][0],obs_list.velocity.x])

            # obs translation - 정지선 라인
            for obs_list in self.all_object.obstacle_list:
                global_result = np.array([[obs_list.position.x], [obs_list.position.y], [1]])
                local_result = tmp_det_t.dot(global_result)
                if local_result[0][0] > 0:
                    global_obs_info.append([obs_list.type, obs_list.position.x, obs_list.position.y, obs_list.velocity.x])
                    local_obs_info.append([obs_list.type, local_result[0][0], local_result[1][0], obs_list.velocity.x])

        return global_npc_info, local_npc_info, global_ped_info, local_ped_info, global_obs_info, local_obs_info

    def calc_pure_pursuit(self,):

        #TODO: (2) 속도 비례 Look Ahead Distance 값 설정
        '''
        # 차량 속도에 비례하여 전방주시거리(Look Forward Distance) 가 변하는 수식을 구현 합니다.
        # 이때 'self.lfd' 값은 최소와 최대 값을 넘어서는 안됩니다.
        # "self.min_lfd","self.max_lfd", "self.lfd_gain" 을 미리 정의합니다.
        # 최소 최대 전방주시거리(Look Forward Distance) 값과 속도에 비례한 lfd_gain 값을 직접 변경해 볼 수 있습니다.
        # 초기 정의한 변수 들의 값을 변경하며 속도에 비례해서 전방주시거리 가 변하는 advanced_purepursuit 예제를 완성하세요.
        '''
        self.lfd = (self.status_msg.velocity.x) * self.lfd_gain

        rospy.loginfo(self.lfd)

        vehicle_position=self.current_postion
        self.is_look_forward_point= False

        translation = [vehicle_position.x, vehicle_position.y]

        #TODO: (3) 좌표 변환 행렬 생성
        '''
        # Pure Pursuit 알고리즘을 실행 하기 위해서 차량 기준의 좌표계가 필요합니다.
        # Path 데이터를 현재 차량 기준 좌표계로 좌표 변환이 필요합니다.
        # 좌표 변환을 위한 좌표 변환 행렬을 작성합니다.
        # Path 데이터를 차량 기준 좌표 계로 변환 후 Pure Pursuit 알고리즘 중 전방주시거리(Look Forward Distance) 와 가장 가까운 Path Point 를 찾습니다.
        # 전방주시거리(Look Forward Distance) 와 가장 가까운 Path Point 를 이용하여 조향 각도를 계산하게 됩니다.
        # 좌표 변환 행렬을 이용해 Path 데이터를 차량 기준 좌표 계로 바꾸는 반복 문을 작성 한 뒤
        # 전방주시거리(Look Forward Distance) 와 가장 가까운 Path Point 를 계산하는 로직을 작성 하세요.
        '''
        trans_matrix = np.array([   [cos(self.vehicle_yaw), -sin(self.vehicle_yaw),translation[0]],
                                    [sin(self.vehicle_yaw),cos(self.vehicle_yaw),translation[1]],
                                    [0                    ,0                    ,1            ]])

        det_trans_matrix = np.linalg.inv(trans_matrix)

        for num,i in enumerate(self.path.poses) :
            path_point = i.pose.position

            global_path_point = [path_point.x,path_point.y,1]
            local_path_point = det_trans_matrix.dot(global_path_point)    

            if local_path_point[0]>0 :
                dis = sqrt(pow(local_path_point[0],2)+pow(local_path_point[1],2))
                if dis >= self.lfd :
                    self.forward_point = path_point
                    self.is_look_forward_point = True
                    break

        #TODO: (4) Steering 각도 계산
        '''
        # 제어 입력을 위한 Steering 각도를 계산 합니다.
        # theta 는 전방주시거리(Look Forward Distance) 와 가장 가까운 Path Point 좌표의 각도를 계산 합니다.
        # Steering 각도는 Pure Pursuit 알고리즘의 각도 계산 수식을 적용하여 조향 각도를 계산합니다.
        '''
        theta = atan2(local_path_point[1],local_path_point[0])
        # steering = atan2(2*self.vehicle_length*sin(theta), self.lfd)
        steering = theta / pi

        return steering

class pidControl:
    def __init__(self):
        self.p_gain = 0.3
        self.i_gain = 0.00
        self.d_gain = 0.03
        self.prev_error = 0
        self.i_control = 0
        self.controlTime = 0.02

    def pid(self, target_vel, current_vel):
        #TODO: (5) PID 제어 생성 - 종방향 제어

        # 현재와 목표 속도 갭
        error = target_vel - current_vel

        p_control = self.p_gain * error         # p 제어
        self.i_control += error * self.controlTime    # 누적 속도(i) 차이 - 에러 값들의 총합
        d_control = self.d_gain * (error - self.prev_error) / self.controlTime  # 미분(d) 값

        # PID 제어 반영
        output = p_control + self.i_gain*self.i_control + d_control
        self.prev_error = error

        return output


class velocityPlanning:
    def __init__ (self,car_max_speed, road_friciton):
        self.car_max_speed = car_max_speed
        self.road_friction = road_friciton

    def curvedBaseVelocity(self, gloabl_path, point_num):
        out_vel_plan = []

        for i in range(0,point_num):
            out_vel_plan.append(self.car_max_speed)

        for i in range(point_num, len(gloabl_path.poses) - point_num):
            x_list = []
            y_list = []
            for box in range(-point_num, point_num):
                x = gloabl_path.poses[i+box].pose.position.x
                y = gloabl_path.poses[i+box].pose.position.y
                x_list.append([-2*x, -2*y ,1])
                y_list.append((-x*x) - (y*y))

            #TODO: (6) 도로의 곡률 계산 - 최소 자승법 이용하여 곡률 반지름 "r" 계산
            #========================================================#
            # A = np.array(x_list)
            # B = np.array(y_list)
            # a, b, c = np.dot(np.linalg.pinv(A), B)            
            # 최소 자승법 안정성 증가 - 아래 코드
            x_matrix = np.array(x_list)
            y_matrix = np.array(y_list)
            x_trans = x_matrix.T

            a_matrix = np.linalg.inv(x_trans.dot(x_matrix)).dot(x_trans).dot(y_matrix)
            a = a_matrix[0]
            b = a_matrix[1]
            c = a_matrix[2]

            r = sqrt(a*a + b*b - c)


            #TODO: (7) 곡률 기반 속도 계획
            '''
            # 계산 한 곡률 반경을 이용하여 최고 속도를 계산합니다.
            # 평평한 도로인 경우 최대 속도를 계산합니다. 
            # 곡률 반경 x 중력가속도 x 도로의 마찰 계수 계산 값의 제곱근이 됩니다.
            v_max = 

            '''
            v_max = sqrt(r * 9.8 * self.road_friction)

            if v_max > self.car_max_speed:
                v_max = self.car_max_speed
            out_vel_plan.append(v_max)

        for i in range(len(gloabl_path.poses) - point_num, len(gloabl_path.poses) - 10):
            out_vel_plan.append(30)

        for i in range(len(gloabl_path.poses) - 10, len(gloabl_path.poses)):
            out_vel_plan.append(0)

        return out_vel_plan

class AdaptiveCruiseControl:
    def __init__(self, velocity_gain, distance_gain, time_gap, vehicle_length, current_postion):
        self.npc_vehicle = [False, 0]
        self.object = [False, 0]
        self.Person = [False, 0]
        self.velocity_gain = velocity_gain      # 속도 이득 값
        self.distance_gain = distance_gain      # 거리 이득 값
        self.time_gap = time_gap                # 시간 차이 - two-second-rules(2초)
        self.vehicle_length = vehicle_length    # 차량 길이 - default Distance
        # 230920 KMJ 추가 - 현재 위치 추가
        self.current_postion = current_postion

        self.object_type = None
        self.object_distance = 0
        self.object_velocity = 0

    def check_object(self,ref_path, global_npc_info, local_npc_info, 
                                    global_ped_info, local_ped_info, 
                                    global_obs_info, local_obs_info):
        #TODO: (8) 경로상의 장애물 유무 확인 (차량, 사람, 정지선 신호)
        # 주행 경로 상의 장애물 유무 파악, self.object의 변수 첫 번째 값: True, 두 번재 값: List
        # 장애물 여부는 경로 기준 2.5m 안쪽에 있을 경우로 판단하고, 여러 개일 경우 가장 가까운 장애물 정보를 가지도록

        # 주행 경로 상 보행자 유무 파악
        min_rel_distance=float('inf')
        if len(global_ped_info) > 0:
            for i in range(len(global_ped_info)):
                for path in ref_path.poses:
                     # type=0 [pedestrian]
                    if global_ped_info[i][0] == 0:
                        dis = sqrt((path.pose.position.x - global_ped_info[i][1])**2 + (path.pose.position.y - global_ped_info[i][2])**2)
                        if dis < 2.35:
                            rel_distance = sqrt((self.current_postion.x - global_ped_info[i][1])**2 + (self.current_postion.y - global_ped_info[i][2])**2)
                            if rel_distance < min_rel_distance:
                                min_rel_distance = rel_distance
                                self.Person = [True, i]

        # 주행 경로 상 NPC 차량 유무 파악
        if len(global_npc_info) > 0:
            for i in range(len(global_npc_info)):
                for path in ref_path.poses:
                    # type=1 [npc_vehicle]
                    if global_npc_info[i][0] == 1:
                        dis = sqrt((path.pose.position.x - global_npc_info[i][1])**2 + (path.pose.position.y - global_npc_info[i][2])**2)
                        if dis < 2.35:
                            rel_distance = sqrt((self.current_postion.x - global_npc_info[i][1])**2 + (self.current_postion.y - global_npc_info[i][2])**2)
                            if rel_distance < min_rel_distance:
                                min_rel_distance = rel_distance
                                self.npc_vehicle = [True, i]

        # 주행 경로 상 Obstacle 유무 파악
        # ACC 는 정적 장애물에 대해서는 올바르게 작동하지 않는다 - 속도 0인 정적 장애물 바로 뒤에 정지하게 됨
        if len(global_obs_info) > 0:
            for i in range(len(global_obs_info)):
                for path in ref_path.poses:
                    # type=1 [obstacle]
                    if global_obs_info[i][0] == 2:
                        dis = sqrt((path.pose.position.x - global_obs_info[i][1])**2 + (path.pose.position.y - global_obs_info[i][2])**2)
                        if dis < 2.35:
                            rel_distance = sqrt((self.current_postion.x - global_obs_info[i][1])**2 + (self.current_postion.y - global_obs_info[i][2])**2)
                            if rel_distance < min_rel_distance:
                                min_rel_distance = rel_distance
                                self.object = [True, i]



    def get_target_velocity(self, local_npc_info, local_ped_info, local_obs_info, ego_vel, target_vel): 
        #TODO: (9) 장애물과의 속도와 거리 차이를 이용하여 ACC 를 진행 목표 속도를 설정
        out_vel =  target_vel
        default_space = 8
        time_gap = self.time_gap
        v_gain = self.velocity_gain
        x_errgain = self.distance_gain

        if self.npc_vehicle[0] and len(local_npc_info) != 0: #ACC ON_vehicle   
            print("ACC ON NPC_Vehicle")         
            front_vehicle = [local_npc_info[self.npc_vehicle[1]][1], local_npc_info[self.npc_vehicle[1]][2], local_npc_info[self.npc_vehicle[1]][3]]
            
            dis_safe = ego_vel * time_gap + default_space
            dis_rel = sqrt(pow(front_vehicle[0],2) + pow(front_vehicle[1],2))            
            vel_rel=((front_vehicle[2] / 3.6) - ego_vel)                        
            acceleration = vel_rel * v_gain - x_errgain * (dis_safe - dis_rel)

            out_vel = ego_vel + acceleration

        if self.Person[0] and len(local_ped_info) != 0: #ACC ON_Pedestrian
            print("ACC ON Pedestrian")
            Pedestrian = [local_ped_info[self.Person[1]][1], local_ped_info[self.Person[1]][2], local_ped_info[self.Person[1]][3]]
            
            dis_safe = ego_vel* time_gap + default_space
            dis_rel = sqrt(pow(Pedestrian[0],2) + pow(Pedestrian[1],2))            
            vel_rel = (Pedestrian[2] - ego_vel)              
            acceleration = vel_rel * v_gain - x_errgain * (dis_safe - dis_rel)    

            out_vel = ego_vel + acceleration
   
        if self.object[0] and len(local_obs_info) != 0: #ACC ON_obstacle     
            print("ACC ON Obstacle")                    
            Obstacle = [local_obs_info[self.object[1]][1], local_obs_info[self.object[1]][2], local_obs_info[self.object[1]][3]]
            
            dis_safe = ego_vel* time_gap + default_space
            dis_rel = sqrt(pow(Obstacle[0],2) + pow(Obstacle[1],2))            
            vel_rel = (Obstacle[2] - ego_vel)
            acceleration = vel_rel * v_gain - x_errgain * (dis_safe - dis_rel)    

            out_vel = ego_vel + acceleration           

        return out_vel * 3.6


if __name__ == '__main__':
    try:
        test_track=pure_pursuit()
    except rospy.ROSInterruptException:
        pass
