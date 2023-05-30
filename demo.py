#!/usr/bin/env python

import rospy

from nav_msgs.msg import OccupancyGrid, MapMetaData, Odometry
from std_msgs.msg import String, Float64
import std_msgs.msg as std_msgs
import std_srvs.srv as std_srvs
from nav_msgs.srv import GetPlan
from geometry_msgs.msg import PoseStamped
import cv2
import tf.transformations as transformations
import math
from geometry_msgs.msg import Pose, Quaternion, TransformStamped, Vector3
import tf2_ros
from gazebo_msgs.srv import SetModelState
import sys
from kobuki_msgs.msg import ButtonEvent
from sensor_msgs.msg import Imu
import numpy as np
import actionlib
import move_base_msgs.msg as move_base_msgs
from pprint import pprint
import tf
from actionlib_msgs.msg import GoalStatus
from nav_msgs.msg import Odometry
from kobuki_msgs.msg import BumperEvent, WheelDropEvent
#import dippykit as dip
from skimage import measure
from skimage.measure import label, regionprops
import random
import datetime


class DemoResetter():
    def __init__(self):
        rospy.init_node('Prototype')
        # self.map_pub = rospy.Publisher("/map", OccupancyGrid, queue_size=1, latch=True)
        self.clear_costmap_srv = None
        self.p_occpu = 0.2  # the probability of occpancy foe each unknown cell (tuning)
        self.pose_entropy_gmapping = 4.377   # an initial value of ~entropy published by gmapping
        self.counter_odom = 0  
        self.poses_hist = np.array([[-99.0,-99.0,0]])
        # self.publishMap()
        self.pub = rospy.Publisher("/mobile_base/commands/reset_odometry", std_msgs.Empty, queue_size=1)
        self.sub_map = rospy.Subscriber('/map', OccupancyGrid, self.callback_map)
        self.sub_odom = rospy.Subscriber('/odom', Odometry, self.callback_odom)
        self.tflistener = tf.TransformListener()
        self.sub_entropy_gmapping = rospy.Subscriber('slam_gmapping/entropy', Float64, self.callback_entropy_gmapping)
        self.client = actionlib.SimpleActionClient('move_base', move_base_msgs.MoveBaseAction)
        print "waiting for server"
        self.client.wait_for_server()
        print "Done!"
        self.stop = False
        self.flag_stuck=0
        self.candidate_idx = 0
        self.Pk_list = [np.array([[0.1,0,0],[0,0.1,0],[0,0,0.05]])]
        self.center_ray = []
        self.result_ray = []
        # rospy.Subscriber("/mobile_base/events/button", ButtonEvent, self.ButtonEventCallback)
        # rospy.Subscriber("/mobile_base/events/wheel_drop", WheelDropEvent, self.WheelDropEventCallback)
        self.initialGoals()
        self.navigate()
        
        rospy.spin()

    def callback_map(self, OccupancyGrid):
        info = OccupancyGrid.info
        data = OccupancyGrid.data
        #rospy.loginfo(rospy.get_caller_id() + 'I heard the map')
        rawmap = np.array(data)
        self.rawmap = rawmap.reshape(info.height, info.width)
        np.savetxt("rawmap.txt", self.rawmap, fmt='%d');
        self.p0 = np.array([info.origin.position.x, info.origin.position.y, info.origin.position.z, info.resolution])
        # p0 is the origin and resolution of the map
        np.savetxt("info.txt", self.p0, delimiter=' ')

    def callback_odom(self, Odometry):
        self.counter_odom = self.counter_odom + 1;
        position_x = Odometry.pose.pose.position.x
        position_y = Odometry.pose.pose.position.y
        position_z = Odometry.pose.pose.position.z
        orientation_z = Odometry.pose.pose.orientation.z
        orientation_w = Odometry.pose.pose.orientation.w
        #heading = math.atan2(2*orientation_w*orientation_z,1-2*orientation_z**2)      
        #print 'odom x: %s' % position_x
        #print 'odom y: %s' % position_y
        self.odom = np.array([position_x, position_y, orientation_z, orientation_w])
        np.savetxt("odom.txt", self.odom, delimiter=' ')
        if(self.counter_odom > 500):
            #record current pose in map frame
            self.counter_odom = 0
            self.getcurrentpose()
            self.poses_hist = np.append(self.poses_hist, self.pose_map[np.newaxis], axis=0)  # a record of history position
            #print self.poses_hist
            


    def callback_entropy_gmapping(self, data):
        #print 'pose entropy estimated by gmapping: ', data.data
        self.pose_entropy_gmapping = data.data



    def initialGoals(self):	
        self.goals = []
        goalA = Pose()
        goalA.position.x = 100
        goalA.position.y = 100
        goalA.orientation.w = 1
        goalB = Pose()
        goalB.position.x = 0
        goalB.position.y = 0
        goalB.orientation.w = 1
        self.goals.append(goalA)
        self.goals.append(goalB)
        print "initial goals!"

    def setupGoals(self, next_x, next_y):
	if abs(self.goals[1].position.x - next_x)<0.02 and abs(self.goals[1].position.y - next_y)<0.02:
		print "Almost same to last goal, may get stuck"
                self.flag_stuck = 1
        goalB = Pose()   # goalB is next frontier
        goalB.position.x = next_x
        goalB.position.y = next_y
        #goalB.orientation.z = self.odom[2]
        goalB.orientation.z = random.uniform(0, 1)
        #goalB.orientation.w = self.odom[3]
        goalB.orientation.w = math.sqrt(1-goalB.orientation.z**2)
        self.goals[1] = goalB 
        #print "exploration goal is set!"



    def covariance(self, P0, Pk, d_d, theta_k):
        #d_d : delta_d (odometry reading: translation)
        #theta_k : theta_k (current heading)
        i=0
        a = 1.5  # tuning here
        V = a*np.array([[0.05**2, 0],[0, math.radians(0.5)**2]])  # odom noise variance, with standard deviation 5cm and 0.5 degree
        
        for dd in d_d:
            theta = theta_k[i]
            Fx =  np.array([[1,0,-dd*math.sin(theta)],[0,1,dd*math.cos(theta)],[0,0,1]])  #jacobian matrix
            Fv = np.array([[math.cos(theta), 0],[math.sin(theta), 0],[0,1]])

            Pk = np.dot(np.dot(Fx, P0), np.transpose(Fx)) + np.dot(np.dot(Fv, V), np.transpose(Fv))
            P0 = Pk
            i=i+1

        return Pk


    def getcurrentpose(self):
        # get pose in map frame
        tf_trans, tf_rot = self.tflistener.lookupTransform('/map', '/odom', rospy.Time(0))
        #theta_tf1 = math.acos(tf_rot[3]) 
        #print '1: ', theta_tf1
        theta_tf = math.atan2(2*tf_rot[3]*tf_rot[2],1-2*tf_rot[2]**2)
        #print '2: ', theta_tf
        theta_odom = math.atan2(2*self.odom[3]*self.odom[2],1-2*self.odom[2]**2) #rad
        x_tf = tf_trans[0]
        y_tf = tf_trans[1]
        T_tf = np.array([[math.cos(theta_tf), -math.sin(theta_tf), x_tf],[math.sin(theta_tf), math.cos(theta_tf), y_tf],[0,0,1]])
        pose_odom = np.array([self.odom[0], self.odom[1], 1])
        positioninmap = T_tf.dot(pose_odom)  
        self.pose_map = positioninmap
        self.pose_map[2] = theta_odom + theta_tf  #nparray[x,y,theta(rad)]
        self.pose_map[2] = self.pose_map[2]*180/math.pi  #nparray[x,y,theta(degree)]
        #print self.pose_map



    def navigate(self):

        while not rospy.is_shutdown():   
            if not self.stop:


                try:
                    
                    np.savetxt("history.txt", self.poses_hist, fmt='%f');
                    self.getcurrentpose()
                    next_goal = self.process(info=self.p0, grid=self.rawmap, odom=self.pose_map)  # in map frame                  
                    self.setupGoals(next_x=next_goal[0],next_y=next_goal[1])
                    print "go to: ", self.goals[1]
                    self.navigateToGoal(goal_pose=self.goals[1])  # go to next frontier
                    self.resetCostmaps()


                except Exception, e:
                    print e
                    pass
                    rospy.sleep(.1)
            else:
                rospy.sleep(.2)


    def resetCostmaps(self):
        if self.clear_costmap_srv is None:
            rospy.wait_for_service('/move_base/clear_costmaps')
            self.clear_costmap_srv = rospy.ServiceProxy('/move_base/clear_costmaps', std_srvs.Empty)
        self.clear_costmap_srv()
        print "reset costmaps"


    def navigateToGoal(self, goal_pose):



        # Create the goal point

        goal = move_base_msgs.MoveBaseGoal()

        goal.target_pose.pose = goal_pose

        goal.target_pose.header.stamp = rospy.Time.now()

        goal.target_pose.header.frame_id = "map"  #odom,  map



        # Send the goal!

        print "sending goal"

        self.client.send_goal(goal)

        print "waiting for result"



        r = rospy.Rate(5)



        start_time = rospy.Time.now()



        keep_waiting = True

        while keep_waiting:

            state = self.client.get_state()

            #print "State: " + str(state)

            if state is not GoalStatus.ACTIVE and state is not GoalStatus.PENDING:

                keep_waiting = False

            else:

                r.sleep()



        state = self.client.get_state()

        if state == GoalStatus.SUCCEEDED:

            return True



        return True



   



    def process(self, info, grid, odom):

        
        print "processing!"
        resolution = info[3]

        origin_x = info[0]

        origin_y = info[1]

        # origin_z = info[2]

        size = grid.shape

        robot_pose_world = odom  # robot initial position in world

        robot_pose_pixel = [0,0]  # robot initial position in grid (pixel in image)

        robot_pose_pixel[0] = (robot_pose_world[0] - origin_x) / resolution

        robot_pose_pixel[1] = (robot_pose_world[1] - origin_y) / resolution

        

        # --------------------------------------------- open cells ---------------------



        thresh_low = 0

        thresh_high = 10

        opencell = ((grid <= thresh_high) & (grid >= thresh_low)) * 1.0  # threshold
            
        # "1" in dst_open are the open cells
        # "0" in dst_open are the unvisited cells and occupied cells

        # detect contours

        contours_open = measure.find_contours(opencell, 0.5)

        contours_open_cell = list()

        for ele in contours_open:

            for cell in ele:

                contours_open_cell.append(cell.tolist())

        # print(contours_open_cell)



        # --------------------------------------------- unvisited cells ---------------------

        thresh_low = -1

        thresh_high = -1

        unknown = ((grid <= thresh_high) & (grid >= thresh_low)) * 1.0  # threshold

        # "1" in unknown are the unvisited cells

        # "0" in unknown are the open cells and occupied cells


        # detect contours

        contours_unvisited = measure.find_contours(unknown, 0.5)

        contours_unvisited_cell = list()

        for ele in contours_unvisited:

            for cell in ele:

                contours_unvisited_cell.append(cell.tolist())

        # print(contours_unvisited_cell)


        # -----------------occupied cells---------------------
        thresh_low = 90
        thresh_high = 100
        occup = ((grid <= thresh_high) & (grid >= thresh_low)) * 1.0



        # ----------------------------------------------------------------

        # frontier detection 

        frontier_cells = [x for x in contours_unvisited_cell if x in contours_open_cell]


        grid_frontier = np.zeros(size)
        for ele in frontier_cells:

            #grid_frontier[math.floor(ele[0]), math.floor(ele[1])] = 1
            grid_frontier[int(ele[0]), int(ele[1])] = 1



        # group them!
        conected_frontier, label_num = measure.label(grid_frontier, return_num=True)
        
        
        manh_dist = []  # stores distances
        cents = []  # stores centers of frontiers
        for region in regionprops(conected_frontier):

            # take regions with large enough areas
            

            if region.area >= 30:  # do not consider small frontier groups
                # the centroid of each valid region
                
                cen_y = region.centroid[0]  # Centroid coordinate tuple (row, col)
                cen_x = region.centroid[1]  # Centroid coordinate tuple (row, col)
                cents.append([cen_x, cen_y])  #cents[col,row]
                manh = abs(cen_x - robot_pose_pixel[0]) + abs(
                    cen_y - robot_pose_pixel[1])  # Manhattan Distance from robot to each frontier center
                manh_dist.append(manh)
      

      	# sort two list: centers of each frontiers according to the man_distance
        cents_sorted = [x for _,x in sorted(zip(manh_dist, cents))]   # a sorted record of all the candidate goals (close-far)
        
        


        # calculate entropy of each candidates
        entropy_shannon = list()  # corresponding Shannon's entropy of each candidate goals
        raycast_num = list()

        for center in cents_sorted:  # in image frame
            cx = center[0] * resolution + origin_x
            cy = center[1] * resolution + origin_y
            c=[cx,cy]
            #print "all calculated centers: ", self.center_ray
            #print "try to find current center: ", c

            if (c in self.center_ray):  # if it has been already calculated
                num_unknown = self.result_ray[self.center_ray.index(c)]
                print "exactly same center, use old results\n\n"
                
            else:
                #print "cast ray based on center: ", center
                num_unknown = self.ray_casting(size, unknown, occup, center, 80)  # ray casting

            raycast_num.append(num_unknown) 

            #Shannon's entropy
            entropy_shannon_i = self.cal_entropy_shannon(num_unknown)  # shannon's entropy at this location
    
            entropy_shannon.append(entropy_shannon_i)    

            #print 'Shannon Entropy = ', entropy_shannon



        print "candidates(pixel col,row): ", cents_sorted
        # pixel to world
        #resolution = self.p0[3] # update map info
        #origin_x = self.p0[0]
        #origin_y = self.p0[1]
        cents_sorted_world = 1*cents_sorted
        for ele in cents_sorted_world:
            ele[0] = ele[0] * resolution + origin_x
            ele[1] = ele[1] * resolution + origin_y
        print "candidates(world xy): ", cents_sorted_world





        # generate trajectory to each frontier
        # Wait for the availability of this service
        rospy.wait_for_service('/move_base/make_plan')

        # Get a proxy to execute the service
        self.make_plan = rospy.ServiceProxy('/move_base/make_plan', GetPlan)

        closure_count_list = []
        candidate_num = 0
        entropy_renyi = []

        Last_P = self.Pk_list[self.candidate_idx]  # read from last time
        self.Pk_list = []

        for center in cents_sorted_world:  # in map frame, for each candidate

            plan_start, plan_goal, plan_tolerance = self.set_plan_goal(center)  #set up a planning goal 
            plan_response = self.make_plan(start=plan_start, goal=plan_goal, tolerance=plan_tolerance) # plan

            plan_len = len(plan_response.plan.poses)  # length of trajectory
            print "plan size: %f" %(plan_len)
            if plan_len == 0:  # fail to make a plan
                print 'cannot generate trajectory'
                entropy_renyi.append(float('Inf'))

                continue
            if plan_len > 2000:  
                print 'too long'
                #continue

            plan_dd, plan_theta, plan_x, plan_y = self.get_plan_trajectory(plan_response)  # get delta_d and theta_k for uncertainty propogation
            #print '\nplan length:',len(plan_dd), len(plan_theta), len(plan_x), len(plan_y)
            plan_pose = np.zeros((len(plan_x), 3))
            plan_pose[:,0] = plan_x.reshape((1,len(plan_x)))  #x
            plan_pose[:,1] = plan_y.reshape((1,len(plan_y)))  #y
            plan_pose[:,2] = plan_theta.reshape((1,len(plan_theta)))  #theta(degree)
            #print plan_pose

            # loop closure detection
            plan_dd_trunc, plan_theta_trunc, closure_count = self.LoopClosure(plan_pose, plan_dd, plan_theta)

            #print 'for this candidate traj, the number of familiar pose is ', closure_count
            closure_count_list.append(closure_count)  # for different candidates, larger values means more familiar poses            
            #print "num of familiar pose for every candidate:\n", closure_count_list

            # covariance
            P0 = Last_P
            if closure_count != 0:
                P0 = np.array([[0.1,0,0],[0,0.1,0],[0,0,0.05]])  # set to initial covariance, because of loop closure
            #print "P0 = ", P0
            Pk = np.array([[0.1,0,0],[0,0.1,0],[0,0,0.05]]) 
            uncertainty = math.sqrt(np.linalg.det(P0))
            #print 'initial ucertainty: ', uncertainty

            # propogate
            Pk = self.covariance( P0, Pk, plan_dd_trunc, plan_theta_trunc)
            self.Pk_list.append(Pk)

            #predicted uncertainty
            uncertainty = math.sqrt(np.linalg.det(Pk))
            #print 'final ucertainty: ', uncertainty

            num_cell_seen = raycast_num[candidate_num]

            # Renyi's entropy
            entropy_renyi_i = self.RenyiEntropy(uncertainty, num_cell_seen) 
            
            entropy_renyi.append(entropy_renyi_i)

            #print 'Renyi Entropy: ', entropy_renyi
            candidate_num = candidate_num + 1

        utility = np.asarray(entropy_shannon) - np.asarray(entropy_renyi)

        # stop condition
        if utility.size==0:
            print "finish!"
            exit()

        print "Utility of each candidate: ", utility
        self.candidate_idx = np.argmax(utility)
        print 'max index:', self.candidate_idx
        #print "Pk of each candidate: ", self.Pk_list

        


        


        if self.flag_stuck==0:
            #next_goal_pixel = cents_sorted_world[0]
            #print "try the closest goal!!"
            next_goal_world = cents_sorted_world[ entropy_shannon.index(max(entropy_shannon)) ] # max info-gain frontier
            next_goal_world = cents_sorted_world[ closure_count_list.index(max(closure_count_list)) ] # zigzag frontier
            next_goal_world = cents_sorted_world[ np.argmax(utility) ] # max utility
            print "try the max-utility goal!!"
        if self.flag_stuck==1:
            #next_goal_pixel = cents_sorted_world[1] # try a further frontier if get stuck into the closest one
            #cent_rand = sample(cents_sorted_world,  1)
             # assume there are more than 4 frontiers
            print "try another goal!!"
            self.flag_stuck=0
            next_goal_world = cents_sorted_world[random.randint(1,2)] # try a random frontier if get stuck into the closest one
            
       
		
        # transform into real world
        #next_goal_world = [0,0]

        #next_goal_world[0] = next_goal_pixel[0] * resolution + origin_x

        #next_goal_world[1] = next_goal_pixel[1] * resolution + origin_y

        #print 'next_goal: ', next_goal_world

        return next_goal_world

    def RenyiEntropy(self, uncertainty, num_cell_seen): 
        # Renyi's entropy
        alpha = 1 + 1/(uncertainty*1.2) # tuning here
        H_renyi_one = (1/(1-alpha)) * math.log((self.p_occpu**alpha + (1-self.p_occpu)**alpha), 2)
        entropy_renyi_i = H_renyi_one * num_cell_seen
        return entropy_renyi_i
        


    def LoopClosure(self, plan_pose, plan_dd, plan_theta):
     
        closure_count = 0 # number of familiar poses in a plan
        loop_closure_index = 0
        i = 0
        loop_closure_pose = plan_pose[0,:] # set initial value to the first pose in a plan traj
        for pose in plan_pose:  #for every pose in a plan
            i = i + 1
            hist = self.poses_hist
            diff_pose = hist - pose
            diff_angle = np.absolute(diff_pose[:,2])
            diff_pose[:,2] = 0  # mute angle
            norm = np.linalg.norm(diff_pose, axis=1)
            hist_close_to_plan = hist[(norm<0.2)*((diff_angle<30)+(diff_angle>330)),:]   # in history poses, which poses are close to current pose in current plan
            #print 'in histroy poses, the close poses are ',hist_close_to_plan
            if len(hist_close_to_plan)>1:  #if this plan pose is familiar
                closure_count = closure_count + 1
                loop_closure_index = i
                # start pose for uncertainty propogatation should be updated
                loop_closure_pose = pose  # the last closure pose
                  
            #print "in the plan :", plan_pose
            #print "find this:",loop_closure_pose
            #print "the index is",loop_closure_index
            #print 'before:',plan_dd
            plan_dd_trunc = plan_dd[loop_closure_index:,:]
            #print 'after,',plan_dd
            plan_theta_trunc = plan_theta[loop_closure_index:,:]

        return plan_dd_trunc, plan_theta_trunc, closure_count



    def cal_entropy_shannon(self, num_unknown):
        # Shannon's entropy of one cell
        H_shannon_one = - ( self.p_occpu*math.log(self.p_occpu,2) + (1-self.p_occpu)*math.log((1-self.p_occpu),2) ) 
        # total Shannon's entrooy
        entropy_shannon = num_unknown * H_shannon_one 
        return entropy_shannon  


    def set_plan_goal(self, center):
        # make a plan and generate a trajectory to the "center" without real action
        
        # set start and goal
        self.getcurrentpose()
        start = PoseStamped()
        start.header.frame_id = "map"
        start.pose.position.x = self.pose_map[0]
        start.pose.position.y = self.pose_map[1]
        start.pose.orientation.z = self.odom[2]
        start.pose.orientation.w = self.odom[3]

        goal = PoseStamped()
        goal.header.frame_id = "map"
        goal.pose.position.x = center[0]
        goal.pose.position.y = center[1]
        goal.pose.orientation.z = self.odom[2]
        goal.pose.orientation.w = self.odom[3]

        # set tolerance
        tolerance = 0.1
        return start, goal, tolerance


    def get_plan_trajectory(self, plan_response):
        #plan_len = len(plan_response.plan.poses)
        plan_x = np.empty((len(plan_response.plan.poses),1))
        plan_y = np.empty((len(plan_response.plan.poses),1))
        #plan_w = np.empty((len(plan_response.plan.poses),1))
        i = 0
        for plan_pose in plan_response.plan.poses:
            plan_x[i] = plan_pose.pose.position.x  #x
            plan_y[i] = plan_pose.pose.position.y  #y
            #plan_w[i] = plan_pose.pose.orientation.w  #w
            i = i+1
        plan_x = plan_x[::5,:] # downsample
        plan_y = plan_y[::5,:]

        #plan_x_next = np.zeros((plan_len,1))
        plan_x_next = np.zeros((len(plan_x),1))
        #plan_x_next[0:plan_len-1] = plan_x[1:plan_len]
        plan_x_next[0:len(plan_x)-1] = plan_x[1:len(plan_x)]
        #plan_x_next[plan_len-1] = plan_x[plan_len-1]
        plan_x_next[len(plan_x)-1] = plan_x[len(plan_x)-1]
        plan_dx = plan_x_next - plan_x
    
        #plan_y_next = np.zeros((plan_len,1))
        #plan_y_next[0:plan_len-1] = plan_y[1:plan_len]
        #plan_y_next[plan_len-1] = plan_y[plan_len-1]
        #plan_dy = plan_y_next - plan_y
        plan_y_next = np.zeros((len(plan_y),1))
        plan_y_next[0:len(plan_y)-1] = plan_y[1:len(plan_y)]
        plan_y_next[len(plan_y)-1] = plan_y[len(plan_y)-1]
        plan_dy = plan_y_next - plan_y


        plan_dd = np.sqrt(plan_dx**2 + plan_dy**2)  # displacement (delta_d)
        #print plan_dd

        plan_theta = np.angle(plan_dx + plan_dy*1j,deg=True)  # current heading (theta_k)
        #print "\n",plan_x,"\n"
        return plan_dd, plan_theta, plan_x, plan_y



    def angle_point(self, p1, p2):  # calculation based on index
        dif_y = -(p1[0] - p2[0])
        dif_x = p1[1] - p2[1]
        ang = np.arctan2(dif_y, dif_x)
        deg = np.rad2deg(ang % (2 * np.pi))
       	deg = deg.astype(int)
        return deg


    def angle_points(self, p1, robot):  # calculation based on index
        dif_yx = p1 - robot
        dif_yx[:,0] = - dif_yx[:,0]
        ang = np.arctan2(dif_yx[:,0], dif_yx[:,1])
        deg = np.rad2deg(ang % (2 * np.pi))
    	deg = deg.astype(int)
        return deg


    def ray_casting(self, size, unknown, occup, cent, laser_range):
        
        #start = timeit.default_timer()
        #print "cent: ", cent
        self.center_ray.append(cent)  # remember this center (col ,row)
        robot = [0, 0]
        robot[0] = int(cent[1])  # robot[row,col]
        robot[1] = int(cent[0])
        
        occup_index = np.transpose(np.nonzero(occup)) # (row,col)
        occpu_disttorobot = np.linalg.norm(occup_index - robot, axis=1)
        outrage_idx = occup_index[occpu_disttorobot>laser_range]
        occup[outrage_idx[:,0], outrage_idx[:,1]] = 0
        
        # -----------------------------------------------------
        # special situation: angle=0
        #exist_right_obstacle = list()  # initialize flag
        #for col in range(robot[1] + 1, size[1]):
        #    if occup[robot[0], col] == 1:
        #        exist_right_obstacle.append(col)  # set the flag (there exist obstacle cell on the right of robot)
        exist_right_obstacle = 0
        if np.count_nonzero(occup[robot[0], robot[1]+1:robot[1]+laser_range]) != 0:
            exist_right_obstacle = 1;

        # the closest "right" obstacle is at (robot[0], exist_right_obstacle)
        # if exist_right_obstacle = 0, then there is no such situation
        # print(exist_right_obstacle)

        occup[robot[0], robot[1]:] = 0  # clear all obstacles on the right

        # -------------group the obstacles!------------------

        conected_obstacle, label_num = measure.label(occup, return_num=True, connectivity=2)
        # print("num of obstacles: %d" %(label_num))
        # conected_obstacle = dip.float_to_im(conected_obstacle/label_num)
        # dip.im_write(conected_frontier, "conected_frontier.tif")
        # plt.imshow(conected_obstacle)
        # plt.show()

        # maybe detect contours here #
        # but in occupancy grid, obstacle is like "contour"

        # -------------coordinates of obstacles------------------
        coords = list()
        for region in regionprops(conected_obstacle):
            coords.append(region.coords)  # a record of coordinates of all occupied cells that belong to the same obstacle
        # coords[0] ... coords[label_num-1]

        # -------------distances of obstacles------------------
        dist = list()  # a record of distance of all occupied cells that belong to the same obstacle
        # dist[0] ... dist[label_num-1]
        dist_obst_i = list()
        for i in range(label_num):  # for every obstacle
            # print('Obstacle No.',i,':')
            for coor in coords[i]:  # take out coordinate of each occupied cell that belongs to the same obstacle
                # print('coordinate:',coor)
                d = np.linalg.norm(coor - robot)
                # print('distance between this cell to robot:',d)
                dist_obst_i.append(d)
            dist.append(dist_obst_i)
            dist_obst_i = list()

        # print(dist[0])
        # print(dist[1])
        # print('----')

        # -------------angles of obstacles------------------
        angle = list()  # a record of angle of all occupied cells that belong to the same obstacle
        # angle[0] ... angle[label_num-1]
        angle_obst_i = list()
        for i in range(label_num):
            # print('Obstacle No.', i, ':')
            for coor in coords[i]:  # take out coordinate of each occupied cell that belongs to the same obstacle
                # print('coordinate:', coor)
                ang = self.angle_point(coor, robot)
                # print('angle: ',ang)
                angle_obst_i.append(ang)
            angle.append(angle_obst_i)
            angle_obst_i = list()  # delete
        # print('angles: ',angle)

        # angle range of obstacle i:
        angle_range = list()
        for i in range(label_num):
            # print('Obstacle No.', i, ':')
            angle_min = min(angle[i])
            angle_max = max(angle[i])
            if exist_right_obstacle != list():  # if I have cleared some obstacles
                if angle_min <= 10:
                    angle_min = 0  # then I have to fill the blank
                if angle_max >= 360 - 10:
                    angle_max = 360  # fill the blank
            # print('min angle:',angle_min)
            # print('max angle:', angle_max)
            # agr = [angle_min,angle_max]
            angle_range.append([angle_min, angle_max])
        #print 'angle_range of obstacle i:',angle_range

        # -------------coordinates of unknown cells------------------
        # unknown_coor = list()  # a record of coordinates of all unknown cells
        unknown_index = list()  # a record of index of all unknown cells
        unknown_coors = np.nonzero(unknown)
        # unknown_coors_y = unknown_coors[0]  # change x/y
        # unknown_coors_x = unknown_coors[1]  # change x/y
        for i in range(unknown_coors[0].size):
            #    unk = [unknown_coors_x[i],unknown_coors_y[i]]
            unknown_index.append([unknown_coors[0][i], unknown_coors[1][i]])
        #    unknown_coor.append(unk)
        # print('coordinates of unknown cells:',unknown_coor)
        # print('index of unknown cells:',unknown_index)



        # -------------distances and angles of unknown cells------------------

        unknown_index = np.array(unknown_index, dtype='int32')  # a record of index of all unknown cells
        index_diff = unknown_index - robot
        dists = np.sqrt(index_diff[:,0]**2 + index_diff[:,1]**2)  # a record of distances of all unknown cells
        unknown_index_inrange = unknown_index[dists < laser_range]
        unknown_dist = dists[dists < laser_range]   # a record of distances of all unknown cells within range
        unknown_angle = self.angle_points(unknown_index_inrange,robot)   # a record of angle of all unknown cells within range

        # -------------cells that can be seen (faster algorithm)-------------
        unknown_seen = np.zeros(size)
        for ele in unknown_index_inrange:
            unknown_seen[ele[0], ele[1]] = 1  # first only use cells that within laser range

        # unknown_seen_img = dip.float_to_im(unknown_seen)
        # dip.imshow(unknown_seen_img)
        # dip.show()



        for i in range(label_num):
            agr = angle_range[i]  # angle range of this obstacle
            #print 'obstacle i=', i
            #print 'range=', agr
            index_anglerange = (unknown_angle >= agr[0]) & (unknown_angle <= agr[1])
            unknown_angle_obs_i = unknown_angle[ index_anglerange ]
            unknown_dist_obs_i = unknown_dist[ index_anglerange ]
            unknown_index_obs_i = unknown_index_inrange[ index_anglerange ]
            dist_closest_obs = list()


            for ele in unknown_angle_obs_i:  # if the angle range of obstacle i is large, then this loop is slow
                #angle_closest_obs = min(angle[i], key=lambda x: abs(x - ele))
                whe = np.where(angle[i]==ele)  # try searching
                if whe[0].size != 0:   #there is occupied cell who has the same angle with the given unknown cell
                	angle_closest_obs = ele
            	else:
                	angle_closest_obs = min(angle[i], key=lambda x: abs(x - ele))##
                ind = angle[i].index(angle_closest_obs)
                dist_closest_obs.append(dist[i][ind])

            unknown_behind = unknown_index_obs_i[unknown_dist_obs_i > dist_closest_obs]


            for ele in unknown_behind:
                unknown_seen[ele[0], ele[1]] = 0

        result = np.sum(unknown_seen == 1)      
        self.result_ray.append(result)
        return result








if __name__ == '__main__':

    try:
	DemoResetter()

    except rospy.ROSInterruptException:

        
        rospy.loginfo("exception")
