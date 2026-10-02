/* Config + probe marker for the actuator XDP reflect (examples/mock-actuator).
 *
 * Defaults mirror scripts/env.sh (TX_DST_PORT / RX_VLAN / RX_TALKER_IP /
 * RX_ENDPOINT_IP). Override any of them at build time without editing this file:
 *   make CFLAGS_EXTRA='-DECHO_VID=200 -DECHO_PCP=3'
 */
#ifndef __REFLECT_H__
#define __REFLECT_H__

/* Probe payload marker — identical to the data plane's TS_MAGIC ("TSN1",
 * big-endian on the wire). Only frames carrying it are reflected. */
#define TS_MAGIC       0x54534E31u
#define TS_PAYLOAD_MIN 16          /* magic(4) + seq(4) + t0(8) */

/* Echo addressing. Defaults = arm-1 (192.168.1.11, RX node-endpoint port 4840).
 * For arm-2 (192.168.1.12, RX port 4841) build with:
 *   make CFLAGS_EXTRA='-DECHO_SRC_IP=0xC0A8010Cu -DECHO_DPORT=4841'
 * IPs are HOST-order 32-bit (the program applies bpf_htonl).
 * src   = sensor/talker source (rx_flows key src_ip   = this arm's IP);
 * dst   = node TSN receive endpoint (rx_flows key orig_dst_ip = RX_ENDPOINT_IP);
 * dport = the arm's RX node-endpoint port (rx_flows key dst_port). */
#ifndef ECHO_SRC_IP
#define ECHO_SRC_IP 0xC0A8010Bu    /* 192.168.1.11  (arm-1 talker)  */
#endif
#ifndef ECHO_DST_IP
#define ECHO_DST_IP 0xC0A80101u    /* 192.168.1.1   (RX_ENDPOINT_IP)*/
#endif
#ifndef ECHO_DPORT
#define ECHO_DPORT 4840            /* arm-1 RX node-endpoint port (arm-2 = 4841) */
#endif

/* Echo VLAN tag — the RX stream's VID/PCP, which differ from the TX tag the
 * incoming probe carries (so this is a rewrite, not a passthrough). */
#ifndef ECHO_VID
#define ECHO_VID 100               /* RX_VLAN */
#endif
#ifndef ECHO_PCP
#define ECHO_PCP 5                 /* RX stream priority */
#endif

#endif /* __REFLECT_H__ */
