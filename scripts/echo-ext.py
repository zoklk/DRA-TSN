#!/usr/bin/env python3
# Actuator-side ECHO for round-trip latency (ADR-0028). Runs on the external node
# (sandbox-worker1), as root. Captures the vPLC's TX probe frames (raw AF_PACKET,
# magic-filtered) and immediately reflects each one back to the vPLC node's RX TSN
# endpoint as "sensor feedback" — VLAN-tagged, src=talker IP, dst=node NIC MAC — so
# the w1 RX data plane (tsn_rx) DNATs it into the pod. The payload (incl. seq) is
# preserved so the pod can pair the echo to its send. NO clock/PTP work here — all
# timing is measured on the w1 side.
#
#   sudo IFACE=eno1 NODE_MAC=<w1 eno1 MAC> python3 echo-ext.py
#   env: PORT=4840 RX_VLAN=100 TALKER_IP=192.168.1.20 ENDPOINT_IP=192.168.1.1 COUNT=10000 TIMEOUT=40
import os, socket, struct, time, sys

IFACE=os.environ.get("IFACE","eno1"); PORT=int(os.environ.get("PORT","4840"))
NODE_MAC=os.environ["NODE_MAC"]                 # w1 eno1 MAC (echo frame dst) — REQUIRED
RX_VLAN=int(os.environ.get("RX_VLAN","100")); TALKER=os.environ.get("TALKER_IP","192.168.1.20")
ENDPOINT=os.environ.get("ENDPOINT_IP","192.168.1.1")
COUNT=int(os.environ.get("COUNT","10000")); TIMEOUT=float(os.environ.get("TIMEOUT","40"))
MAGIC=0x54534E31
mac=lambda s: bytes(int(x,16) for x in s.split(':'))

ETH_P_ALL=3
s=socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
s.bind((IFACE,0)); s.settimeout(TIMEOUT)
src_mac=s.getsockname()[4] or mac("02:00:00:00:99:99")
dmac=mac(NODE_MAC)

def find_probe(f):
    if len(f)<14: return None
    et=struct.unpack_from("!H",f,12)[0]; off=14
    if et==0x8100:
        if len(f)<18: return None
        et=struct.unpack_from("!H",f,16)[0]; off=18
    if et!=0x0800 or len(f)<off+20: return None
    ihl=(f[off]&0x0f)*4
    if f[off+9]!=17: return None
    udp=off+ihl
    if len(f)<udp+8 or struct.unpack_from("!H",f,udp+2)[0]!=PORT: return None
    pl=udp+8
    if len(f)<pl+16: return None
    magic,seq,t0=struct.unpack_from(">IIQ",f,pl)
    if magic!=MAGIC: return None
    return seq,t0

def build_echo(seq,t0):
    eth=dmac+src_mac+struct.pack("!H",0x8100)
    d1q=struct.pack("!H",(5<<13)|(RX_VLAN&0xfff))+struct.pack("!H",0x0800)
    pay=struct.pack(">IIQ",MAGIC,seq,t0)
    udp=struct.pack("!HHHH",12345,PORT,8+len(pay),0)+pay
    ip=struct.pack("!BBHHHBBH4s4s",0x45,0,20+len(udp),1,0,64,17,0,
                   socket.inet_aton(TALKER),socket.inet_aton(ENDPOINT))
    c=sum(struct.unpack("!10H",ip)); c=(c>>16)+(c&0xffff); c+=c>>16; c=(~c)&0xffff
    ip=ip[:10]+struct.pack("!H",c)+ip[12:]
    return eth+d1q+ip+udp

n=0
print("echo: reflecting probes %s:%d -> vlan%d %s->%s (dst_mac=%s)"%(IFACE,PORT,RX_VLAN,TALKER,ENDPOINT,NODE_MAC), file=sys.stderr)
while n<COUNT:
    try: f=s.recv(2048)
    except socket.timeout: print("timeout; stop", file=sys.stderr); break
    r=find_probe(f)
    if not r: continue
    s.send(build_echo(*r)); n+=1
print("echoed %d frames"%n, file=sys.stderr)
