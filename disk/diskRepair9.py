#!/usr/bin/env python3
#
# Author: Peter Maloney
#
# Licensed GNU GPLv2; if you did not recieve a copy of the license, get one at http://www.gnu.org/licenses/gpl-2.0.html

import sys
import argparse
import subprocess
import os.path
import decimal
import time
import random
import os
import glob

################################################################################
# error codes
################################################################################

failed_disk=1
bad_argparse=2 # defined by argparse, not used here
missing_command=3

################################################################################
# Output functions
################################################################################

logfreq=50000000

sameline_used=0

debug_enabled = False

# Does not print a newline at the end, and if called more than once, the later lines overwrite the previous ones
def sameline(text):
    global sameline_used
    b=b''
    if(sameline_used != 0):
        spaces=''
        while( len(spaces) < sameline_used ):
            # create enough spaces to remove the old text that was written last time
            spaces+=' '
        b = b'\r' + bytes(spaces, 'utf-8') + b'\r'
        
    b += bytes(text, "utf-8")
    
    # write the erase+text together so there is no timing issue
    sys.stdout.buffer.write(b)
    sameline_used=len(text)
    pass

# If the last text on screen was printed using sameline, this prints a blank line, else does nothing
def samelinereturn():
    global sameline_used
    if( sameline_used != 0 ):
        print()
        sameline_used = 0

def info(str, end='\n'):
    samelinereturn()
    print("INFO: %s" % (str), end=end)
    sys.stdout.flush()

def debug(str, end='\n'):
    if( debug_enabled ):
        samelinereturn()
        print("DEBUG: %s" % (str), end=end)
    sys.stdout.flush()

def warn(str, end='\n'):
    samelinereturn()
    print("WARN: %s" % (str), end=end)
    sys.stdout.flush()

def error(str, end='\n'):
    samelinereturn()
    print("ERROR: %s" % (str), end=end)
    sys.stdout.flush()

# Dumps the raw bytes on screen; disable the other output to use this; It is useful just to compare output with hdparm, dd, etc. to validate writing, reading, seeking math
def dump(chunk):
#    samelinereturn()
#    sys.stdout.buffer.write( chunk )
#    sys.stdout.flush()
    pass

################################################################################
# CLI Handling
################################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair a disk's bad sectors.")
    parser.add_argument('devices', metavar='devices', type=str, nargs='+',
                    help='device(s) to repair (path to device, or (linux only) the serial number, or for action zerobaddmesg "all" selects all found')
    parser.add_argument('--dry-run', dest='dry_run', action='store_const',
                    const=True, default=False,
                    help='To report but not repair any sectors')
    parser.add_argument('--debug', dest='debug', action='store_const',
                    const=True, default=False,
                    help='enable debug level output')
    parser.add_argument('-s', dest='sector', action='store',
                    type=int, default=0,
                    help='Starting sector (default=0)')
    parser.add_argument('-e', dest='end_sector', action='store',
                    type=int, default=None,
                    help='Starting sector (default=0)')
    parser.add_argument('-a', dest='action', action='store',
                    type=str, default="zerobad", choices=["zerobad", "zerogood", "zerobaddmesg", "zeroall", "recover"],
                    help="Action: zerobad = (default) zero only the bad sectors to repair them; zerogood = zero only good sectors so the disk is less likely to fail during zeroing and is still noticably bad for returning; zerobaddmesg = scan dmesg output instead of the disk surface; zeroall = zero everything without scanning first; recover = if a bad sector can be read sometimes, then use that value to overwrite it so it is recovered old data rewritten to a good sector")
    parser.add_argument('-r', dest='random', action='store_const',
                    const=True, default=False,
                    help='instead of zeros, use random data')

    args = parser.parse_args()

    dry_run = args.dry_run
    debug_enabled = args.debug
    devices = args.devices
    sector = args.sector
    end_sector = args.end_sector
    sector_size = 512 # TODO: unhardcode this
    action = args.action

    if( args.dry_run ):
        info("DRY RUN")
    info("devices = %s, sector = %s, action = %s" % (devices, sector, action))

