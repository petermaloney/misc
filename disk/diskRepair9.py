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

# how many seconds should pass between updating the status line
target_output_interval=1

sameline_used=0

debug_enabled = False
syslog_enabled = False

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
    sys.stdout.flush()
    sameline_used=len(text)
    

# If the last text on screen was printed using sameline, this prints a blank line, else does nothing
def samelinereturn():
    global sameline_used
    if( sameline_used != 0 ):
        print()
        sameline_used = 0

def info(txt, end='\n'):
    samelinereturn()
    txt = "INFO: %s" % (txt)
    print(txt, end=end)
    sys.stdout.flush()

    if syslog_enabled:
        syslog.syslog(syslog.LOG_INFO, txt)
        
def debug(txt, end='\n'):
    if not debug_enabled:
        return
    
    txt = "DEBUG: %s" % (txt)

    samelinereturn()
    print(txt, end=end)
    sys.stdout.flush()

    if syslog_enabled:
        syslog.syslog(syslog.LOG_DEBUG, txt)

def warn(txt, end='\n'):
    samelinereturn()
    txt = "WARN: %s" % (txt)
    print(txt, end=end)
    sys.stdout.flush()

    if syslog_enabled:
        syslog.syslog(syslog.LOG_WARNING, txt)

def error(txt, end='\n'):
    samelinereturn()
    txt = "ERROR: %s" % (txt)
    print(txt, end=end)
    sys.stdout.flush()

    if syslog_enabled:
        syslog.syslog(syslog.LOG_ERR, txt)

# Dumps the raw bytes on screen; disable the other output to use this; It is useful just to compare output with hdparm, dd, etc. to validate writing, reading, seeking math
def dump(chunk):
#    samelinereturn()
#    sys.stdout.buffer.write( chunk )
#    sys.stdout.flush()
    pass

################################################################################
# misc functions
################################################################################

def get_serial(dev_path):
    ex = "/dev/disk/by-id/ata-*"
    matches = glob.glob(ex)

    dev_path_r = os.path.realpath(dev_path)

    shortest_match = None
    
    for m in matches:
        dev_path2 = os.path.realpath(m)
        if dev_path == dev_path2 or dev_path:
            s = m.split("_")
            match = s[-1]
            if not shortest_match or len(match) < len(shortest_match):
                # the shortest should contain the serial; others probably have things like "-part1" on the end
                shortest_match = match
    
    if shortest_match:
        return shortest_match

    raise Exception("failed to find serial for device \"%s\"" % (dev_path))

