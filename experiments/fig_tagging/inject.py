#!/usr/bin/env python3
# Inject VLAN-tagged UDP frames toward the TSN node's NIC (run as root on a peer
# node whose NIC sits on the isolated TSN segment). Frame layout mirrors
# tests/dataplane_e2e.sh inject-rx; adds PCP, per-frame seq payload and pacing.
#
# usage: inject.py IFACE DST_MAC VLAN PCP SRC_IP[,SRC_IP2] DST_IP DST_PORT COUNT [PACE_MS]
#   SRC_IP may be "a,b": frames alternate a,b,a,b... (concurrent OT/IT mix);
#   COUNT is per source IP.
import sys, struct, socket, time

iface, dmac = sys.argv[1], sys.argv[2]
vlan, pcp = int(sys.argv[3]), int(sys.argv[4])
sips = sys.argv[5].split(",")
dip, dport = sys.argv[6], int(sys.argv[7])
count = int(sys.argv[8]) * len(sips)
pace = float(sys.argv[9]) / 1000.0 if len(sys.argv) > 9 else 0.005

mac = lambda s: bytes(int(x, 16) for x in s.split(":"))
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.bind((iface, 0))
src = s.getsockname()[4] or mac("02:00:00:00:99:99")

TS_MAGIC = 0x54534E31  # "TSN1" — rx.c ts_probe_seq stamps t3 into rx_ts[seq] on
                       # the fast path when the payload starts with magic+seq.
for i in range(count):
    sip = sips[i % len(sips)]
    pay = struct.pack(">IIQ", TS_MAGIC, i, 0)  # magic, seq, t0(unused)
    eth = mac(dmac) + src + struct.pack("!H", 0x8100)
    dot1q = struct.pack("!H", (pcp << 13) | (vlan & 0xFFF)) + struct.pack("!H", 0x0800)
    udp = struct.pack("!HHHH", 12345, dport, 8 + len(pay), 0) + pay
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), i & 0xFFFF, 0, 64, 17, 0,
                     socket.inet_aton(sip), socket.inet_aton(dip))
    # IP header checksum — rx.c updates it incrementally, so the base must be valid
    c = sum(struct.unpack("!10H", ip)); c = (c >> 16) + (c & 0xFFFF); c += c >> 16
    ip = ip[:10] + struct.pack("!H", (~c) & 0xFFFF) + ip[12:]
    s.send(eth + dot1q + ip + udp)
    time.sleep(pace)
print("sent %d tagged UDP (vlan %d pcp %d) %s -> %s:%d" % (count, vlan, pcp, "/".join(sips), dip, dport))
