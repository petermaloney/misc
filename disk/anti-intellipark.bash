#!/bin/bash
#
# Author: Peter Maloney
#
# Trying to prevent the head from parking on the disks, to reduce wear. 
# This has to be run by cron, and flock will make sure there aren't 2 running. If you don't use cron, and the script or disks change, it will quit and assume that cron will start it again.
#
# This is useful for:
#     WD Green
#     Seagate Barracuda ST3000DM001-9YN166
#     Seagate Barracuda ST3000DM001-1CH166
#     Seagate Barracuda ST2000DM001-1CH164
#
#
# goals:
#    DONE- compatible with FreeBSD and Linux (requires ports: bash, facter, flock, gdd)
#    DONE- only run on disks with known problem
#        dynamically detect problem... if Load_Cycle_Count is more than 50x the Power_Cycle_Count, then it's a problem disk
#    DONE- run in cron
#    DONE- use flock to make sure more instances don't run
#    - when there are read errors, verify again:
#        -disk exists
#        -it is a problem disk
#    DONE- every once in a while, look for new disks
#        this wasn't perfectly easy ... but seems to work. There is a chance it would hang if one of the dd processes hung.
#    DONE- validate that dependencies are found
#    DONE- read the middle of the disk instead of $RANDOM? it would probably lower the seeking (2 runs in a row will not seek, and runs while other IO not from this script will interfere a bit less since the middle is probably closer to the other requests)
#    DONE- make it quit so cron can restart it if the script itself is modified
#
# Licensed GNU GPLv2; if you did not recieve a copy of the license, get one at http://www.gnu.org/licenses/gpl-2.0.html

# If this file exists, the script will stop. The file is removed when starting the script unless another instance is running.
stopFile=/var/run/anti-intellipark.stop
stopChildrenFile=/var/run/anti-intellipark.stop-children
# The lock file to preven other instances from running.
lockFile=/var/run/anti-intellipark.lock
basePidFile="/var/run/anti-intellipark-%d.pid"

# note: stat is not the same on the 2 OSses, but we aren't using it all, just comparing the output between runs and not parsing anything
linuxdeps=(flock facter smartctl dd fdisk stat)
bsddeps=(flock facter smartctl gdd geom stat)

operatingsystem=$(facter operatingsystem)

if [ "$operatingsystem" = "FreeBSD" ]; then
    deps=("${bsddeps[@]}")
else
    deps=("${linuxdeps[@]}")
fi

for dep in "${deps[@]}"; do
    fail=0
    if ! which "$dep" &>/dev/null; then
        echo "ERROR: missing command: \"$dep\""
        fail=1
    fi
    if [ "$fail" = 1 ]; then
        exit 1
    fi
done

mydd() { 
    if [ "$operatingsystem" = "FreeBSD" ]; then
        # use the GNU dd on FreeBSD, installed from ports, because it has things like conv= and iflag=
        gdd "$@"
    else
        dd "$@"
    fi
}

mystat() {
    if [ "$operatingsystem" = "FreeBSD" ]; then
        stat -f %m "$@"
    else
        stat -c %Y "$@"
    fi
}
originalmtime=$(mystat "$0")

listalldisks() {
    local alldisks=()
    if [ "$operatingsystem" = "FreeBSD" ]; then
        # DEBUG grep "da19" added
        #alldisks=($(geom disk list | grep "da19" | grep "Geom name:" | awk '{printf "/dev/%s\n", $3}' | sort))
        alldisks=($(geom disk list | grep "Geom name:" | awk '{printf "/dev/%s\n", $3}' | sort))
    else
        alldisks=($(fdisk -l 2>/dev/null | grep "Disk /dev" | grep -vE "/dev/mapper/|/dev/md[0-9]|/dev/md-[0-9]" | awk '{print $2}' | grep -Eo "/dev/[a-zA-Z0-9]+" | sort))
    fi
    
    # DEBUG: for testing disk changes
    #if [ -e /tmp/fakedisk ]; then
    #    echo "${alldisks[@]} /tmp/fakedisk"
    #else
        echo "${alldisks[@]}"
    #fi
}

# handles MegaRAID
mysmartctl() {
    output=$(smartctl "$@" 2>&1)
    status=$?
    
    if [ "$status" = "2" ]; then
        smartctl -d megaraid,0 "$@"
    else
        echo "$output"
    fi
}