################################################################################

# http://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
def which(cmd):
    import os
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

def require(cmd):
    if( which(cmd) == None ):
        error("Could not find required command %s" % (cmd))
        exit(missing_command)

if hasattr(subprocess, "DEVNULL"):
    subprocess_devnull = subprocess.DEVNULL
else:
    # python 3.2.3 (Ubuntu 12.04) doesn't have DEVNULL... so use PIPE
    subprocess_devnull = subprocess.PIPE

# low level scanning and repairing by using hdparm (Linux only)
def fixup_hdparm(device, sector):
    sector = int(sector)
    
    device_size = get_file_size(device)
    x_end_sector = device_size / sector_size - 1
    
    for n in range(0, 300):
        x = sector + n
        if( x > x_end_sector ):
            #if x is not a valid sector (past end of disk), return
            return
        p = subprocess.Popen(["hdparm", "--read-sector", str(x), device], 
            stdout=subprocess_devnull, stderr=subprocess_devnull)
        p.wait()
        if( p.returncode == 0 ):
            # this sector is OK... no repair needed
            pass
        elif( 
            # I/O Error
            p.returncode == 5 
             
            # The running kernel lacks CONFIG_IDE_TASK_IOCTL support for this device.
            # FAILED: Invalid argument
              or p.returncode == 22 ): 
            # if fail,
            debug("return code was %s" % (p.returncode))
            if( not dry_run ):
                p = subprocess.Popen(["hdparm", "--yes-i-know-what-i-am-doing", "--write-sector", str(x), device], 
                    stdout=subprocess_devnull, stderr=subprocess_devnull)
                p.wait()
                if( p.returncode == 0 ):
                    info("repair of sector %s successful" % (x))
                else:
                    info("repair of sector %s failed" % (x))
            else:
                info("DRY RUN - skipping repair of sector %s" % (x))
        elif( p.returncode == 25 ): # reading sector 5860531760: FAILED: Inappropriate ioctl for device
            # It does this when a disk is so bad that it fails and Linux loses it, and smartctl fails too
            
            # print the error again
            p = subprocess.Popen(["hdparm", "--read-sector", str(x), device])
            
            # notify user and exit
            error("Disk %s has failed... can no longer access it." % (device))
            
            # TODO: make this handle multi-disk dmesg scan, so it will check other disks after one fails
            exit(failed_disk)
        else:
            # print the error again
            p = subprocess.Popen(["hdparm", "--read-sector", str(x), device])
            # notify user and exit
            error("Unsuppoted hdparm error code = %s detected... aborting" % p.returncode)
            return

# high level repair using python... fallback when other methods are unavailable (FreeBSD)
# on FreeBSD, this might actually work even though it won't work on Linux, because FreeBSD has (raw/lower level) character devices, and Linux has block devices
def fixup_python(device, sector):
    sector = int(sector)
    
    device_size = get_file_size(device)
    x_end_sector = device_size / sector_size - 1
    
    data = None
    if not args.random:
        data = get_zeros(sector_size)
    
    with open(device, "rb") as f:
        x = sector
        if( x > x_end_sector ):
            #if x is not a valid sector (past end of disk), return
            return
        f.seek(sector*sector_size, 0)
        for n in range(0, 300):
            x = sector + n
            if( x > x_end_sector ):
                #if x is not a valid sector (past end of disk), return
                return
            try:
                f.seek(x*sector_size, 0)
                chunk = f.read(sector_size)
                # this sector is OK... no repair needed
            except:
                #e = sys.exc_info()[0]
                #debug("%s" % (e))
                if( not dry_run ):
                    # overwrite one sector with zeros
                    try:
                        with open(device, "wb") as fw:
                            tell = fw.seek(x*sector_size, 0)
                            if x*sector_size != tell:
                                print("ERROR: seek sector %s does not match target sector %s" % (tell, x*sector_size))
                            else:
                                if args.random:
                                    data = get_random_data(sector_size)
                                fw.write(data)
                        info("repair of sector %s successful" % (x))
                    except:
                        e = sys.exc_info()[0]
                        debug("%s" % (e))
                        info("repair of sector %s failed" % (x))
                else:
                    info("DRY RUN - skipping repair of sector %s" % (x))

