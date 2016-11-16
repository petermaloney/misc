#!/bin/bash
#
#
# WARNING this script *really* hammers the ntp server... don't use it on public servers, and just run it manually for large drifts, not routinely.


## =================================================================
## date commands for gradual adjustment
## =================================================================
## 
## apt-get install rdate adjtimex
## 
## rdate -an ntp
##     -a = adjust gradually instead of just hopping
##     -n = use SNTP (RFC 2030) instead of RFC 868
##     ntp = the NTP server
## 
## adjtimex
##     eg. slow the clock by the maximum amount (lose 1 second every 10 seconds)
##     
##     adjtimex --tick 9000
##     
##     then wait ...
##     
##     then if the date is right, set it back
##     
##     adjtimex --tick 10000
## 
##     To go back 1 second:
##     
##     adjtimex --tick 9000 ; sleep 9.0909 ; adjtimex --tick 10000
## 
##     To go forward 1 second:
##     
##     adjtimex --tick 11000 ; sleep 11 ; adjtimex --tick 10000



#automatic using both (for when rdate doesn't do anything, even without -p)
# echos the offset in seconds
getoffset() {
    local ts="$1"
    if [ -z "$ts" ]; then
        ts=$timeserver
    fi
    sum=0
    count=0
    #for n in {1..20}; do
        data=$(rdate -anp "$ts" | awk '$NF == "seconds" {n=NF-1; print $n}')
        if [ -z "$data" ]; then
            return 1
        fi
        sum=$(echo "$sum + $data" | bc)
        let count++

    #done
    data=$(echo "scale=6; $sum / $count" | bc)
    echo "$data"
}

get_time_server() {
    timeserver=$(grep ^server /etc/ntp.conf | awk '{print $2; exit}')
    if [ "$timeserver" = "127.127.1.0" ]; then
        timeserver=bcvm1
    elif [ -z "$timeserver" ]; then
        if rdate -anp "bcvm1" >/dev/null 2>&1; then
            timeserver=bcvm1
        elif rdate -anp "ntp" >/dev/null 2>&1; then
            timeserver=ntp
        elif rdate -anp "ntp3" >/dev/null 2>&1; then
            timeserver=ntp3
        else
            echo "ERROR: no time servers could be used"
            return 1
        fi
    fi
    
    echo "$timeserver"
}

autotimedrift() {
    if ! which rdate >/dev/null 2>&1; then
        echo "ERROR: install rdate first"
        return 1
    fi

    if ps -ef | grep n[t]pd >/dev/null; then
        echo "ERROR: stop ntpd first"
        return 1
    fi
    
    timeserver=$(get_time_server)
    echo "Adjusting drift to match time server $timeserver"
    
    defaultFreq=$(adjtimex --print | awk '$1 == "frequency:" {print $2}')
    defaultTick=$(adjtimex --print | awk '$1 == "tick:" {print $2}')
    
    # Find and set what the --tick and --frequency should be to make this clock drift less
    targetFreq="$defaultFreq"
    targetTick="$defaultTick"
    while true; do
        data1=$(getoffset)
        sleep 30
        data2=$(getoffset)
        drift=$(echo "scale=6; $data2 - $data1" | bc)
        positive=$(awk '{print ($1 > 0)}' <<< "$drift")
        
        large_drift=$(awk '{print ($1 < -0.002    || $1 > 0.002   )}' <<< "$drift")
        small_drift=$(awk '{print ($1 < -0.00003  || $1 > 0.00003 )}' <<< "$drift")
        tiny_drift=$(awk  '{print ($1 < -0.000005 || $1 > 0.000005)}' <<< "$drift")
        
        echo "drift = $drift, pos = $positive, l = $large_drift, s = $small_drift, t = $tiny_drift"
        
        # a change in freq of 65536 is 0.0864 seconds per day, so we use large numbers here
        adjustment=0
        if [ "$large_drift" = 1 ]; then
            adjustment=2000000
        elif [ "$small_drift" = 1 ]; then
            adjustment=20000
        elif [ "$tiny_drift" = 1 ]; then
            adjustment=1500
        else
            echo "Done offset adjustment: tick = $targetTick, freq = $targetFreq"
            break
        fi
        
        if [ "$positive" = 1 ]; then
            targetFreq=$(echo "$targetFreq + $adjustment" | bc)
        else
            targetFreq=$(echo "$targetFreq - $adjustment" | bc)
        fi
        
        # TODO: check range: if freq is > 6553600 or < -6553600 then add 1 to tick and subtract 6553600
        if [ "$targetFreq" -gt 6553600 ]; then
            let targetTick++
            targetFreq=0
        elif [ "$targetFreq" -lt -6553600 ]; then
            let targetTick--
            targetFreq=0
        fi
        
        echo "adjtimex --tick \"$targetTick\" --frequency \"$targetFreq\""
        adjtimex --tick "$targetTick" --frequency "$targetFreq"
        
        if ! grep -qE "bc.local$|^10\." <<< "$timeserver"; then
            # make sure not to violate rate limiters on internet servers
            sleep 5
        fi
    done
}

