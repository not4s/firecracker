#!/usr/bin/env bash
# Live-path perf for the ARM MemoryRegionCache regression (FC-DEV-2926).
# Boots a firecracker binary, runs an h2g iperf, perf-stats the FC process.
# Usage: sudo bash perf_run.sh /root/fc_main main     (then again with /root/fc_poc poc)
# Prereqs on the box: /root/fc_main, /root/fc_poc built; /root/firecracker has
# vmlinux-*, ubuntu-*.ext4, ubuntu-*.id_rsa; perf installed; run as root.
set -u
BIN="${1:?usage: perf_run.sh <fc-binary> <tag>}"
TAG="${2:?usage: perf_run.sh <fc-binary> <tag>}"
FCDIR=/root/firecracker
GUEST_IP=172.16.0.2
TAP=tap0
SOCK=/tmp/fc-$TAG.sock
LOG=/root/fc-$TAG.log
KERNEL=$(ls $FCDIR/vmlinux* | tail -1)
ROOTFS=$(ls $FCDIR/*.ext4 | tail -1)
KEY=$(ls $FCDIR/*.id_rsa | tail -1)
# Split into two groups of <=6 so the PMU doesn't multiplex counters out to <not counted>.
COUNTERS_A="cycles,instructions,stall_backend,l1d_cache,l1d_cache_refill,l2d_cache_refill"
COUNTERS_B="cycles,instructions,ll_cache_miss_rd,mem_access,br_mis_pred_retired,stall_frontend"

echo "== $TAG: bin=$BIN kernel=$KERNEL rootfs=$ROOTFS =="
echo -1 > /proc/sys/kernel/perf_event_paranoid
# host needs iperf3 to drive traffic (AL2023: dnf)
which iperf3 >/dev/null 2>&1 || dnf install -y iperf3 >/dev/null 2>&1 || echo "WARN: could not install iperf3 on host"

# (re)create the tap (idempotent)
ip link del $TAP 2>/dev/null || true
ip tuntap add dev $TAP mode tap
ip addr add 172.16.0.1/30 dev $TAP
ip link set dev $TAP up
sh -c "echo 1 > /proc/sys/net/ipv4/ip_forward"

# write JSON config files (no shell-escaping to mangle)
cat > /tmp/boot.json   <<JSON
{"kernel_image_path":"$KERNEL","boot_args":"keep_bootcon console=ttyS0 reboot=k panic=1"}
JSON
cat > /tmp/rootfs.json <<JSON
{"drive_id":"rootfs","path_on_host":"$ROOTFS","is_root_device":true,"is_read_only":false}
JSON
cat > /tmp/net.json    <<'JSON'
{"iface_id":"net1","guest_mac":"06:00:AC:10:00:02","host_dev_name":"tap0"}
JSON
cat > /tmp/mach.json   <<'JSON'
{"vcpu_count":1,"mem_size_mib":1024}
JSON

rm -f $SOCK
$BIN --api-sock $SOCK > $LOG 2>&1 &
FCPID=$!
sleep 1
C() { curl -s -X PUT --unix-socket $SOCK --data @"$1" "http://localhost/$2"; }
C /tmp/boot.json   boot-source
C /tmp/rootfs.json drives/rootfs
C /tmp/net.json    network-interfaces/net1
C /tmp/mach.json   machine-config
curl -s -X PUT --unix-socket $SOCK --data '{"action_type":"InstanceStart"}' http://localhost/actions

echo "booting (pid $FCPID)..."
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -i $KEY root@$GUEST_IP"
BOOTED=0
for i in $(seq 1 10); do
  if ! kill -0 $FCPID 2>/dev/null; then echo "!! FC PROCESS DIED. last log:"; tail -8 $LOG; exit 1; fi
  if $SSH "echo GUEST_OK" 2>/dev/null | grep -q GUEST_OK; then BOOTED=1; break; fi
  echo "  guest not up yet ($i)"; sleep 3
done
if [ $BOOTED -ne 1 ]; then
  echo "!! GUEST UNREACHABLE after 30s. FC log tail:"; tail -12 $LOG
  kill $FCPID 2>/dev/null; exit 1
fi
echo "GUEST UP."

# iperf3 server in guest; ensure present
$SSH "which iperf3 >/dev/null 2>&1 || (apt-get update && apt-get install -y iperf3)" >/dev/null 2>&1
$SSH "pkill iperf3 2>/dev/null; iperf3 -s -D" 2>/dev/null
sleep 1
# h2g transfer = data flows host->guest = FC RX path (writes into guest mem, the
# regressing direction). Topology here is server-in-guest + client-on-host, so the
# host client must SEND (NO -R): host->guest. (-R would make the host client receive
# = g2h, the WRONG direction; the A/B test gets h2g via server-on-host+client-in-guest+-R.)
# Long enough to cover warmup + two perf passes (6 + 12 + 12 + margin).
( iperf3 -c $GUEST_IP -t 40 >/tmp/iperf-$TAG.txt 2>&1 ) &
IPERF=$!
sleep 6   # warmup / steady state
echo "==== PERF $TAG group A (FC pid $FCPID) ===="
perf stat -e "$COUNTERS_A" -p $FCPID -- sleep 12
echo "==== PERF $TAG group B (FC pid $FCPID) ===="
perf stat -e "$COUNTERS_B" -p $FCPID -- sleep 12
echo "==== END PERF $TAG ===="
wait $IPERF 2>/dev/null
tail -3 /tmp/iperf-$TAG.txt
kill $FCPID 2>/dev/null
sleep 1
echo "== $TAG DONE =="