found_hdparm = which("hdparm")

def fixup(device, sector):
    global found_hdparm
    if found_hdparm:
        fixup_hdparm(device, sector)
    else:
        # TODO: on FreeBSD, run    sysctl kern.geom.debugflags=0x10 
        fixup_python(device, sector)

def get_zeros(chunksize):
    zeros = b'\0\0\0\0\0\0\0\0'
    while( len(zeros)*2 < chunksize ):
        zeros += zeros
    if( len(zeros) < chunksize ):
        zeros += zeros[0:(chunksize-len(zeros))]
    if( len(zeros) > chunksize ):
        # redundant ... just in case; we don't want to write too much data
        zeros = zeros[0:chunksize]
        
    return zeros

# slow... takes 2 seconds for chunksize=1048576
def x1_get_random_data(chunksize):
    print("get_random_data, chunksize = %s" % chunksize)
    data = []
    
    while len(data) < chunksize:
        data += [random.randrange(255)]
    
    data = bytes(data)
    return data

# slow... takes 64 seconds for chunksize=1048576
def x2_get_random_data(chunksize):
    print("get_random_data, chunksize = %s" % chunksize)
    data = b''
    
    n=0
    while n < chunksize:
        data += bytes([random.randrange(255)])
        n += 1
    
    return data

# seems fast, but not fast enough... only about 15 MB/s
def x3_get_random_data(chunksize):
    #print("get_random_data, chunksize = %s" % chunksize)
    return bytearray(os.urandom(chunksize))

def get_random_data(chunksize):
    print("get_random_data, chunksize = %s" % chunksize)
    return random.getrandbits(chunksize*8)


# http://stackoverflow.com/questions/2773604/query-size-of-block-device-file-in-python
def get_file_size(filename):
    "Get the file size by seeking at end"
    fd= os.open(filename, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)

# broad scanning with high level IO
# This replaces diskRepair[1-8].bash
def scan(device, chunksize=1024*1024, sector=0, end_sector=None):
    count=0
    
    if( chunksize % sector_size != 0 ):
        # prevent side effects of casting len(chunk)/sector_size to int later
        raise Exception("chunksize (%s) must be a multiple of sector_size (%s)" % (chunksize, sector_size))
    
    bad = []
    start_sector = sector
    
    # Information needed for progress indicator
    if( end_sector == None ):
        device_size = get_file_size(device)
        x_end_sector = device_size / sector_size - 1
    else:
        x_end_sector = end_sector
    total_bytes = (x_end_sector - start_sector) * sector_size
    start_time = time.time()

    info("Scanning device \"%s\" for bad sectors..." % device)
    debug("device = %s, chunksize = %s, sector = %s, end_sector = %s" % (device, chunksize, sector, end_sector))
    
    with open(device, "rb") as f:
        if(sector != 0):
            f.seek(sector*sector_size, 0)
        while True:
            try:
                tell = f.tell()
                if( tell != sector*sector_size ):
                   # safety check, in case my math is wrong somewhere, to prevent the wrong sector from being written to
                   # after lots of testing with different disks and situations, this can probably be removed
                   # In this section of the code, the check is redudnant; fixup(...) does its own check before modifying anything.
                   # This slows down the scan significantly
                   error("sector doesn't match... coding error. tell says %s which is sector %s, but sector = %s" % (tell, tell/sector_size, sector))
                   return
                
                if( end_sector != None and sector >= end_sector ):
                    info("hit end_sector; stopping reading")
                    break
                chunk = f.read(chunksize)
                if chunk:
                    if( count >= logfreq ):
                        # Simple output
                        #sameline("read ok, sector = %d" % (sector))
                        
                        # Output with progress indicator
                        done_bytes=(sector-start_sector)*sector_size
                        now_time = time.time()
                        rate = round(done_bytes / (now_time - start_time) / 1000000, 2)
                        sameline(
                            "read ok, sector = %d - %.2f MB/s - %.2f %% - %.2f GB / %.2f GB" % 
                            (sector, rate, round(100*done_bytes/total_bytes, 2), round(done_bytes/1000000000,2), round(total_bytes/1000000000,2))
                        )
                        
                        count=0
                    if len(chunk) != chunksize:
                        warn("partial chunk read")
                    sector += int(len(chunk)/sector_size)
                    
                    #dump(chunk)
                else:
                    info("End of file")
                    break
            except KeyboardInterrupt as e:
                samelinereturn()
                return
            except:
                e = sys.exc_info()[0]
                info("read failed, sector = %s, chunksize = %s" % (sector, chunksize))
                # failed_at is greater than sector by up to chunksize minus 1; we use this to tell fixup what to fix
                failed_at = int( f.tell() / sector_size )
                debug("failedat = %s" % (failed_at))
                if( action == "zerobad" ):
                    fixup(device, failed_at)
                elif( action == "recover" ):
                    error("recover not implemented. device = %s, failed_at = %s" % (device, failed_at))
                elif( action == "zerogood" ):
                    bad +=[sector]
                sector += 1
                f.seek(sector*sector_size, 0)
            count += chunksize
    
    debug("len(bad) = %s, bad = %s" % (len(bad), bad))
    
    #TODO: instead of handling all bad at the end, handle as they are discovered, so interrupting doesn't mean you have to start over
    if( action == "zerogood" ):
        zerogood(device, bad, chunksize=chunksize, sector=start_sector, end_sector=end_sector)

