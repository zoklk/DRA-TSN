# fig_tagging — per-flow selective bypass (without/with ablation)

**목적**: 같은 pod에서 OT/IT 트래픽을 **동시에 interleave**로 흘려, 등록된
flow(4-tuple/talker 일치)만 TSN fast path(TX 태깅 / RX DNAT+redirect)를 타고
나머지는 기존 cilium/host 경로를 유지함을 패킷 단위로 보인다.
figure 문법은 TSN metadata proxy 논문(arXiv:2503.12878 Fig.2,
`../../figures/f11_example.png`)의 without/with 상하 ablation을 따른다.

**전제**: **실클러스터 전용** — TSN 노드(native XDP NIC)와 주입 노드가 같은 L2
세그먼트. 노드에 tcpdump·xdp-tools(xdpdump), 로컬에 tshark.
ssh: `export SSH_PASS='<pw>'` (키 인증이면 비워둠).

**시나리오** (run.sh가 순서대로 수행):
1. RX baseline — mock-vplc 삭제로 `rx_flows` 빈 상태 확인(bpftool) 후 주입
2. TX baseline — claim 없는 pod(vplc-baseline)에서 동일 트래픽 송신
3. mock-vplc 재배포(tx_flows 프로그래밍 확인) → TX mix → RX mix

**실행**:
```bash
FRAMES=200 RUN=myrun ./run.sh      # 캡처 수집 (pcap)
./parse.sh results/myrun           # tshark → summary.csv + packets.csv
../.venv/bin/python plot.py results/myrun
```

**출력**: `results/<RUN>/`: `tx_base|tx_mix.pcap`, `rxbase_*|rx_*.pcap`,
`summary.csv`(집계), `packets.csv`(패킷별), `rx_ts.json`+`rx_latency.csv`(프레임별
t3→pod 지연), `fig_tagging.png`(TX 분류), `fig_tagging_rx.png`(논문 f12 — 아래).

**RX figure (f12) = pod 도달 + fast path 지연**: x=주입 seq(OT 짝수/IT 홀수),
y=XDP entry(t3)→pod 소켓 도달 지연(µs), 미도달 프레임은 상단 "not delivered"
밴드. 주입 payload가 probe 포맷(TSN1 magic+seq)이라 rx.c가 fast path에서
t3(`bpf_ktime_get_tai_ns`)를 `rx_ts`에 스탬프하고, pod tcpdump(realtime)와
커널 TAI−REALTIME offset(실측값, run.sh가 meta.env에 기록)으로 결합해 계산.
주의: xdpdump 자체 pcap 클럭은 realtime과 미지 상수만큼 어긋나 있어(실측 +14s대)
지연 계산에 쓸 수 없다 — 반드시 rx_ts 경유.

**검증**: summary.csv 기대 패턴 — tx-ot 100% tagged / tx-it·txbase-* 100%
untagged / rx-ot: xdp N·wire 0·pod N(delivered=N) / rx-it: xdp N·wire N·pod 0 /
rxbase-*: pod 0.

## taprio 시나리오 (`run_taprio.sh` — 논문 f11)

**목적**: ADR-0008(`bpf/tx.c`의 `skb->priority = pcp`)이 실제 taprio 게이트에
반영됨을 arXiv:2503.12878 Fig.2와 같은 사이클 산점도로 보인다.
w1 eno1에 **software taprio**(cycle 10ms: prio 6 게이트 [0,2.5ms) / BE [2.5,10ms))를
실험 중에만 설정 — igb는 hw offload(LaunchTime) 미지원이며, 참고 논문과
동일하게 "논점을 보이기 위한 게이트 구성"임을 본문에 명시할 것.

```bash
FRAMES=200 RUN=myrun ./run_taprio.sh    # taprio 설정→(a)/(b) 캡처→원복(trap)
./parse_taprio.sh results/myrun         # 사이클 오프셋 계산 + 윈도우 점유 verdict
../.venv/bin/python plot.py results/myrun   # fig_tagging_taprio.png
```

기대: (a) claimless pod = OT/IT 전부 BE 윈도우(게이트 개방 직후 pile-up 포함),
(b) claimed pod = OT 100% TSN 윈도우(+vlan 200/pcp 6 태깅), IT 100% BE 윈도우.
송신 pace 13ms — 10ms 사이클과 서로소라 전 위상 커버(aliasing 방지).
base-time은 TAI 10ms 배수 (TAI-UTC 37s도 10ms 배수 → realtime 캡처 스탬프로
mod-cycle 계산이 정확).

**캡처 주의**: tcpdump 필터는 plain 절을 `vlan` 절보다 앞에
(`udp ... or (vlan and udp ...)`) — 커널이 VLAN을 skb 메타데이터로 strip한
프레임은 `vlan` 선행 필터에 걸리지 않는다. RX fast path 프레임은 eno1
tcpdump에 안 보이므로(XDP redirect가 스택 우회) xdpdump + pod 내부 캡처 사용.