autotimesync() {
    if ! which rdate >/dev/null 2>&1; then
        echo "ERROR: install rdate first"
        return 1
    fi

    if ps -ef | grep n[t]pd >/dev/null; then
        echo "ERROR: stop ntpd first"
        return 1
    fi
    
    timeserver=$(get_time_server)
    echo "Adjusting time to match time server $timeserver"
    
    # find delay in seconds, to see how old our date responses are, so we can add the delay to get closer to the correct time at the instant the query is done
    delay=$(
        ping -i 0.2 -c 20 "$timeserver" \
        | awk -F'/| ' '
            /min.avg.max/ {
                time=$8
                unit=$NF
                
                if( unit == "ms" ){
                    # print seconds
                    print time / 1000
                }else{
                    printf "unsupported unit %s\n", unit
                    exit 1
                }
            }
        '
    )
    
    if [ -z "$delay" ]; then
        echo "ERROR: couldn't find delay"
        return
    fi
    
    targetTick=$(adjtimex --print | awk '$1 == "tick:" {print $2}')
    
    # Then adjust the current time
    while true; do
        # data here is the number of seconds the time server is ahead (negative for behind)
        data=$(getoffset)
        data=$(echo "$data + $delay" | bc)
        positive=$(awk '{print ($1 > 0)}' <<< "$data")
        
        hugeoffset=$(awk '{print ($1 < -30 || $1 > 30)}' <<< "$data")
        hugesleep=$(awk '{pos = $1 < 0 ? -$1 : $1; print $1 * 10}' <<< "$data")
        largeoffset=$(awk '{print ($1 < -0.1 || $1 > 0.1)}' <<< "$data")
        smalloffset=$(awk '{print ($1 < -0.005 || $1 > 0.005)}' <<< "$data")
        tinyoffset=$(awk '{print ($1 < -0.0001 || $1 > 0.0001)}' <<< "$data")

        # h=1 means the offset is >30s, and so we will use a single adjtimex+sleep+adjtimex with 
        #    a calculated sleep time instead of short sleeps and checking progress to know when to stop
        # l=1 means there is a very large offset (>0.1 seconds), 
        #    so the time is being sped up or slowed down by the maximum amount to catch up
        #    the maximum is only 10% faster/slower, so if the clock is off by x minutes, then
        #    it will take at least 10*x minutes to correct it (a clock off by 2h takes 20h)
        # s=1 means there is a small offset (>0.001 seconds), so time is being more gradually changed to match
        #    so it doesn't flap up and down while adjusting (bash is slow, so good timing is not possible)
        # t=1 means there is a very small offset (>0.0001 seconds), more gradual than the small
        # pos=1 means the offset is positive (the remote clock is set later than this clock)
        echo "h=$hugeoffset l=$largeoffset s=$smalloffset t=$tinyoffset pos=$positive $data s"
        if [ "$tinyoffset" = 0 ]; then
            echo "Done"
            break
        else
            if [ "$positive" = 1 ]; then
                if [ "$largeoffset" = 1 ]; then
                    adjtimex --tick 11000 ; sleep 1 ; adjtimex --tick "$targetTick"
                elif [ "$smalloffset" = 1 ]; then
                    adjtimex --tick 10090 ; sleep 0.5 ; adjtimex --tick "$targetTick"
                else
                    adjtimex --tick 10010 ; sleep 0.2 ; adjtimex --tick "$targetTick"
                fi
            else
                if [ "$largeoffset" = 1 ]; then
                    adjtimex --tick 9000 ; sleep 1 ; adjtimex --tick "$targetTick"
                elif [ "$smalloffset" = 1 ]; then
                    adjtimex --tick 9910 ; sleep 0.5 ; adjtimex --tick "$targetTick"
                else
                    adjtimex --tick 9990 ; sleep 0.2 ; adjtimex --tick "$targetTick"
                fi
            fi
        fi
        
        if ! grep -qE "bc.local$|^10\." <<< "$timeserver"; then
            # make sure not to violate rate limiters on internet servers
            sleep 5
        fi
    done
    adjtimex --tick "$targetTick"
    hwclock --systohc
}

main() {
    while ps -ef | grep -q pup[p]et; do
        echo "Waiting for puppet agent to finish"
        sleep 1
    done
    echo "Disabling puppet..."
    sed -i -r "s/^([^# ]+) ([^ ]+) (.*puppet agent)/\1 0 \3/" /etc/cron.d/puppet
    
    echo "Stopping ntpd"
    /etc/init.d/ntp stop
    
    echo "Setting default tick and frequency"
    adjtimex --tick 10000 --frequency 0
    
    ## my drift thing is not the best quality, and (if tick and frequency start out right) ntpd does it fine and undoes all the work it does, so not necessary
    #autotimedrift
    
    autotimesync
    
    echo "Starting ntpd, and deleting old drift data"
    rm /var/lib/ntp/ntp.drift
    /etc/init.d/ntp start
    
    echo "Enabling puppet"
    sed -i -r "s/^([^ ]+) 0 (.*puppet agent)/\1 * \2/" /etc/cron.d/puppet
    
}

if [ "$1" = "status" ]; then
    adjtimex --print
    ntpq -nc pe
else
    main
fi