def get_devices(args):
    ret = []
    args2 = []

    for n in range(0, len(args)):
        arg = args[n]
        if arg == "all":
            # TODO: how to properly list all disk devices? This might miss something eg. when there are no sata/scsi devices.
            for ex in ["/dev/sd[a-z]", "/dev/sd[a-z][a-z]"]:
                for match in glob.glob(ex):
                    if match not in args:
                        args2 += [match]
        else:
            args2 += [arg]

    # on Linux, allow using the serial number
    # if the device given looks like a serial number, change it to have the device path instead
    for n in range(0, len(args2)):
        arg = args2[n]
        serial = None
        
        if not os.path.exists(arg) and not "/" in arg:
            # this arg is a serial
            ex = "/dev/disk/by-id/*%s" % arg
            matches = glob.glob(ex)
            if( len(matches) != 1 ):
                raise Exception("Expression \"%s\" matched multiple files: %s" % (ex, matches))
            
            id_path = matches[0]
            dev_path = id_path
            serial = arg
        elif os.path.exists(arg):
            # this arg is a path
            dev_path = arg
        else:
            raise Exception("device %s doesn't exist" % (arg)

        dev_path_r = os.path.realpath(dev_path)
        if dev_path_r.startswith("/dev/dm-"):
            # We assume there is no serial for device mapper paths, so just fake it.
            # this is expected to be the more stable path, such as /dev/vgname/lvname or /dev/mapper/vgname-lvname
            serial = dev_path
            # this is expected to be like /dev/dm-99
            dev_path = os.path.realpath(dev_path)
        elif not serial:
            serial = get_serial(dev_path)

        ret += [Device(dev_path, serial)]

    return ret

def int_or_none(value):
    try:
        value_int = int(value)
        return value_int
    except Exception as ignored:
        pass
    return None


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

# low level scanning and repairing by using hdparm (Linux only)
# returns the last sector worked on (failed or successful)
def fixup_hdparm(device, sector, fuzzy_after=300):
    sector = int(sector)
    device_size = get_file_size(device.path)
    x_end_sector = device_size / sector_size - 1
    prev_sector = None
    start_sector = sector
    end_sector = sector+fuzzy_after
    check_sector = start_sector
    while check_sector <= end_sector:
        force_repair = False
        if( check_sector > x_end_sector ):
            #if check_sector is not a valid sector (past end of disk), return
            return prev_sector
        p = subprocess.Popen(["hdparm", "--read-sector", str(check_sector), device.path], 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        stdoutdata, stderrdata = p.communicate()
        output1 = stdoutdata.decode("utf-8")
        output2 = stderrdata.decode("utf-8")
        output = output1 + output2
        zeros_count = 0
        if "0000 0000 0000 0000 0000 0000 0000 0000" in output:
            for line in output.splitlines():
                if "0000 0000 0000 0000 0000 0000 0000 0000" in line:
                    zeros_count += 1

        p.wait()
        prev_sector = check_sector
        if( p.returncode == 0 and zeros_count == 32 and "SG_IO: bad/missing sense data" in output ):
            # fails but returns 0
            # example output seen on ST3000DM001-9YN166

            # /dev/sdd:
            # reading sector 5676131376: SG_IO: bad/missing sense data, sb[]:  70 00 03 00 00 00 00 0a 40 51 00 00 11 04 00 00 a0 30 00 00 00 00 00 00 00 00 00 00 00 00 00 00
            # succeeded
            # 0000 0000 0000 0000 0000 0000 0000 0000
            # (all 0's)
            
            # [1695937.590034] ata4.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x0
            # [1695937.590915] ata4.00: irq_stat 0x40000001
            # [1695937.591781] ata4.00: failed command: READ SECTOR(S) EXT
            # [1695937.592645] ata4.00: cmd 24/00:01:37:e4:52/00:00:52:01:00/e0 tag 18 pio 512 in
            #                           res 51/40:00:37:e4:52/00:00:52:01:00/00 Emask 0x9 (media error)
            # [1695937.594382] ata4.00: status: { DRDY ERR }
            # [1695937.595259] ata4.00: error: { UNC }
            # [1695937.643025] ata4.00: configured for UDMA/133
            # [1695937.643048] ata4: EH complete
            force_repair = True
            
        if( not force_repair and p.returncode == 0 ):
            # this sector is OK... no repair needed
            #debug("sector %s is ok" % (check_sector))
            pass
        elif( 
              force_repair
            
            # I/O Error
              or p.returncode == 5 
             
            # The running kernel lacks CONFIG_IDE_TASK_IOCTL support for this device.
            # FAILED: Invalid argument
              or p.returncode == 22 ): 
            # if fail,
            debug("%s - return code was %s" % (device, p.returncode))
            if( not dry_run ):
                p = subprocess.Popen(["hdparm", "--yes-i-know-what-i-am-doing", "--write-sector", str(check_sector), device.path], 
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                p.wait()
                if( p.returncode == 0 ):
                    info("%s - repair of sector %s successful" % (device, check_sector))
                else:
                    info("%s - repair of sector %s failed" % (device, check_sector))
            else:
                info("%s - DRY RUN - skipping repair of sector %s" % (device, check_sector))
                
            if end_sector < check_sector + fuzzy_after:
                # if we find an error, we want to search fuzzy_after past that too
                end_sector = check_sector + fuzzy_after
            
        elif( p.returncode == 25 ): # reading sector 5860531760: FAILED: Inappropriate ioctl for device
            # It does this when a disk is so bad that it fails and Linux loses it, and smartctl fails too
            
            # print the error again
            error("%s - %s" (device, output))
            
            # notify user and exit
            error("%s - disk failed... can no longer access it." % (device))
            
            # TODO: make this handle multi-disk dmesg scan, so it will check other disks after one fails
            exit(failed_disk)
        else:
            # print the error again
            error("%s - %s" (device, output))
            
            # notify user and exit
            error("%s - Unsuppoted hdparm error code = %s detected... aborting" % (device, p.returncode))
            return prev_sector

        check_sector += 1

    return prev_sector

# high level repair using python... fallback when other methods are unavailable (FreeBSD)
# on FreeBSD, this might actually work even though it won't work on Linux, because FreeBSD has (raw/lower level) character devices, and Linux has block devices
def fixup_python(device, sector, fuzzy_after=300):
    sector = int(sector)
    
    device_size = get_file_size(device.path)
    x_end_sector = device_size / sector_size - 1
    
    data = None
    if not args.random:
        data = get_zeros(sector_size)
    
    prev_sector = None
    with open(device.path, "rb") as f:
        x = sector
        if( x > x_end_sector ):
            #if x is not a valid sector (past end of disk), return
            return prev_sector
        f.seek(sector*sector_size, 0)
        for n in range(0, fuzzy_after):
            x = sector + n
            if( x > x_end_sector ):
                #if x is not a valid sector (past end of disk), return
                return prev_sector
            prev_sector = x
            try:
                f.seek(x*sector_size, 0)
                chunk = f.read(sector_size)
                # this sector is OK... no repair needed
            except:
                #e = sys.exc_info()[0]
                #debug("%s - %s" % (device, e))
                if( not dry_run ):
                    # overwrite one sector with zeros
                    try:
                        with open(device.path, "wb") as fw:
                            tell = fw.seek(x*sector_size, 0)
                            if x*sector_size != tell:
                                error("%s - seek sector %s does not match target sector %s" % (device, tell, x*sector_size))
                            else:
                                if args.random:
                                    data = get_random_data(sector_size)
                                fw.write(data)
                        info("%s - repair of sector %s successful" % (device, x))
                    except:
                        e = sys.exc_info()[0]
                        debug("%s - %s" % (device, e))
                        info("%s - repair of sector %s failed" % (device, x))
                else:
                    info("%s - DRY RUN - skipping repair of sector %s" % (device, x))
    return prev_sector

found_hdparm = which("hdparm")

# fuzzy_after: how many sectors after the given sector to also scan (default 300)
def fixup(device, sector, fuzzy_after=300):
    global found_hdparm
    if found_hdparm:
        return fixup_hdparm(device, sector, fuzzy_after=fuzzy_after)
    else:
        # TODO: on FreeBSD, run    sysctl kern.geom.debugflags=0x10 
        return fixup_python(device, sector, fuzzy_after=fuzzy_after)

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
    #info("get_random_data, chunksize = %s" % chunksize)
    data = []
    
    while len(data) < chunksize:
        data += [random.randrange(255)]
    
    data = bytes(data)
    return data

# slow... takes 64 seconds for chunksize=1048576
def x2_get_random_data(chunksize):
    #info("get_random_data, chunksize = %s" % chunksize)
    data = b''
    
    n=0
    while n < chunksize:
        data += bytes([random.randrange(255)])
        n += 1
    
    return data

# seems fast, but not fast enough... only about 15 MB/s
def x3_get_random_data(chunksize):
    #info("get_random_data, chunksize = %s" % chunksize)
    return bytearray(os.urandom(chunksize))

def get_random_data(chunksize):
    #info("get_random_data, chunksize = %s" % chunksize)
    return random.getrandbits(chunksize*8)


# http://stackoverflow.com/questions/2773604/query-size-of-block-device-file-in-python
def get_file_size(filename):
    "Get the file size by seeking at end"
    fd= os.open(filename, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)

# wraps regular file objects and os.open() file descriptors to create a common interface (but currently doesn't actually use regular file objects)
# supposed to fix issues such as:
# - os.open() doesn't support "with" clause
# - fdopen(fd) objects have some UTF-8 issue when reading
# - O_DIRECT causing "Invalid argument" (solved like http://www.alexonlinux.com/direct-io-in-python)
# - os.open() objects not supporting os.SEEK_CUR
# without having to make your code handle it all ugly mixed in with your business logic
class OSFile():
    def __init__(self):
        self.os_fd = None
        #self.file_obj = None
        self.position = 0
        
    # flags eg. os.O_DIRECT | os.O_RDONLY
    def open(self, path, flags):
        self.size = get_file_size(path)
        self.os_fd = os.open(path, flags)
        #self.file_obj = os.fdopen(self.os_fd)

        import mmap
        size = self.size
        self.m = mmap.mmap(self.os_fd, size, prot=mmap.PROT_READ)

        return self
    
    # methods like the file object
    def seek(self, position, how):
        if how == os.SEEK_CUR:
            self.position += position
        elif how == os.SEEK_SET:
            self.position = position
        elif how == os.SEEK_END:
            # this cam be implemented by using os.SEEK_SET and self.size, but since I don't have any code that uses it, I can't quickly test it, won't gain anything by that work, so I'll just fail here so it will be known as untested.
            raise Exception("unsupported")
            
            # maybe this is the right implementation
            # self.position = self.size - position
        else:
            raise Exception("unsupported")
        #self.file_obj.seek(self.position, os.SEEK_SET)

        # maybe to go with the os.SEEK_END code...but unnecessary now
        # if self.position < 0:
        #     raise Exception("seek is before the start")
        # not sure if this one is right; maybe it requires refreshing the file size if the file changes
        # if self.position > self.size:
        #     raise Exception("seek is past the end")
    
        self.m.seek(self.position, os.SEEK_SET)
    
    def tell(self):
        #return self.file_obj.tell()
        return self.position
    
    def read(self, chunksize):
        r = self.m.read(chunksize)
        
        #debug("read some data = %s" % str(r, "ISO-8859-1"))
        self.seek(len(r), os.SEEK_CUR)
        return r
    
    # for the with clause
    # http://effbot.org/zone/python-with-statement.htm
    def __enter__(self):
        return self
    
    def __exit__(self, type, value, traceback):
        os.close(self.os_fd)


def open_device_for_scan(device):
    global args
    
    if args.direct:
        o = OSFile()
        return o.open(device, os.O_DIRECT | os.O_RDONLY)
    else:
        return open(device, "rb")


class Device():
    def __init__(self, path, serial):
        if not path:
            raise Exception("path is required")
        if not serial:
            raise Exception("serial is required")
        self.path = path
        self.serial = serial
        self.status_txt = None

    def __str__(self):
        return "{" + self.path + "|" + self.serial + "}"
    
    def print_status(self, txt):
        self.status_txt = txt

        if parallel:
            # the controller will read status from workers and print it separately
            # and currently there is no other output... having different intervals for syslog vs stdout is more work than it's worth
            pass
        else:
            sameline(txt)

    # broad scanning with high level IO
    # This replaces diskRepair[1-8].bash
    def scan(self, chunksize=1024*1024, sector=0, end_sector=None):
        global args
        
        if( chunksize % sector_size != 0 ):
            # prevent side effects of casting len(chunk)/sector_size to int later
            raise Exception("chunksize (%s) must be a multiple of sector_size (%s)" % (chunksize, sector_size))
        
        bad = []
        start_sector = sector
        
        # Information needed for progress indicator
        if( end_sector == None ):
            device_size = get_file_size(self.path)
            x_end_sector = device_size / sector_size - 1
        else:
            x_end_sector = end_sector
        total_bytes = (x_end_sector - start_sector) * sector_size
        start_time = time.time()

        debug("%s - scanning for bad sectors..." % self)
        debug("%s - chunksize = %s, sector = %s, end_sector = %s" % (self, chunksize, sector, end_sector))
        
        prev_time = None
        last_output_time = 0
        
        with open_device_for_scan(self.path) as f:
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
                       error("%s - sector doesn't match... coding error. tell says %s which is sector %s, but sector = %s" % (self, tell, tell/sector_size, sector))
                       return
                    
                    if( end_sector != None and sector >= end_sector ):
                        info("%s - hit end_sector; stopping reading" % self)
                        break
                    chunk = f.read(chunksize)
                    if chunk:
                        now = time.time()
                        if( last_output_time + target_output_interval < now ):
                            # Simple output
                            #print_status("read ok, sector = %d" % (sector))
                            
                            # Output with progress indicator
                            done_bytes=(sector-start_sector)*sector_size
                            now_time = time.time()
                            rate = round(done_bytes / (now_time - start_time) / 1000000, 2)
                            self.print_status("read ok, sector = %d - %.2f MB/s - %.2f %% - %.2f GB / %.2f GB" %
                                (sector, rate, round(100*done_bytes/total_bytes, 2), round(done_bytes/1000000000,2), round(total_bytes/1000000000,2)))

                            last_output_time = now
                            
                        if len(chunk) != chunksize:
                            warn("%s - partial chunk read" % self)
                        sector += int(len(chunk)/sector_size)
                        
                        if args.sleep_percent:
                            now = time.time()
                            
                            if prev_time:
                                sleep_factor = args.sleep_percent/100
                                sleep_time = (sleep_factor * (now - prev_time))/(1 - sleep_factor)
                                
                                #debug("%s - sleeping %s seconds" % (self, sleep_time))
                                time.sleep(sleep_time)
                            
                            prev_time = time.time()
                        #dump(chunk)
                    else:
                        info("%s - End of file" % self)
                        break
                except KeyboardInterrupt as e:
                    samelinereturn()
                    return
                except (TypeError, NameError, ValueError, AttributeError) as e: 
                    # TODO: add OSError in here somehow...but also handle it in the fixup except
                    # handle this one in the fixup except:
                    #     OSError: [Errno 5] Input/output error
                    raise e
                except:
                    e = sys.exc_info()[0]
                    debug("%s - %s" % (self, e))
                    info("%s - read failed, sector = %s, chunksize = %s" % (self, sector, chunksize))
                    # failed_at is greater than sector by up to chunksize minus 1; we use this to tell fixup what to fix
                    failed_at = int( f.tell() / sector_size )
                    debug("%s - failedat = %s" % (self, failed_at))
                    prev_fixup_sector = None
                    if( action == "zerobad" ):
                        prev_fixup_sector = fixup(self, failed_at)
                        debug("%s - prev_fixup_sector = %s" % (self, prev_fixup_sector))
                    elif( action == "recover" ):
                        error("%s - recover not implemented. failed_at = %s" % (self, failed_at))
                    elif( action == "zerogood" ):
                        bad += [sector]
                    
                    if prev_fixup_sector != None:
                        sector = prev_fixup_sector+1
                    else:
                        sector += 1
                    f.seek(sector*sector_size, 0)
        
        debug("%s - len(bad) = %s, bad = %s" % (self, len(bad), bad))
        
        #TODO: instead of handling all bad at the end, handle as they are discovered, so interrupting doesn't mean you have to start over
        if( action == "zerogood" ):
            self.zerogood(bad, chunksize=chunksize, sector=start_sector, end_sector=end_sector)


    # This replaces something that isn't in other files in the bc-it-admin repo
    def zerogood(self, bad, chunksize=1024*1024, sector=0, end_sector=None):
        start_sector = sector
        
        # Information needed for progress indicator
        if( end_sector == None ):
            device_size = get_file_size(self.path)
            x_end_sector = device_size / sector_size
        else:
            x_end_sector = end_sector
        total_bytes = (x_end_sector - start_sector) * sector_size
        start_time = time.time()
        last_output_time = 0
        
        data = None
        if not args.random:
            data = get_zeros(chunksize)
            data_size = int(len(data)/sector_size)
        debug("%s - zeroing good sectors..." % (self))
        debug("%s - list of bad sectors to skip = %s" % (self, bad))
        chunksize_sectors = chunksize/sector_size
        with open(self.path, "wb") as f:
            if(sector != 0):
                f.seek(sector*sector_size, 0)
            while True:
                if args.random:
                    data = get_random_data(chunksize)
                    data_size = int(len(data)/sector_size)
                try:
                    if( end_sector != None and sector >= end_sector ):
                        info("%s - hit end_sector; stopping writing" % self)
                        break
                    # if this write would overwrite a bad sector, skip to the point after the bad sector
                    if( len(bad) != 0 and sector + int(chunksize_sectors) > bad[0] ):
                        breakagain=0
                        while( sector + data_size > bad[0] ):
                            debug("%s - while zeroing, skipped sector %s" % (self, sector))
                            
                            sector = bad[0]+1
                            bad.remove(bad[0])
                            debug("%s - len(bad) = %s, bad = %s" % (self, len(bad), bad))
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
                        #    error("%s - sector doesn't match... coding error. tell says %s, but sector = %s" % (self, f.tell(), sector))
                        #    # This slows down the scan significantly (calling f.tell() I think)
                        #    return
                        
                        f.write(data)
                        now = time.time()
                        if( last_output_time + target_output_interval < now ):
                            # Simple output
                            #sameline("write ok, sector = %d" % (sector))
                            
                            # Output with progress indicator
                            done_bytes=(sector-start_sector)*sector_size
                            now_time = time.time()
                            rate = round(done_bytes / (now_time - start_time) / 1000000, 2)
                            self.print_status(
                                "write ok, sector = %d - %.2f MB/s - %.2f %% - %.2f GB / %.2f GB" % 
                                (sector, rate, round(100*done_bytes/total_bytes, 2), round(done_bytes/1000000000,2), round(total_bytes/1000000000,2))
                            )
                            
                            last_output_time = now
                        sector += data_size
                    else:
                        info("%s - DRY RUN - skipping zeroing of sector %s + chunksize %s" % (self, sector, chunksize))
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
                        warn("%s - write failed, sector = %s, chunksize = %s" % (self, sector, chunksize))
                        fixup(self, sector)
                        sector += 1
                        f.seek(sector*sector_size, 0)
                    else:
                        # if not zeroall, it is an error to fail here; we are supposed to skip bad sectorsm
                        raise e

    def zeroall(self, chunksize=1024*1024, sector=0, end_sector=None):
        self.zerogood([], chunksize=chunksize, sector=sector, end_sector=end_sector)

    # This replaces diskRepairDmesg.bash (Linux only probably)
    def list_sectors_dmesg(self, bad_sectors):
        debug("%s - checking with dmesg" % (self))
        p1 = subprocess.Popen(["dmesg"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep", "-Eo", "dev sd[a-z]+, sector [0-9]+"], stdin=p1.stdout, stdout=subprocess.PIPE)
        p3 = subprocess.Popen(["awk", "{print $2 $NF}"], stdin=p2.stdout, stdout=subprocess.PIPE)
        p4 = subprocess.Popen(["sort", "-u"], stdin=p3.stdout, stdout=subprocess.PIPE)
        p4.wait()
        
        if( p4.returncode != 0 ):
            error("%s - Failed to get sector list from dmesg" % (self))
            return
        
        stdoutdata, stderrdata = p4.communicate()
        lines = stdoutdata.decode("utf-8").splitlines()
        
        for line in lines:
            loggeddevice, sector = line.split(",")
            loggeddevice = "/dev/%s" % loggeddevice
            
            if( loggeddevice != self.path ):
                # Only handle the device given on CLI
                continue
            
            value_int = int_or_none(sector)
            if value_int != None and value_int not in bad_sectors:
                info("%s - dmesg; device = %s, sector = %s" % (self, loggeddevice, sector))
                bad_sectors += [value_int]
            else:
                debug("%s - dmesg; sector = %s" % (self, sector))

    def list_sectors_smartctl_selftest(self, bad_sectors):
        cmd = ["smartctl", "-l", "selftest", self.path]
        debug("%s - checking with cmd = %s" % (self, cmd))
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        p1.wait()
        
        stdoutdata, stderrdata = p1.communicate()
        lines = stdoutdata.decode("utf-8").splitlines()

        if( len(lines) == 0 ):
            error("%s - Failed to get sector list from %s" % (self, cmd))
            return
        
        lba_index=-1
        for line in lines:
            if not line:
                continue
            if lba_index == -1:
                lba_index = line.find("LBA_of_first_error")
            else:
                value = line[lba_index:]
            
                if value != "-":
                    value_int = int_or_none(value)
                    if value_int != None and value_int not in bad_sectors:
                        info("%s - smartctl selftest; sector = %s" % (self, value_int))
                        bad_sectors += [value_int]
                    else:
                        debug("%s - smartctl selftest; sector = %s (skipped)" % (self, value_int))


    def list_sectors_smartctl_error(self, bad_sectors):
        cmd = ["smartctl", "-l", "error", self.path]
        debug("%s - checking with cmd = %s" % (self, cmd))
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        p1.wait()

        # lines are like:
        #     Num  Test_Description    Status                  Remaining  LifeTime(hours)  LBA_of_first_error
        #     # 1  Extended offline    Self-test routine in progress 40%      6884         -
        
        stdoutdata, stderrdata = p1.communicate()
        lines = stdoutdata.decode("utf-8").splitlines()
        
        if( len(lines) == 0 ):
            error("%s - Failed to get sector list from %s" % (self, cmd))
            return

        # lines are like:
        #  40 51 00 31 02 00 00  Error: UNC at LBA = 0x00000231 = 561

        for line in lines:
            if not line:
                continue
            if "Error: UNC at LBA" in line:
                value = line.split()[-1]
                value_int = int_or_none(value)
                if value_int != None and value_int not in bad_sectors:
                    info("%s - smartctl error; sector = %s" % (self, value_int))
                    bad_sectors += [value_int]
                else:
                    debug("%s - smartctl error; sector = %s (skipped)" % (self, value_int))

    # scans specific sector list
    # intended to be used with list_sectors_X() functions to generate lists
    def scan_list(self, bad_sectors):
        for sector in bad_sectors:    
            info("%s - sector = %s" % (self, sector))
            fixup(self, sector)

def run(device):
    debug("%s - working on device" % device)
    if( not os.path.exists(device.path) ):
        raise Exception("Device file does not exist: %s" % device.path)
    elif( os.path.isfile(device.path) or os.path.isdir(device.path) ):
        raise Exception("File is not a device: %s" % device.path)

    if( action in ["zerobaddmesg", "zerobadsmartctl", "quick"] ):
        bad_sectors = []
        
        if( action in ["zerobaddmesg", "quick"] ):
            device.list_sectors_dmesg(bad_sectors)
        if( action in ["zerobadsmartctl", "quick"] ):
            device.list_sectors_smartctl_selftest(bad_sectors)
            device.list_sectors_smartctl_error(bad_sectors)
            
        bad_sectors = sorted(bad_sectors)
        device.scan_list(bad_sectors)
    elif( action == "zerobad" or action == "zerogood" or action == "recover" ):
        device.scan(sector=sector, end_sector=end_sector)
    elif( action == "zeroall" ):
        device.zeroall(sector=sector, end_sector=end_sector)

def init_threading():
    import threading
    
    global Worker

    class Worker(threading.Thread) :
        def __init__(self, device):
            super(Worker, self).__init__()
            self.device = device
            
            self.started = False
            self.done = False

        def run(self):
            try:
                self.started = True
                run(self.device)
            finally:
                self.done = True

################################################################################
# Main - CLI Handling
################################################################################

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair a disk's bad sectors.")
    parser.add_argument('devices', metavar='devices', type=str, nargs='+',
                    help='device(s) to repair (path to device, or (linux only) the serial number, or for action zerobaddmesg "all" selects all found')
    parser.add_argument('-n', '--dry-run', action='store_const',
                    const=True, default=False,
                    help='To report but not repair any sectors')
    
    parser.add_argument('--debug', action='store_const',
                    const=True, default=False,
                    help='enable debug level output')
    parser.add_argument('--syslog', action='store_const',
                    const=True, default=False,
                    help='enable syslog)')
    
    parser.add_argument('-s', '--sector', action='store',
                    type=int, default=0,
                    help='Starting sector (default=0)')
    parser.add_argument('-e', '--end-sector', action='store',
                    type=int, default=None,
                    help='Starting sector (default=0)')
    parser.add_argument('-a', '--action', action='store',
                    type=str, default="zerobad", 
                        choices=["zerobad", "zerogood", "zerobaddmesg", "zerobadsmartctl", "zeroall", "recover", "quick"],
                    help="Action: zerobad = (default) zero only the bad sectors to repair them; zerogood = zero only good sectors so the disk is less likely to fail during zeroing and is still noticably bad for returning; zerobaddmesg = use dmesg for sector list; zerobadsmartctl = use smartctl error log for sector list; zeroall = zero everything without scanning first; recover = if a bad sector can be read sometimes, then use that value to overwrite it so it is recovered old data rewritten to a good sector; quick = use dmesg and smartctl for sector list")
    parser.add_argument('-r', '--random', action='store_const',
                    const=True, default=False,
                    help='instead of zeros, use random data (not fully implemented; has no effect when using hdparm, so doesn\'t affect zerobad* on Linux)')
    parser.add_argument('-z', '--sleep-percent', action='store', type=float, default=20,
                    help="for read scanning only, sleep some percentage of the time so the disk can't be busy with only repairing (default 20)")
    parser.add_argument('--direct', action='store_const', const=True, default=False,
                    help="enable experimental O_DIRECT support")
    parser.add_argument('-p', '--parallel', action='store_const',
                    const=True, default=False,
                    help='enable parallel mode, with one device per thread (intended to be used only with syslog)')

    args = parser.parse_args()

    devices = get_devices(args.devices)
    
    dry_run = args.dry_run
    debug_enabled = args.debug
    sector = args.sector
    end_sector = args.end_sector
    sector_size = 512 # TODO: unhardcode this
    action = args.action
    syslog_enabled = args.syslog
    parallel = args.parallel
    
    if( args.syslog ):
        import syslog
        syslog.openlog("diskRepair9")
    if( args.dry_run ):
        info("DRY RUN")
    if( args.parallel ):
        init_threading()

def list_to_string(l):
    ret = ""
    for i in l:
        if len(ret) != 0:
            ret += ", "
        ret += str(i)
    ret = "[" + ret + "]"
    return ret

################################################################################
# Main
################################################################################

def main():
    global devices
    
    info("devices = %s, sector = %s, action = %s" % (list_to_string(devices), sector, action))
    
    # Verify that required shell commands exist
    shell_commands_required = []
    if( action == "zerobaddmesg" ):
        shell_commands_required=["dmesg", "grep", "awk", "sort"]
    if os.uname().sysname == "Linux":
        shell_commands_required += ["hdparm"]
    for cmd in shell_commands_required:
        require(cmd)

    if parallel:
        workers = []
        for device in devices:
            worker = Worker(device)
            workers += [worker]
            worker.start()
        
        #wait for workers
        while True:
            done_count = 0
            for w in workers:
                if w.done:
                    done_count += 1
                elif w.device.status_txt:
                    sameline("%s - %s" % (w.device, w.device.status_txt))

                    for n in range(0, 3):
                        if w.done:
                            break
                        time.sleep(1)
            
            if done_count == len(workers):
                break
            
            time.sleep(0.1)
    else:
        for device in devices:
            run(device)


if __name__ == "__main__":
    main()
