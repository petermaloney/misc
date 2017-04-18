#!/usr/bin/env python3
#
# Variance is calculated based on the size of pgs, not the used space in the filesystem. The values will be different than seen with ceph osd reweight-by-utilization or ceph osd df. But it means we can predict how full the OSDs will be when rebalance is done. That way you can reweight during rebalance until we know the balance will be right when rebalance is done. And it seems more stable... not having to reweight again too soon.
#
# Licensed GNU GPLv2; if you did not recieve a copy of the license, get one at http://www.gnu.org/licenses/gpl-2.0.html

import sys
import subprocess
import re
import argparse
import time
import logging
import json

#====================
# global variables
#====================

# pg_stat column in `ceph pg dump`, for finding the end of the pg list to ignore whatever is after it
re_pg_stat = re.compile("^[0-9]+\.[0-9a-z]+")

osds = {}
avg_old = 0
avg_new = 0


#====================
# logging
#====================

logging.VERBOSE = 15
def log_verbose(self, message, *args, **kws):
    if self.isEnabledFor(logging.VERBOSE):
        self.log(logging.VERBOSE, message, *args, **kws)

logging.addLevelName(logging.VERBOSE, "VERBOSE")
logging.Logger.verbose = log_verbose

formatter = logging.Formatter(
    fmt='%(asctime)-15s.%(msecs)03d %(levelname)s: %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

handler = logging.StreamHandler()
handler.setFormatter(formatter)

logger = logging.getLogger("bc-ceph-reweight-by-utilization")

logger.addHandler(handler)

#====================

def ceph_health():
    p = subprocess.Popen(["ceph", "health"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        lines = out.decode("UTF-8")
        return lines
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))

def ceph_osd_df():
    p = subprocess.Popen(["ceph", "osd", "df", "--format=json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        return json.loads(out.decode("UTF-8"))
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


def ceph_pg_dump():
    #bc-ceph-pg-dump -a -s

    p = subprocess.Popen(["ceph", "pg", "dump"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        lines = out.decode("UTF-8").splitlines()
        
        # find the header row
        header = 0
        for line in lines:
            if line[0:8] == "pg_stat\t":
                break
            header+=1
            
        # find the last pg
        last_pg = header
        for line in lines[header+1:]:
            if not re_pg_stat.match(line[0:8]):
                break
            last_pg+=1
    
        # return just the pg lines, not the other stats
        return lines[header:last_pg]
    else:
        raise Exception("pg dump command failed; err = %s" % str(err))


def ceph_osd_reweight(osd_id, weight):
    p = subprocess.Popen(["ceph", "osd", "reweight", str(osd_id), str(weight)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out, err = p.communicate()
    if( p.returncode == 0 ):
        return
    else:
        raise Exception("ceph osd df command failed; err = %s" % str(err))


# weighted average, based on bytes and weight
def refresh_average():
    global osds
    global avg_old
    global avg_new
    
    total_old = 0
    total_new = 0
    count = 0
    
    for osd in osds.values():
        total_old += osd.bytes_old / osd.weight
        total_new += osd.bytes_new / osd.weight
        count += 1
    
    avg_old = total_old/count
    avg_new = total_new/count

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("avg_old = %s" % avg_old)
        logger.debug("avg_new = %s" % avg_new)


class Osd:
    def __init__(self, osd_id):
        self.osd_id = osd_id
        
        # from ceph osd df
        self.weight = None
        self.reweight = None
        self.use_percent = None
        self.size = None
        self.df_var = None

        # from ceph pg dump
        self.bytes_old = None
        self.bytes_new = None

        self.var_old = None
        self.var_new = None
        # fudge factor to take the "new" numbers and adjust them to be closer to what ceph osd df gives you
        self.df_fudge = None

def refresh_weight():
    global osds
    
    for row in ceph_osd_df()["nodes"]:
        osd_id = row["id"]
        
        if osd_id in osds:
            osd = osds[osd_id]
        else:
            osd = Osd(osd_id)
            osds[osd_id] = osd
        
        osd.weight = row["crush_weight"]
        osd.reweight = row["reweight"]
        
        osd.use_percent = row["utilization"]
        
        osd.size = row["kb"]*1024
        osd.df_var = row["var"]

def refresh_bytes():
    global osds
    
    for osd in osds.values():
        osd.bytes_old = 0
        osd.bytes_new = 0
        
    for line in ceph_pg_dump():
        line = line.split()
        
        if line[0] == "pg_stat":
            # ignore header
            continue
        
        #   0          1          2      3       4       5      6        7      8          9        10 11         12    13          14    15            16                
        # ['pg_stat', 'objects', 'mip', 'degr', 'misp', 'unf', 'bytes', 'log', 'disklog', 'state', 'state_stamp', 'v', 'reported', 'up', 'up_primary', 'acting', 'acting_primary', 'last_scrub', 'scrub_stamp', 'last_deep_scrub', 'deep_scrub_stamp']


        size = int(line[6])
        up = line[14]
        acting = line[16]
        objects = int(line[1])
        
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("DEBUG: size = %s, up = %s, acting = %s" % (size,up,acting))
        
        osds_old = acting.replace("[", "").replace("]", "").split(",")
        osds_new = up.replace("[", "").replace("]", "").split(",")
        
        osds_old = list(map(int, osds_old))
        osds_new = list(map(int, osds_new))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("DEBUG: osds_old = %s, osds_new = %s" % (osds_old, osds_new))
        
        for osd_id in osds_old:
            osd_id = int(osd_id)
            osd = osds[osd_id]
            if not osd.bytes_old:
                osd.bytes_old = 0
            osd.bytes_old += size

        for osd_id in osds_new:
            osd_id = int(osd_id)
            osd = osds[osd_id]
            if not osd.bytes_new:
                osd.bytes_new = 0
            osd.bytes_new += size


def refresh_var():
    global osds
    global avg_old
    global avg_new
    
    for osd in osds.values():
        osd.var_old = osd.bytes_old / osd.weight / avg_old
        osd.var_new = osd.bytes_new / osd.weight / avg_new
        
        if args.fudge:
            # adding the fudge factor to try to match `ceph osd df` but also allow predicting post recovery size
            myuse = osd.bytes_old/osd.size*100
            osd.df_fudge = osd.use_percent / myuse
            
            osd.var_old *= osd.df_fudge
            osd.var_new *= osd.df_fudge


def refresh_all():
    refresh_weight()
    refresh_bytes()
    refresh_average()
    refresh_var()


def print_report():
    global osds
    
    print("%-3s %-7s %-8s %-14s %-7s %-14s %-7s" % (
        "osd", "weight", "reweight", "old_size", "var", "new_size", "var"))
    for osd in osds.values():
        print("%3d %7.5f %8.5f %14d %7.5f %14d %7.5f" % 
            (osd.osd_id, osd.weight, osd.reweight, osd.bytes_old, osd.var_old, osd.bytes_new, osd.var_new))


def is_peering():
    h = ceph_health()
    if "peering" in h:
        return True, h
    return False, h


def get_increment(var):
    if var < 0.85 or var > 1.15:
        return args.step
    
    # relatively how far between 0.85 or 1.15 and 1 are we
    p = abs(1 - var) / 0.15
    
    # sharply lower step relative to p
    return p**2 * args.step


def adjust():
    lowest = osds[0]
    highest = osds[0]
    
    for osd in osds.values():
        if osd.var_new < lowest.var_new:
            lowest = osd
        if osd.var_new > highest.var_new:
            highest = osd
    
    # We look at the spread between lowest and highest instead of just comparing the lowest to the avg, and  highest to avg. That way a lowest with reweight = 1 and a highest that is close enough to avg doesn't stop the process.
    spread = highest.var_new - lowest.var_new
    max_spread = (args.oload - 1)*2
    
    txt = "lowest osd_id = %s, var = %.5f" % (lowest.osd_id, lowest.var_new)
    txt += ", highest osd_id = %s, var = %.5f" % (highest.osd_id, highest.var_new)
    txt += ", spread = %.5f, max_spread = %.5f" % (spread, max_spread)
    logger.info(txt)

    adjustment_made = False
    
    # We don't reweight the lowest if it's 1, so that way one osd will always have reweight 1, so the other numbers always end up in a range 0-1. And also we don't raise numbers greater than 1.
    if lowest.reweight < 1 and spread > max_spread:
        increment = get_increment(lowest.var_new)
        new = round(round(lowest.reweight,3) + increment, 4)
        if new > 1:
            new = 1
        logger.info("Doing reweight: osd_id = %s, reweight = %s -> %s" % (lowest.osd_id, lowest.reweight, new))
        if not args.dry_run:
            ceph_osd_reweight(lowest.osd_id, new)
        adjustment_made = True
    else:
        logger.verbose("Skipping reweight: osd_id = %s, reweight = %s" % (lowest.osd_id, lowest.reweight))
        
    if spread > max_spread:
        increment = get_increment(highest.var_new)
        new = round(round(highest.reweight,3) - increment, 4)
        logger.info("Doing reweight: osd_id = %s, reweight = %s -> %s" % (highest.osd_id, highest.reweight, new))
        if not args.dry_run:
            ceph_osd_reweight(highest.osd_id, new)
        adjustment_made = True
    else:
        logger.verbose("Skipping reweight: osd_id = %s, reweight = %s" % (highest.osd_id, highest.reweight))
    
    return adjustment_made


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reweight OSDs so they have closer to equal space used.')
    parser.add_argument('-d', '--debug', action='store_const', const=True,
                    help='enable debug level logging')
    parser.add_argument('-v', '--verbose', action='store_const', const=True, default=False,
                    help='verbose mode')
    parser.add_argument('-q', '--quiet', action='store_const', const=True, default=False,
                    help='quiet mode')
    parser.add_argument('-F', '--fudge', action='store_const', const=True, default=False,
                    help='compare to ceph osd df to calculate a fudge factor to use when calculating var')

    parser.add_argument('-r', '--report', action='store_const', const=True, default=False,
                    help='print report table')
    parser.add_argument('-a', '--adjust', action='store_const', const=True, default=False,
                    help='adjust the reweight (default is report only)')
    parser.add_argument('-n', '--dry-run', action='store_const', const=True, default=False,
                    help='if combined with --adjust, go through all the adjustment code but don\'t actually adjust')
    
    parser.add_argument('-o', '--oload', default=1.03, action='store', type=float,
                    help='minimum var before reweight (default 1.03)')
    parser.add_argument('-s', '--step', default=0.03, action='store', type=float,
                    help='max step size for each reweight iteration. the value is scaled down when 0.85<var<1.15 (default 0.03)')

    parser.add_argument('-l', '--loop', action='store_const', const=True, default=False,
                    help='Repeat the reweight process forever.')
    parser.add_argument('--sleep', action='store', default=60, type=float,
                    help='Seconds to sleep between loops (default 60)')
    parser.add_argument('--sleep-short', action='store', default=10, type=float,
                    help='Seconds to sleep between loops that do adjustments (default 10)')
    
    args = parser.parse_args()

    if args.oload <= 1:
        logger.error("oload must be greater than 1")
        exit(1)

    if not args.report and not args.adjust:
        logger.error("Either report or adjust must be set")
        exit(1)

    if args.debug:
        logger.setLevel(logging.DEBUG)
    elif args.verbose:
        logger.setLevel(logging.VERBOSE)
    elif args.quiet:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)

    while True:
        refresh_all()
        
        if args.report:
            print_report()

        do_short_sleep = False
        if args.adjust:
            # our "new" bytes and variance numbers will only be right after peering is done, so don't run until then
            b, h = is_peering()
            if b:
                logger.info("refusing to reweight during peering. Try again later.\n%s" % h)
                do_short_sleep = True
            else:
                do_short_sleep = adjust()

        if not args.loop:
            break
        
        if do_short_sleep:
            time.sleep(args.sleep_short)
        else:
            time.sleep(args.sleep)
            
        if args.report:
            print()
