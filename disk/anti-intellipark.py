#!/usr/bin/env python3
#
# Author: Peter Maloney
#
# Requires python 3.2 or newer. 3.1.2 (Ubuntu 10.04) does not work .
#
# Trying to prevent the head from parking on the disks, to reduce wear. 
#
# This is useful for:
#     WD Green
#     Seagate Barracuda ST3000DM001-9YN166
#     Seagate Barracuda ST3000DM001-1CH166
#     Seagate Barracuda ST2000DM001-1CH164
#
#
# goals:
#    -compatible with FreeBSD and Linux (requires ports: bash, facter, flock, gdd)
#    - only run on disks with known problem
#        dynamically detect problem... if Load_Cycle_Count is more than 50x the Power_Cycle_Count, then it's a problem disk
#    TODO: run by cron? by init?
#    - use flock to make sure more instances don't run
#    - when there are read errors, verify again:
#        -disk exists
#        -it is a problem disk
#    - every once in a while, look for new disks
#    - validate that dependencies are found
#    - read the middle of the disk instead of $RANDOM? it would probably lower the seeking (2 runs in a row will not seek, and runs while other IO not from this script will interfere a bit less since the middle is probably closer to the other requests)
#    DONE- make it quit so cron can restart it if the script itself is modified
#
# Licensed GNU GPLv2; if you did not recieve a copy of the license, get one at http://www.gnu.org/licenses/gpl-2.0.html

import sys
import os
import subprocess
import stat
import re
import time
import argparse
import fcntl

################################################################################
# error codes
################################################################################

e_missing_action=1
e_bad_argparse=2 # defined by argparse, not used here
e_missing_command=3

# support python 3.2 which apparently has no subprocess.DEVNULL... using PIPE and then just not using communicate() seems to work fine, maybe just wasting some RAM for buffers, or some small CPU
subprocess_devnull = None
if hasattr(subprocess, "DEVNULL"):
    subprocess_devnull = subprocess.DEVNULL
else:
    subprocess_devnull = subprocess.PIPE

def facter(key):
    p = subprocess.Popen(["facter", str(key)], 
        stdout=subprocess.PIPE, stderr=subprocess_devnull)
    p.wait()
    if( p.returncode == 0 ):
        out, err = p.communicate()
        return out.decode("utf-8").splitlines()[0]
    else:
        raise Exception("facter command failed; key = \"%s\"" % (key))

def debug(text):
    if not debug_enabled:
        return
    print("DEBUG: %s" % (text))
    sys.stdout.flush()
    
def warn(text):
    print("WARNING: %s" % (text))
    sys.stdout.flush()
    
def info(text):
    print("INFO: %s" % (text))
    sys.stdout.flush()
    
################################################################################
# Command line arguments
################################################################################

parser = argparse.ArgumentParser(description="Scan for intellipark disks and produce IO at regular intervals to prevent parking.")
actions=["install","stop","list","run"] # "test" action is purposely undocumented
parser.add_argument('action', metavar='action', type=str,
                   help='action to run %s' % actions, choices=actions)
parser.add_argument('-d', '--debug', dest='debug', action='store_const',
                   const=True, default=False,
                   help='enable debug level output')

args = parser.parse_args()

debug_enabled = args.debug
action = args.action

################################################################################
# functions
################################################################################

# http://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
def which(cmd):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(cmd)
    if fpath:
        if is_exe(cmd):
            return cmd
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, cmd)
            if is_exe(exe_file):
                return exe_file

    return None

# expression = regex or string to search for
# lines = a string with many lines, or a list
# regex = False is like -F
# keep = False is like -v
# only = keep only the part of the line that matches; each match on the same line becomes a new line in the returned list
# returns a list of matches
def grep(expression, lines, regex=True, keep=True, only=False, ignore_case=False):
    results = []
    
    #debug("grep expression = \"%s\", regex = %s, keep = %s, only = %s" % (expression, regex, keep, only))
    if( regex ):
        p = re.compile(expression)
        
    if( isinstance(lines, str) ):
        lines = lines.splitlines()
    for line in lines:
        #debug("grep line = \"%s\"" % (line))
        if( regex ):
            if( ignore_case ):
                m = p.search(line, re.IGNORECASE)
            else:
                m = p.search(line)
            #debug("grep type(m) = %s" % type(m))
            if keep == (m != None):
                if( only ):
                    results += [m.group(0)]
                else:
                    results += [line]
        else:
            if keep == (expression in line):
                results += [line]
            
    if( len(results) == 0 ):
        return None
    return results

