#!/usr/bin/env bash
# RX XDP DNAT verification.
# Proves: a packet sent to the NODE TSN endpoint arrives in the pod with its
# dst IP rewritten to the POD IP (DNAT) via XDP redirect, bypassing kube-proxy.
#
# Strategy: capture inside the pod netns (ephemeral netshoot container) while a
# sender on another node fires UDP at the node endpoint. If the pod-side capture
# shows dst == POD_IP, the header was rewritten -> DNAT+redirect confirmed.
set -u
NS=dratsn-test
RXPOD=vplc-rx
NODE=${NODE:-alpha-w1}          # node hosting the RX stream / pod
SENDER_NODE=${SENDER_NODE:-alpha-w2}
ENDPOINT_IP=${ENDPOINT_IP:-192.168.1.1}   # node TSN NIC endpoint (rx_flows key)
PORT=4840
PIN=/sys/fs/bpf/tsn

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
ok(){ printf '%s[OK]%s   %s\n' "$GRN" "$RST" "$*"; }
no(){ printf '%s[FAIL]%s %s\n' "$RED" "$RST" "$*"; }
inf(){ printf '%s[..]%s   %s\n' "$YEL" "$RST" "$*"; }

hostsh(){ kubectl debug "node/$1" -it -q --image=busybox:1.36 --profile=sysadmin -- \
            chroot /host sh -c "$2" </dev/null 2>/dev/null | grep -v 'Creating debugging'; }

echo "=== 0. deploy RX pod ==="
kubectl apply -f - >/dev/null <<EOF
apiVersion: resource.k8s.io/v1
kind: ResourceClaimTemplate
metadata: { name: tsn-rx-cell-a, namespace: $NS }
spec:
  spec:
    devices:
      requests:
        - name: tsn-stream
          exactly:
            deviceClassName: tsn.dra.net
            selectors:
              - cel: { expression: "device.attributes['tsn.dra.net'].domainId == 'cell-A' && device.attributes['tsn.dra.net'].direction == 'RX'" }
---
apiVersion: v1
kind: Pod
metadata: { name: $RXPOD, namespace: $NS, labels: { app: vplc } }
spec:
  restartPolicy: Never
  containers:
    - name: vplc
      image: busybox:1.36
      command: ["sh","-c","echo RX up; nc -u -l -p $PORT"]
      resources: { claims: [{ name: tsn }] }
  resourceClaims:
    - { name: tsn, resourceClaimTemplateName: tsn-rx-cell-a }
EOF

if ! kubectl wait -n $NS pod/$RXPOD --for=condition=Ready --timeout=60s >/dev/null 2>&1; then
  no "RX pod not Ready"; kubectl get pod -n $NS $RXPOD -o wide; exit 1
fi
POD_IP=$(kubectl get pod -n $NS $RXPOD -o jsonpath='{.status.podIP}')
POD_NODE=$(kubectl get pod -n $NS $RXPOD -o jsonpath='{.spec.nodeName}')
ok "RX pod Ready: IP=$POD_IP node=$POD_NODE"
[ "$POD_NODE" = "$NODE" ] || inf "pod node ($POD_NODE) != expected $NODE; using $POD_NODE"
NODE=$POD_NODE

echo; echo "=== 1. static: map + attach on $NODE ==="
DUMP=$(hostsh "$NODE" "echo RXFLOWS:; bpftool map dump pinned $PIN/rx_flows; echo DEVMAP:; bpftool map dump pinned $PIN/xdp_devmap; echo NET:; bpftool net show dev eno1")
echo "$DUMP" | sed 's/^/    /'
echo "$DUMP" | grep -q 'tsn_rx' && ok "tsn_rx XDP attached on eno1" || no "tsn_rx not attached on eno1"
echo "$DUMP" | grep -qi 'pod_ip\|"value"' && ok "rx_flows has entries" || inf "rx_flows may be empty"

echo; echo "=== 2. live: capture in pod netns while sending to node endpoint ==="
CAP=/tmp/rx_dnat_cap.$$
inf "starting pod-netns capture (eth0) for ~18s ..."
timeout 18 kubectl debug -n $NS pod/$RXPOD -q --image=nicolaka/netshoot --target=vplc -- \
  tcpdump -i eth0 -n -l udp port $PORT >"$CAP" 2>/dev/null &
CAPPID=$!
sleep 8   # let ephemeral container start + tcpdump attach

inf "sending 6 UDP packets $SENDER_NODE -> $ENDPOINT_IP:$PORT ..."
hostsh "$SENDER_NODE" "for i in 1 2 3 4 5 6; do echo rxdnat-\$i | nc -u -w1 $ENDPOINT_IP $PORT; sleep 0.5; done" >/dev/null

wait $CAPPID 2>/dev/null
echo "--- pod eth0 capture ---"
sed 's/^/    /' "$CAP"

echo; echo "=== 3. verdict ==="
if grep -q "> ${POD_IP}.${PORT}" "$CAP"; then
  ok "DNAT CONFIRMED: packet arrived in pod with dst=$POD_IP (rewritten from $ENDPOINT_IP)"
elif grep -q "$ENDPOINT_IP" "$CAP"; then
  no "packet reached pod netns but dst still $ENDPOINT_IP (DNAT did NOT rewrite)"
else
  no "no packet in pod netns — redirect/L2 drop. Checking eno1 ingress for arrival..."
  hostsh "$NODE" "timeout 6 tcpdump -i eno1 -n -c 3 udp port $PORT 2>/dev/null" &
  TPID=$!
  sleep 1
  hostsh "$SENDER_NODE" "for i in 1 2 3; do echo probe-\$i | nc -u -w1 $ENDPOINT_IP $PORT; sleep 0.5; done" >/dev/null
  wait $TPID 2>/dev/null
  inf "if eno1 saw it but pod didn't -> XDP redirect or L2/MAC issue (rx.c v1 L3/L4-only)"
fi

echo; echo "=== pod nc receipt (end-to-end) ==="
kubectl logs -n $NS $RXPOD 2>/dev/null | sed 's/^/    /'

rm -f "$CAP"
echo; echo "(cleanup: kubectl delete pod -n $NS $RXPOD; kubectl delete resourceclaimtemplate -n $NS tsn-rx-cell-a; delete node-debugger pods)"