# stops the current run, but does not stop a new one from being run afterwards
if [ "$1" = "stop" ]; then
    # stop it with:
    touch "$stopFile"
    #TODO: wait and makes sure they really ended... if not, kill, or kill -9

    echo -n "Waiting..."
    ( #)
        if ! flock -n 9; then
            echo -n "."
        fi
    ) 9>"$lockFile"
    
    exit 0
fi

# installs the script in cron and /usr/local/bin
# NOTE that puppet is in use on the servers and your changes will get overridden if you install this way.
if [ "$1" = "install" ]; then
    if [ "$0" != "/usr/local/bin/anti-intellipark.bash" ]; then
        cp "$0" /usr/local/bin/anti-intellipark.bash
    fi
    chmod +rx /usr/local/bin/anti-intellipark.bash
    echo "PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/games:/usr/local/sbin:/usr/local/bin:/root/bin" > /etc/cron.d/anti-intellipark
    #echo "0 * * * * root /usr/local/bin/anti-intellipark.bash >/dev/null 2>&1" >> /etc/cron.d/anti-intellipark
    echo "* * * * * root /usr/local/bin/anti-intellipark.bash >/dev/null 2>&1" >> /etc/cron.d/anti-intellipark
    exit 0
fi

# lists the Load_Cycle_Count for all recognized disks, and saves a log to the current directory
if [ "$1" = "list" ]; then
    alldisks=($(listalldisks))
    for disk in "${alldisks[@]}"; do
        if [ ! -e "$disk" ]; then
            continue
        fi
        (
            model=$(mysmartctl -a "$disk" | grep -E "Device Model:" | awk -F: '{print $2}' | tr -d '\n')
            load=$(mysmartctl -a "$disk" | grep -E "Load_Cycle_Count" | tr -d '\n' | tr -d '\n' | awk '{print $2 " " $10}')
            
            printf "%-9s %-40s %-s\n" "$disk" "${model:0:40}" "$load"
        ) &
    done | sed -r 's/da([0-9]+)/da \1/' | sort -k2,2n | sed -r 's/da ([0-9]+)/da\1/' | tee load_cycle_count.$(date +%s).log
    exit 0
fi

if [ -n "$1" ]; then
    echo "USAGE: $(basename "$0") [stop|install|list]"
    exit 1
fi