# just a grep test
#print( grep("/dev/mapper/", ["/dev/sda", "/dev/mapper/blah"]) )
#print( grep("/dev/mapper/", ["/dev/sda", "/dev/mapper/blah"], keep=False) )
#print( grep("/dev/mapper/", ["/dev/sda", "/dev/mapper/blah"], only=True) )
#print( grep("notfound", ["/dev/sda", "/dev/mapper/blah"], only=True) )
#exit(3)

def mystat(path):
    mode = os.stat(path)
    text = "%s %s" % (mode[stat.ST_MTIME], mode[stat.ST_SIZE])
    #debug("DEBUG: stat mtime and size: %s" % text)
    return text

originalmtime = mystat( sys.argv[0] )

def list_all_disks():
    alldisks=[]
    if ( operatingsystem == "FreeBSD" ):
        p = subprocess.Popen(["geom", "disk", "list"], stdout=subprocess.PIPE, stderr=subprocess_devnull)
        
        p.wait()
        if( p.returncode == 0 ):
            out, err = p.communicate()
            out = out.decode("utf-8")
            out = grep("Geom name:", out, regex=False)
            if isinstance(out, str):
                out = out.splitlines()
            for line in out:
                device = line.split()[2]
                alldisks += ["/dev/" + device]
    else:
        p = subprocess.Popen(["fdisk", "-l"], stdout=subprocess.PIPE, stderr=subprocess_devnull)
        
        p.wait()
        if( p.returncode == 0 ):
            out, err = p.communicate()
            out = out.decode("utf-8")

            # keep only device lines
            out = grep("Disk /dev", out, regex=False)
            # remove virtual devices (without Load_Cycle_Count)
            out = grep("/dev/mapper/|/dev/md[0-9]|/dev/md-[0-9]|/dev/bcache", out, keep=False)
            
            # Keep only name of the device
            lines = []
            for line in out:
                line = line.split()[1]
                lines += [line]
            
            # remove the colon after the device name, or possibly other extra stuff we don't want
            alldisks = grep("/dev/[a-zA-Z0-9]+", lines, only=True)
    
    # DEBUG: for testing disk changes
    #alldisks+=["/tmp/fakedisk"]
    #debug("list_all_disks; alldisks = %s" % alldisks)

    return alldisks

# handles MegaRAID too
def mysmartctl(device, args="-iA"):
    p = subprocess.Popen(["smartctl", args, str(device)], stdout=subprocess.PIPE, stderr=subprocess_devnull)
    p.wait()
    
    if( p.returncode == 2 ):
        p = subprocess.Popen(["smartctl", args, "-d", "megaraid,0", str(device)], stdout=subprocess.PIPE, stderr=subprocess_devnull)
        p.wait()
    
    out, err = p.communicate()
    out = out.decode("utf-8")
    return out