# This replaces something that isn't in other files in the bc-it-admin repo
def zerogood(device, bad, chunksize=1024*1024, sector=0, end_sector=None):
    count=0
    start_sector = sector
    
    # Information needed for progress indicator
    if( end_sector == None ):
        device_size = get_file_size(device)
        x_end_sector = device_size / sector_size
    else:
        x_end_sector = end_sector
    total_bytes = (x_end_sector - start_sector) * sector_size
    start_time = time.time()
    
    data = None
    if not args.random:
        data = get_zeros(chunksize)
        data_size = int(len(data)/sector_size)
    info("Zeroing good sectors...")
    debug("list of bad sectors to skip = %s" % (bad))
    chunksize_sectors = chunksize/sector_size
    with open(device, "wb") as f:
        if(sector != 0):
            f.seek(sector*sector_size, 0)
        while True:
            if args.random:
                data = get_random_data(chunksize)
                data_size = int(len(data)/sector_size)
            try:
                if( end_sector != None and sector >= end_sector ):
                    info("hit end_sector; stopping writing")
                    break
                # if this write would overwrite a bad sector, skip to the point after the bad sector
                if( len(bad) != 0 and sector + int(chunksize_sectors) > bad[0] ):
                    breakagain=0
                    while( sector + data_size > bad[0] ):
                        debug("while zeroing, skipped sector %s" % (sector))
                        
                        sector = bad[0]+1
                        bad.remove(bad[0])
                        debug("len(bad) = %s, bad = %s" % (len(bad), bad))
                        if( len(bad) == 0 ):
                            breakagain=1
                            break
                    if( breakagain == 1 ):
                        # This is because python is silly and doesn't support named blocks
                        break
                        
                    f.seek(sector*sector_size, 0)
                if( not dry_run ):
                    #if( f.tell() != sector*sector_size ):
                    #    # safety check, in case my math is wrong somewhere, to prevent the wrong sector from being written to
                    #    # after lots of testing with different disks and situations, this can probably be removed
                    #    error("sector doesn't match... coding error. tell says %s, but sector = %s" % (f.tell(), sector))
                    #    # This slows down the scan significantly (calling f.tell() I think)
                    #    return
                    
                    f.write(data)
                    if( count >= logfreq ):
                        # Simple output
                        #sameline("write ok, sector = %d" % (sector))
                        
                        # Output with progress indicator
                        done_bytes=(sector-start_sector)*sector_size
                        now_time = time.time()
                        rate = round(done_bytes / (now_time - start_time) / 1000000, 2)
                        sameline(
                            "write ok, sector = %d - %.2f MB/s - %.2f %% - %.2f GB / %.2f GB" % 
                            (sector, rate, round(100*done_bytes/total_bytes, 2), round(done_bytes/1000000000,2), round(total_bytes/1000000000,2))
                        )
                        
                        count=0
                    sector += data_size
                else:
                    info("DRY RUN - skipping zeroing of sector %s + chunksize %s" % (sector, chunksize))
                    sector += data_size
            except KeyboardInterrupt as e:
                samelinereturn()
                return
            except OSError as e:
                if( "No space left on device" in str(e) ):
                    return
                raise e
            except:
                e = sys.exc_info()[0]
                if( action == "zeroall" ):
                    # unexpected error... but continue and zero anyway; it's not an error unless fixup fails too
                    warn("write failed, sector = %s, chunksize = %s" % (sector, chunksize))
                    fixup(device, sector)
                    sector += 1
                    f.seek(sector*sector_size, 0)
                else:
                    # if not zeroall, it is an error to fail here; we are supposed to skip bad sectorsm
                    raise e
            count += chunksize

