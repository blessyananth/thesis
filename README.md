# thesis

**Packages Required:**
- ros-kinetic-gmapping 
- ros-kinetic-navigation
- python-opencv
- ros-kinetic-teleop-twist-keyboard
- ros-kinetic-map-server
- ros-kinetic-move-base
- ros-kinetic-hector-mapping
- ros-kinetic-rqt-image-view
- turtlebot3_msgs

**Setup turtlebot and remote PC**
  Remote PC : 
    - roscore
  Turtlebot :
    - roslaunch turtlebot3_bringup turtlebot3_robot.launch


**To run autonomous mapping:**

     - On remote PC:
      - roslaunch turtlebot3_navigation move_base.launch
      - roslaunch explore_lite explore.launch
      - rosrun m-explore explore.py

**Wifi Localisation:**  
    
    - Collect
      - On turtlebot : 
    - Localise (Remote PC)
      - Knn : 
      - Wknn :
      
 **Particle filter**
    - AMCL : roslaunch turtlebot3_slam turtlebot3_slam.launch slam_methods:=gmapping
    - Hector Slam : roslaunch turtlebot3_slam turtlebot3_slam.launch slam_methods:=hector
    
 **Full surveilance**
    - 