def list_intellipark_disks(alldisks):
    # find disks with the problem
    # dynamically detect problem... if Load_Cycle_Count is more than 2x the Power_Cycle_Count, then it is a problem disk
    disks=[]
    for disk in alldisks:
        if ( not os.path.exists(disk) ):
            continue

        #print("TESTING adding all disks; disk = %s" % disk)
        #disks += [disk]
        #continue
        
        #TODO: instead of grep on a string, make a class that has the used information (model, Power_Cycle_Count, Load_Cycle_Count)
        # Add disks by model, for ones we know are bad
        smartid = mysmartctl(disk, "-iA")
        
        if( grep("Western.*Digital.*Green", smartid, ignore_case=True) ):
            disks += [disk]
            debug("Found Western Digital Green: %s" % disk)
            continue
        elif( grep("ST....DM001-9YN166", smartid, ignore_case=True) ):
            disks += [disk]
            debug("Found Seagate Barracuda ST....DM001-9YN166: %s" % disk)
            continue
        elif( grep("ST....DM001-1CH166", smartid, ignore_case=True) ):
            disks += [disk]
            debug("Found Seagate Barracuda ST....DM001-1CH166: %s" % disk)
            continue
        elif( grep("ST....DM001-1CH164", smartid, ignore_case=True) ):
            disks += [disk]
            debug("Found Seagate Barracuda ST....DM001-1CH164 %s" % disk)
            continue
        elif( grep("Hitachi Ultrastar", smartid, ignore_case=True) ):
            continue

        startstopcount = grep("Power_Cycle_Count", smartid)
        if( startstopcount != None ):
            startstopcount = int(startstopcount[0].split()[9])
            
        load = grep("Load_Cycle_Count", smartid)
        if( load != None ):
            load = int(load[0].split()[9])

        debug("%s : startstopcount = %s, load = %s" % (disk, startstopcount, load) )
        
        if( not load ):
            # intellipark disks tend to have that attribute... so this is most likely not one
            continue

        if( not startstopcount ):
            # it is missing the important information to know if we can test it
            
            if( operatingsystem == "FreeBSD" ):
                if( grep("Perc_Rated_Life_Used|Wear_Levelling_Count", smartid) ):
                    # if it's an SSD, don't test it ... it has no mechanical head to park
                    # NOTE: I have no idea if this will test a hybrid disk
                    # TODO: detect an SSD properly instead of just assuming it based on some known attributes present
                    debug("Skipping SSD: %s" % disk)
                    continue
            else:
                diskname = disk.split("/")[-1]
                rotational = None
                with open("/sys/block/%s/queue/rotational" % diskname, "r") as f:
                    b = f.read().splitlines()[0]
                    debug( "rotational b = %s" % (b))
                    if ( b == "1" ):
                        rotational = True
                    else:
                        rotational = False
                    
                if not rotational:
                    debug("Skipping non-rotational disk (SSD?): %s" % disk)
                    continue
                elif( grep("Transport protocol:.*SAS$", smartid) ):
                    debug("Skipping SAS disk: %s" % disk)
                    # for a SAS disk, we only assume there is a problem if it is detected elsewhere
                    continue
            
            # otherwise, do the fix on the disk, not knowing if it is necessary
            debug("unknown if it's an intellipark disk: %s" % disk)
            disks += [disk]
        elif ( load > (startstopcount * 50) ):
            # if a disk has a load cycle count that is greater than 50 x the start stop count
            # then we assume it's an intellipark disk.
            debug( "%s > %s * 50" % (load, startstopcount))
            debug("Found suspected intellipark disk: %s" % disk)
            disks += [disk]

    if ( len(disks) != 0):
        if debug_enabled:
            debug("found %d intellipark disks: %s" % (len(disks), disks))
        else:
            info("found %d intellipark disks: %s" % (len(disks), disks))
    else:
        info("no intellipark disks.")

    return disks

def get_file_size(filename):
    "Get the file size by seeking at end"
    fd= os.open(filename, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)

#def get_disk_size(device):
    #if operatingsystem == "FreeBSD":
        #smartid = mysmartctl(device)
        #lines = grep("User Capacity.*bytes", smartid)
        #cols = lines[0].split()
        #size = cols[len(cols)-1]
    #else:
        #size=$(blockdev --getsize64 "$d")

# cached middle in bytes; get_disk_size is slow, so don't call it often
disk_to_middle={}
# number of bytes to read
chunksize = 512
align = 512

# this doesn't work... O_DIRECT doesn't work for os.read. http://bugs.python.org/issue5396
def read_raw(device, middle):
    # TODO: FIXME: make sure to use directio or no cache, or this will not work
    # if that can't be done in python, juse use dd
    #     mydd if="$d" of=/dev/null bs=512 count=1 iflag=direct skip="$middle" &>/dev/null
    #with os.open(device, os.O_RDONLY | os.O_DIRECT) as f:
    if True:
        f = os.open(device, os.O_RDONLY | os.O_DIRECT)
        try:
            debug("reading disk %s" % device)
            if(middle != 0):
                os.lseek(f, middle, os.SEEK_SET)
            chunk = os.read(f, chunksize)
        except KeyboardInterrupt as e:
            raise e
        except:
            e = sys.exc_info()[0]
            info("read failed, middle = %s, chunksize = %s" % (middle, chunksize))
            raise e
    