def zeroall(device, chunksize=1024*1024, sector=0, end_sector=None):
    zerogood(device, [], chunksize=chunksize, sector=sector, end_sector=end_sector)

# This replaces diskRepairDmesg.bash (Linux only probably)
def scan_dmesg(device):
    p1 = subprocess.Popen(["dmesg"], stdout=subprocess.PIPE)
    p2 = subprocess.Popen(["grep", "-Eo", "dev sd[a-z]+, sector [0-9]+"], stdin=p1.stdout, stdout=subprocess.PIPE)
    p3 = subprocess.Popen(["awk", "{print $2 $NF}"], stdin=p2.stdout, stdout=subprocess.PIPE)
    p4 = subprocess.Popen(["sort", "-u"], stdin=p3.stdout, stdout=subprocess.PIPE)
    p4.wait()
    
    if( p4.returncode != 0 ):
        error("Failed to get sector list from dmesg")
        return
    
    stdoutdata, stderrdata = p4.communicate()
    lines = stdoutdata.decode("utf-8").splitlines()
    
    for line in lines:
        loggeddevice, sector = line.split(",")
        loggeddevice = "/dev/%s" % loggeddevice
        
        if( device != "all" and loggeddevice != device ):
            # Only handle the device given on CLI
            continue
        
        info("device = %s, sector = %s" % (loggeddevice, sector))
        fixup(loggeddevice, sector)

################################################################################
# Main
################################################################################

def main():
    global devices
    
    # on Linux, allow using the serial number
    # if the device given looks like a serial number, change it to have the device path instead
    if( len(devices) == 1 and devices[0] != "all" and not os.path.exists(devices[0]) and not "/" in devices[0] ):
        ex = "/dev/disk/by-id/*%s" % devices[0]
        matches = glob.glob(ex)
        if( len(matches) != 1 ):
            raise Exception("Expression \"%s\" matched multiple files: %s" % (ex, matches))
        devices[0] = matches[0]
    
    if len(devices) == 1 and devices[0] == "all":
        devices = []
        # TODO: how to properly list all disk devices? This might miss something eg. when there are no sata/scsi devices.
        for ex in ["/dev/sd[a-z]", "/dev/sd[a-z][a-z]"]:
            matches = glob.glob(ex)
            devices += matches

    for device in devices:
        if( not os.path.exists(device) ):
            raise Exception("Device file does not exist: %s" % device)
        elif( os.path.isfile(device) or os.path.isdir(device) ):
            raise Exception("File is not a device: %s" % device)

        if( action == "zerobaddmesg" ):
            # Verify that required shell commands exist
            shell_commands_required=["dmesg", "grep", "awk", "sort"]
            for cmd in shell_commands_required:
                require(cmd)

            scan_dmesg(device)
        elif( action == "zerobad" or action == "zerogood" or action == "recover" ):
            scan(device, sector=sector, end_sector=end_sector)
        elif( action == "zeroall" ):
            zeroall(device, sector=sector, end_sector=end_sector)

        print()

if __name__ == "__main__":
    main()
