#!/bin/bash
#
# Author: Peter Maloney
# License: GPLv2
#
# script that works like ntpd ... keeps time based on an ntp server
# just a quick experiment to see if it's easy to be better than ntpd (without rapid polls which I know already is better)

# prerequisite commands: bsd rdate, adjtimex, bc
ntp_server="$1"

if [ -z "$ntp_server" ]; then
    echo "USAGE: $0 <ntp server>"
fi

get_offset() {
    rdate -npv "$ntp_server" | awk '$NF=="seconds"{print $(NF-1)}'
}

get_config_hz() {
    if [ -e /proc/config.gz ]; then
        gunzip -c /proc/config.gz | grep "^CONFIG_HZ="
    elif [ -e /boot/config-$(uname -r) ]; then
        grep "^CONFIG_HZ=" /boot/config-$(uname -r)
    else
        grep "^CONFIG_HZ=" /boot/config-$(uname -r)* /boot/config-* | head -n1
    fi | awk -F= '{print $2}'
}

floattest() {
    lhs="$1"
    op="$2"
    rhs="$3"
    
    l=$(echo "$lhs * 1000000" | bc | sed -r 's/^([0-9]+).*/\1/')
    r=$(echo "$rhs * 1000000" | bc | sed -r 's/^([0-9]+).*/\1/')
    test "$l" "$op" "$r"
    return $?
}

time_correction() {
    interval=30
    
    tick=$(adjtimex --print | awk '$1=="tick:" {print $2}')
    freq=$(adjtimex --print | awk '$1=="frequency:" {print $2}')
    
    # record offset
    offset1=$(get_offset)

    echo "before: tick = $tick, freq = $freq, offset1 = $offset1"

    # wait and measure again to get drift
    sleep $interval
    
    # record offset
    offset2=$(get_offset)
    
    # calculate drift per 10s
    drift=$(echo "scale=6; ($offset2 - $offset1) / $interval * 10" | bc)
    
    offset_positive=$(sed -r 's/-//' <<< "$offset2")
    
    # ----------------------------
    # first make the clock correct
    # ----------------------------
    # set a goal to sync time as fast as possible, and set the tick+freq to make it correct that fast
    adj_tick=10
    if floattest "$offset_positive" -lt 0.000100; then
        adj_tick=1
    fi
    
    if grep -q "^-" <<< "$offset2"; then
        temp_tick=$((tick-adj_tick))
        temp_freq="$freq"
    else
        temp_tick=$((tick+adj_tick))
        temp_freq="$freq"
    fi
    
    config_hz=$(get_config_hz)
    
    sleep_time=$(echo "scale=3; $offset_positive / 0.001 * 10 / $adj_tick" | bc)
    echo "step1: tick = $temp_tick, freq = $temp_freq, offset2 = $offset2, drift = $drift, adj_tick = $adj_tick, sleep_time = $sleep_time"
    adjtimex --tick "$temp_tick" --freq "$temp_freq"
    sleep "$sleep_time"
    adjtimex --tick "$tick" --freq "$freq"
    
    # DEBUG: print out result
    #offset=$(get_offset)
    #echo "after: tick = $tick, freq = $freq, offset2 = $offset"
    
    # ----------------------------
    # then set tick+freq to match drift
    # ----------------------------
    # estimate how much we should change freq... about -1500000 per 0.000200 drift on 250Hz
    # but we just do 10% of that, so we can fine tune it with multiple runs
    drift_positive=$(sed -r 's/-//' <<< "$drift")
    change_freq=$(echo "scale=10; 1500000 * ($drift_positive / 0.0002) * (250 / $config_hz) / 100" | bc)
    change_sign="+"
    if grep -q "^-" <<< "$drift"; then
        change_sign="-"
    fi
    new_freq=$(echo "$freq $change_sign $change_freq" | bc | awk -F. '{print $1}')
    freq="$new_freq"
    while [ "$freq" -gt "6553600" ]; do
        # TODO: this assumes USER_HZ=100
        tick=$(echo "$tick + 1" | bc)
        freq=$(echo "$freq - 6553600" | bc)
    done
    while [ "$freq" -lt "-6553600" ]; do
        # TODO: this assumes USER_HZ=100
        tick=$(echo "$tick - 1" | bc)
        freq=$(echo "$freq + 6553600" | bc)
    done
    echo "step2: change_freq = $change_freq, tick = $tick, freq = $freq"
    adjtimex --tick "$tick" --freq "$freq"
}

echo "Starting time correction loop"
while true; do
    time_correction
done
