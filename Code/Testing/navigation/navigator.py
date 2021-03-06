import itertools, ps_drone, time, math
from threading import Thread
from collections import deque
import numpy as np
np.seterr(divide='ignore', invalid='ignore')

class Navigator:
    """Navigator interface of an AR Drone 2.0"""

    def __init__(self, drone):
        """Initialize drone navigation variables"""
        # Constants
        print ">>> AR Drone 2.0 Navigator"
        self.__REQ_PACKS = ["altitude", "demo", "gps", "magneto", "raw_measures"]
        self.__SOFT_TURN = 0.1
        self.__HARD_TURN = 0.3
        self.__DEF_SPD   = 0.3
        self.__SAMP_NUM  = 150
        self.__SAMP_TIME = 0.005

        # Default (invalid) field values
        self.__mag_avg = [-14, 13] # Manually calculated
        self.__mag_acc = 6  # Points to record during calibration
        self.__samples = deque(maxlen = self.__SAMP_NUM) # Sample queue
        self.__tar_gps = [0.0, 0.0] # Target's gps coordinate
        self.__tar_dist = 0.0
        self.__tar_angle = 0.0
        self.__stats = {}   # Stats dict

        # Initialize sensor data transmissions
        print ">>> Initializing NavData"
        self.__drone = drone
        self.__drone.useDemoMode(False)
        self.__drone.getNDpackage(self.__REQ_PACKS)
        time.sleep(0.1)

        # Start taking sensor data
        print ">>> Populating data queue..."
        self.__sensors = Thread(target=self.__sensors_collect, args=())
        self.__sensors.daemon = True
        self.__sensors.start()
        time.sleep(self.__SAMP_TIME * self.__SAMP_NUM * 1.5)

        # Get current GPS for "home" location
        print ">>> Obtaining Home coordinate"
        self.__set_stats()
        self.__home = self.__stats["gps"]

        # Done initializing
        print ">>> READY"

    def __sensors_collect(self):
        """Continuously collects sensor data"""
        while True:
            self.__samples.append(self.__get_stats())
            time.sleep(self.__SAMP_TIME)

    def __set_stats(self):
        """Preprocessing of stats queue to reduce variation"""
        # 1-to-1 lists used in for loops
        vel = []
        acc, gyr, gps = [], [], []
        alt, mag, deg = [], [], []
        pry, mfu, out = [], [], []
        stat_names = ["vel", "acc", "gyr", "gps", "alt", "mag", "deg", "pry", "mfu"]
        stat_lists = [ vel,   acc,   gyr,   gps,   alt,   mag,   deg ,  pry ,  mfu ]

        # Build lists to be analyzed
        for item in list(self.__samples):
            for i in range(len(stat_names)):
                stat_lists[i].append(item[stat_names[i]])

        # Remove outliers
        for stat in stat_lists:
            out.append(list(itertools.compress(
                stat, self.__is_outlier(np.array(stat)))))

        # Check that lists are populated
        for i in range(len(stat_lists)):
            if out[i]: stat_lists[i] = out[i]

        # Average the remainder of the lists
        for i in range(len(stat_lists)):
            self.__stats[stat_names[i]] = reduce(
                    lambda x, y: x + y, np.array(stat_lists[i])
                    ) / len(stat_lists[i])

    def __is_outlier(self, points, thresh=3.5):
        """
            Boris Iglewicz and David Hoaglin (1993), "Volume 16: How to Detect and
            Handle Outliers", The ASQC Basic References in Quality Control:
            Statistical Techniques, Edward F. Mykytka, Ph.D., Editor. 
        """
        if len(points.shape) == 1:
            points = points[:,None]
        median = np.median(points, axis=0)
        diff = np.sum((points - median)**2, axis=-1)
        diff = np.sqrt(diff)
        med_abs_deviation = np.median(diff)
        modified_z_score = 0.6745 * diff / med_abs_deviation
    
        return modified_z_score > thresh

    def __get_stats(self):
        """Get stats list with human-readable sensor data."""
        stats = {}
        # Get fresh NavData
        NDC = self.__drone.NavDataCount
        while self.__drone.NavDataCount == NDC: time.sleep(0.01)
    
        # Straightforward data
        stats["acc"] = self.__drone.NavData["raw_measures"][0]
        stats["gyr"] = self.__drone.NavData["raw_measures"][1]
        stats["gps"] = self.__drone.NavData["gps"][:-1] # not using altitude value
        stats["pry"] = self.__drone.NavData["demo"][2] # pitch roll yaw
        stats["mfu"] = self.__drone.NavData["magneto"][6]
        stats["vel"] = self.__drone.NavData["demo"][4] # xyz velocity mm/s

        # Convert altitude to meters
        stats["alt"] = self.__drone.NavData["altitude"][0] / 1000.0
    
        # Turn magnetometer data into heading (degrees)
        stats["mag"] = self.__drone.NavData["magneto"][0][:-1] # not using z value
        for i in range(len(stats["mag"])): stats["mag"][i] -= self.__mag_avg[i]
        stats["deg"] = (360 + (-1 * (math.atan2(
            stats["mag"][1], stats["mag"][0]) * 180) / math.pi)) % 360

        # Set new stats
        return stats
    
    def __calc_distance(self):
        """Calculate distance to target"""
        r = 6371e3  # earth's radius in m
        x = self.__stats["gps"]
        y = self.__tar_gps

        # Convert GPS degrees to radians
        phi1 = math.radians(x[0])
        phi2 = math.radians(y[0])
        dphi = math.radians(y[0] - x[0])
        dlam = math.radians(y[1] - x[1])
    
        # 'Great circle' distance between two GPS coords
        a = math.sin(dphi / 2) * math.sin(dphi / 2)
        a += math.cos(phi1) * math.cos(phi2)
        a *= (math.sin(dlam / 2) * math.sin(dlam / 2))
        self.__tar_dist = 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    def __calc_heading(self):
        """Calculate necessary heading for straight flight to target"""
        x = self.__stats["gps"]
        y = self.__tar_gps

        # Initial heading required for 'Great circle' traversal
        q = math.sin(y[1] - x[1]) * math.cos(y[0])
        p = math.cos(x[0]) * math.sin(y[0])
        p -= math.sin(x[0]) * math.cos(y[0]) * math.cos(y[1] - x[1])
        b = math.atan2(q, p) * 180.0 / math.pi
        self.__tar_angle = (b + 360.0) % 360.0
    
    def __calc_mag(self):
        """Rotates the drone to acquire mag data to use in normalization."""
        mag_x, mag_y = [], []
        for i in range(self.__mag_acc):
            NDC = self.__drone.NavDataCount
            while self.__drone.NavDataCount == NDC: time.sleep(0.01)
            mag = self.__drone.NavData["magneto"]
            mag_x.append(mag[0])
            mag_y.append(mag[1])
            self.__drone.turnAngle(-(360.0 / self.__mag_acc), 1.0)
            self.__drone.hover()
            time.sleep(2)
        self.__mag_avg[0] = np.mean(np.array(mag_x))
        self.__mag_avg[1] = np.mean(np.array(mag_y))
    
    def calibrate_drone(self, *mag):
        """Basic gyroscope and magnetometer recalibration."""
        # Requires 10-15 seconds of hovering flight.
        self.__drone.trim()
        time.sleep(5)
        self.__drone.takeoff()
        time.sleep(5)
        self.__drone.mtrim()
        time.sleep(5)
        if mag:
            self.__calc_mag()
            print self.__mag_avg
        self.__drone.land()

    def get_move(self):
        """Perform calculations to get arguments for a drone move"""
        self.__set_stats()

        # Get angle of required turn
        self.__calc_heading()
        self.__calc_distance()
        angle_diff = self.__drone.angleDiff(
                self.__stats["deg"], self.__tar_angle)

        # If drastic turn is needed, only perform that turn
        if   angle_diff >  10.0:
            move_speed, turn_speed = 0.0,           -self.__HARD_TURN
        elif angle_diff < -10.0:
            move_speed, turn_speed = 0.0,            self.__HARD_TURN
        elif angle_diff > 0:
            move_speed, turn_speed = self.__DEF_SPD,  -self.__SOFT_TURN
        elif angle_diff < 0:
            move_speed, turn_speed = self.__DEF_SPD,   self.__SOFT_TURN
        else:
            move_speed, turn_speed = self.__DEF_SPD,   0.0

        # Return movement list and distance to target
        return ([0.0, move_speed, 0.0, turn_speed], self.__tar_dist)

    def set_target(self, new_target):
        self.__tar_gps = new_target


    # Diagnostic functions
    def get_home(self):
        return self.__home

    def set_home(self, new_home):
        self.__home = new_home

    def get_mag(self):
        self.__set_stats()
        #return self.__drone.NavData["magneto"][0]
        return self.__stats["mfu"]

    def get_deg(self):
        self.__set_stats()
        return self.__stats["deg"]

    def get_all(self):
        self.__set_stats()
        return self.__stats

    def get_vel(self):
        self.__set_stats()
        return self.__stats["vel"]

    def get_acc(self):
        self.__set_stats()
        return self.__stats["acc"]

    def get_demo(self):
        self.__set_stats()
        data = self.__stats["pry"]
        return "pitch: {}\nroll: {}\nyaw: {}".format(
                data[0], data[1], data[2])

    def get_gps(self):
        self.__set_stats()
        return self.__stats["gps"]