(
# bracket here to make vim highlighting happy )
    # locking begins here
    if ! flock -n 9; then
        echo "Could not get lock. Quitting. Use \"$0 stop\" to stop the other process."
        exit 1
    fi

    for f in /var/run/anti-intellipark-*.pid "$stopChildrenFile" "$stopFile"; do
        if [ -e "$f" ]; then
            rm "$f"
        fi
    done
    
    while [ ! -e "$stopFile" ]; do
        # find all disks
        alldisks=($(listalldisks))

        echo "DEBUG: all disks: ${alldisks[@]}"
        
        # find disks with the problem
        # dynamically detect problem... if Load_Cycle_Count is more than 2x the Power_Cycle_Count, then it is a problem disk
        disks=()
        for disk in "${alldisks[@]}"; do
            if [ ! -e "$disk" ]; then
                continue
            fi

            # Add disks by model, for ones we know are bad
            smartid=$(mysmartctl -i "$disk")
            if echo "$smartid" | grep -iE "Western.*Digital.*Green" >/dev/null 2>&1; then
                disks[${#disks[@]}]="$disk"
                echo "Found Western Digital Green: $disk"
                continue
            elif echo "$smartid" | grep -iE "ST....DM001-9YN166" >/dev/null 2>&1; then
                disks[${#disks[@]}]="$disk"
                echo "Found Seagate Barracuda ST....DM001-9YN166: $disk"
                continue
            elif echo "$smartid" | grep -iE "ST....DM001-1CH166" >/dev/null 2>&1; then
                disks[${#disks[@]}]="$disk"
                echo "Found Seagate Barracuda ST....DM001-1CH166: $disk"
                continue
            elif echo "$smartid" | grep -iE "ST....DM001-1CH164" >/dev/null 2>&1; then
                disks[${#disks[@]}]="$disk"
                echo "Found Seagate Barracuda ST....DM001-1CH164 $disk"
                continue
            fi

            startstopcount=$(mysmartctl -a "$disk" | grep -E "Power_Cycle_Count" | tr -d '\n' | awk '{print $10}')
            load=$(mysmartctl -a "$disk" | grep -E "Load_Cycle_Count" | tr -d '\n' | awk '{print $10}')

            echo "DEBUG: $disk : startstopcount = $startstopcount, load = $load"

            if [ -z "$load" -o -z "$startstopcount" ]; then
                # it is missing the important information to know if we can test it
                
                if [ "$operatingsystem" = "FreeBSD" ]; then
                    if mysmartctl -A "$disk" | grep -E "Perc_Rated_Life_Used|Wear_Levelling_Count" &>/dev/null; then
                        # if it's an SSD, don't test it ... it has no mechanical head to park
                        # NOTE: I have no idea if this will test a hybrid disk
                        echo "Skipping SSD: $disk"
                        continue
                    fi
                else
                    diskname=$(basename "$disk")
                    if ! grep 1 /sys/block/"$diskname"/queue/rotational &>/dev/null; then
                        echo "Skipping non-rotational disk (SSD?): $disk"
                        continue
                    elif mysmartctl -i "$disk" | grep -E "Transport protocol:.*SAS$" &>/dev/null; then
                        echo "Skipping SAS disk: $disk"
                        # for a SAS disk, we only assume there is a problem if it is detected elsewhere
                        continue
                    fi
                fi
                
                # otherwise, do the fix on the disk, not knowing if it is necessary
                echo "unknown if it's an intellipark disk: $disk"
                disks[${#disks[@]}]="$disk"
            elif [ "$load" -gt "$((startstopcount*50))" ]; then
                # if a disk has a load cycle count that is greater than 20 x the start stop count
                # then we assume it's an intellipark disk.
                echo "Found suspected intellipark disk: $disk"
                disks[${#disks[@]}]="$disk"
            fi
        done
        
        if [ "${#disks[@]}" != 0 ]; then
            echo "DEBUG: found intellipark disks: ${disks[@]}"
        else
            echo "DEBUG: no intellipark disks. :)"
        fi

        children=()
        for d in "${disks[@]}"; do
            echo "DEBUG disk is $d"
            #continue
            (
                if [ "$operatingsystem" = "FreeBSD" ]; then
                    size=$(smartctl -i "$d" | grep -Eo "User Capacity.*bytes" | sed -r "s/[^0-9]//g")
                else
                    size=$(blockdev --getsize64 "$d")
                fi
                skip=$((size / 2))
                while [ ! -e "$stopFile" -a ! -e "$stopChildrenFile" ]; do
                    mydd if="$d" of=/dev/null bs=512 count=1 iflag=direct skip="$skip" &>/dev/null
                    sleep 3
                done
            ) &
            
            # master records child pid file
            pid=$!
            pidFile=$(printf "$basePidFile" "$pid")
            echo "$pid" > "$pidFile"
            children[${#children[@]}]=$pid
        done

        echo "Background processes are running"
        echo "Monitoring for disk changes..."
        
        while [ ! -e "$stopFile" ]; do
            # master process watches the list of devices
            # if the devices changed, it has to quit and restart
            alldisks2=($(listalldisks))
            
            same=1
            if [ "${#alldisks[@]}" != "${#alldisks2[@]}" ]; then
                same=0
            else
                n=0
                count="${#alldisks[@]}"
                while [ "$n" -lt "$count" ]; do
                    if [ "${alldisks[$n]}" != "${alldisks2[$n]}" ]; then
                        echo "Disks were changed... refreshing"
                        same=0
                        break
                    fi
                    let n++
                done
            fi

            # master process watches this script's mtim
            # if the script was modified, it has to quit and restart
            mtime=$(mystat "$0")
            if [ "$mtime" != "$originalmtime" ]; then
                echo "This script was modified... quitting"
                same=0
            fi

            if [ "$same" != 1 ]; then
                touch "$stopChildrenFile"
                
                # make sure children are stopped ...
                for child in "${children[@]}"; do
                    kill "$child"
                done
                
                # TODO: check a different way in case a child hangs and wait blocks forever?
                echo "waiting for children..."
                wait
                
                echo "all children are stopped"
                
                break
            fi
            sleep 10
        done

        if [ "$mtime" != "$originalmtime" ]; then
            exit 100
        fi
    done
) 9>"$lockFile"