def read_dd(device, middle):
    cmd = "dd"
    if operatingsystem == "FreeBSD":
        cmd = "gdd"

    #dd if="$device" of=/dev/null bs=512 count=1 iflag=direct skip="$middle" &>/dev/null
    p = subprocess.Popen([cmd, "if=%s" % device, "of=/dev/null", "bs=%s" % chunksize, "count=1", "iflag=direct", "skip=%s" % int(middle)], 
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.wait()
    if( p.returncode != 0 ):
        out, err = p.communicate()
        warn("dd command failed; device = \"%s\"" % (device))
        warn( out and len(out) != 0 and out.decode("utf-8").splitlines()[0] )
        warn( err and len(err) != 0 and err.decode("utf-8").splitlines()[0] )

def read_middle(device, middle=None):
    if device in disk_to_middle:
        middle = disk_to_middle[device]
    else:
        size = get_file_size(device)
        middle = size/2
        # align the read to a likely sector size, or larger
        size = size - (size % align)
        disk_to_middle[device] = middle
    
    debug("reading device = %s, middle = %s" % (device, middle))
    #read_raw(device, middle)
    read_dd(device, middle / chunksize)
    
################################################################################
# Constants
################################################################################

if not which("facter"):
    print("ERROR: missing command: \"%s\"" % (dep))
    exit(e_missing_command)

pid = os.getpid()

# If this file exists, the script will stop. The file is removed when starting the script unless another instance is running.
stopFile = "/var/run/anti-intellipark.stop"
stopChildrenFile = "/var/run/anti-intellipark.stop-children"
# The lock file to preven other instances from running.
lockFile = "/var/run/anti-intellipark.lock"
basePidFile = "/var/run/anti-intellipark-%d.pid" % (pid)

# TODO: popen to use facter
operatingsystem = facter("operatingsystem")
debug("operatingsystem = %s" % operatingsystem)

# note: stat is not the same on the 2 OSses, but we aren't using it all, just comparing the output between runs and not parsing anything
# TODO: avoid using: flock, stat
if ( operatingsystem == "FreeBSD" ):
    deps = ["flock", "facter", "smartctl", "geom", "stat"]
else:
    deps = ["flock", "facter", "smartctl", "fdisk", "stat"]


################################################################################
# Main
################################################################################

def run():
    info("Testing for intellipark disks")
    timestamp_alldisks = time.time()
    alldisks = list_all_disks()
    intellipark_disks = list_intellipark_disks(alldisks)
    target_interval_alldisks = 60

    # The interval to use normally
    target_interval_default = 3
    # the current interval used (different depending on whether there are disks or not)
    target_interval = target_interval_default
    if len(intellipark_disks) == 0:
        target_interval = target_interval_alldisks

    stop = False
    # the timestamp at the start of the loop, before making sure 3 seconds passed
    timestamp_1 = None
    # the previous time 2, from after the 3 seconds passed on the previous loop
    timestamp_2_prev = None
    # The accumulated error in the timing, which should be subtracted from the calculated sleep time to keep it down
    # if too much is subtracted, then it will accumulate negative error, and stay close to the error from one run
    terror = 0
    while( not stop ):
        timestamp_1_prev = timestamp_1
        timestamp_1 = time.time()
        
        if timestamp_1_prev and timestamp_2:
            # Sleep until the current time is 3 seconds after the start of the previous processing
            sleeptime = timestamp_2 + target_interval - timestamp_1 - terror
            #debug("timestamp_1_prev = %s, timestamp_1 = %s, timestamp_2 = %s, sleeptime = %s" % (timestamp_1_prev, timestamp_1, timestamp_2, sleeptime))
            if sleeptime > 0:
                time.sleep(sleeptime)
            else:
                terror = 0
            timestamp_2_prev = timestamp_2
        
        # The start time of the previous processing
        timestamp_2 = time.time()
        debug("after sleep, timestamp_2 = %s" % timestamp_2)
        
        if timestamp_2_prev:
            # count total error in timing, so it can be removed next loop
            terror += timestamp_2 - timestamp_2_prev - target_interval
            #debug("terror = %s" % terror)
        
        ## pretend processing time
        #debug("processing")
        #time.sleep(3.2)
        #debug()
        
        # TODO: test this without threads... maybe add threads later
        for d in intellipark_disks:
            read_middle(d)

        # watch the list of devices
        # if the devices changed, it has to quit and restart
        if timestamp_alldisks + target_interval_alldisks > timestamp_2:
            timestamp_alldisks = time.time()
            alldisks2=list_all_disks()

            same = True
            if len(alldisks) != len(alldisks2):
                same = False
            else:
                n = 0
                count = len(alldisks)
                while( n < count):
                    if( alldisks[n] != alldisks2[n] ):
                        same = False
                        break
                    n += 1

            if not same:
                info("Disks were changed... testing for intellipark disks")
                alldisks=alldisks2
                intellipark_disks = list_intellipark_disks(alldisks)

        # watch this script's mtime
        # if the script was modified, it has to quit and restart
        mtime = mystat(sys.argv[0])
        if( mtime != originalmtime ):
            info("This script was modified... quitting")
            break

def main():
    for dep in deps:
        fail=0
        if not which(dep):
            print("ERROR: missing command: \"%s\"" % (dep))
            fail=1
        if( fail == 1 ):
            exit(e_missing_command)

    # stops the current run, but does not stop a new one from being run afterwards
    if( action == "stop" ):
        # TODO: find a way for one python process to kill the other.
        print("ERROR: not implemented")
        pass

    # installs the script in cron and /usr/local/bin
    # NOTE that puppet is in use on the servers and your changes will get overridden if you install this way.
    if ( action == "install" ):
        if ( sys.argv[0] != "/usr/local/bin/anti-intellipark.py" ):
            p = subprocess.Popen(["cp", sys.argv[0], "/usr/local/bin/anti-intellipark.py"], stdout=subprocess_devnull, stderr=subprocess_devnull)
            p.wait()

            p = subprocess.Popen(["chmod", "a+rx", "/usr/local/bin/anti-intellipark.py"], stdout=subprocess_devnull, stderr=subprocess_devnull)
            p.wait()
            
        with open("/etc/cron.d/anti-intellipark", "w") as f:
            f.write("PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/games:/usr/local/sbin:/usr/local/bin:/root/bin")
            f.write("* * * * * root /usr/local/bin/anti-intellipark.bash >/dev/null 2>&1")

    # lists the Load_Cycle_Count for all recognized disks, and saves a log to the current directory
    if( action == "list" ):
        alldisks = list_all_disks()
        for disk in alldisks:
            # TODO: multithread this... smartctl can be slow
            out = mysmartctl(disk, "-iA")
            
            model = grep("Device Model:", out)
            if( model == None or len(model) == 0 ):
                # probably something went wrong... the device name is probably invalid
                warn("Problem listing device: %s; out = %s" % (disk, out))
                continue
            else:
                model = model[0].split()[2]
            
            load = grep("Load_Cycle_Count", out)
            if ( load == None or len(load) == 0 ):
                load = None
            else:
                load = load[0].split()
                load = load[1] + " " + load[9]
            
            print("%-9s %-40s %-s" % (disk, model, load))
        
        exit(0)

    # just a test to see if "time ./anti..." includes subprocess cpu usage
    if( action == "test" ):
        p = subprocess.Popen(["bc"], stdin=subprocess.PIPE, stdout=subprocess_devnull, stderr=subprocess_devnull)
        out, err = p.communicate(bytes("2^1000000\n", 'iso-8859-1'))
        p.wait()
        exit(0)
        
    if( action != "run" ):
        usage()
        exit(e_missing_action)
        
    got_lock = False
    try:
        with open(lockFile, "wb") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got_lock = True
            except: # python3.4.x has BlockingIOError here, but python 3.2.x has IOError here... so just don't use those class names
                print("Could not obtain lock; another process already running? quitting")
                exit(1)
            run()
    finally:
        if got_lock:
            os.remove(lockFile)

main()