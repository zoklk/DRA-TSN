# DRA-TSN

가상 PLC의 결정론적 통신을 위해, CNC가 계산한 **TSN 경로를 Kubernetes 표준 자원 모델로
편입**하고 그 배치 결정을 **eBPF로 프레임 수준에서 집행**하는 프레임워크.

**문제.** 제어를 가상화하면 결박은 사라지지 않고 하드웨어에서 **배치 위치**로 옮겨간다.
TSN 경로는 CNC가 특정 스위치 포트와 특정 노드 NIC을 잇도록 사전 계산·배포하므로,
현장 장치와 결정론적으로 통신할 수 있는 Pod는 그 경로가 닿아 있는 노드의 Pod뿐이다.
그러나 K8s 스케줄러에게 그 경로는 보이지 않는다. CPU와 메모리만 보고 배치하며, 경로가
없는 노드에 vPLC가 착지하면 성능 저하가 아니라 **제어 주기 마감 위반**이 된다.

**해법은 두 요구사항으로 나뉜다.**

- **경로 인식** — CNC가 배포한 스트림을 DRA의 ResourceSlice 장치로 게시한다
  (1 스트림 = 1 장치). 스케줄러 교체 없이, 표준 Filter가 경로 보유 노드로 배치를
  한정한다. 틀리게 놓느니 Pending으로 둔다.
- **경로 집행** — 배치된 Pod의 트래픽 중 **사전에 선언된 스트림과 일치하는 흐름만**
  eBPF가 VLAN/PCP 태깅하여 보호 경로로 내보낸다(TX). 수신은 노드 endpoint를 Pod로
  DNAT·redirect하여 kube-proxy와 conntrack을 우회한다(RX). 나머지 트래픽은 기존 CNI가
  그대로 처리한다.

eBPF는 트래픽을 관측해 스트림을 **추론**하는 수단이 아니라, 제어 평면이 선언한 스트림을
**집행**하는 수단이다. 경로의 계산과 우선순위는 CNC의 권한으로 남는다. 이 프레임워크는
이미 깔린 경로 집합 위에서 **워크로드의 위치를 정하는 문제**를 푼다.

---

## 아키텍처 (4 컴포넌트)

```
[OT] Actuator/Sensor ──┐
                       ├── TSN Switch (CNC가 GCL 배포)
[OT] Sensor    ────────┘
                       │  ↑ 정적 TSN 경로
[Control plane]        │      TSN Controller (Deployment, leader-elected singleton)
                       │        ├ CNC Client   — RESTCONF(RFC 8040) SSE 구독 (read-only)
                       │        └ Stream Mapper — TSNNic × CNC topology join → TSNStream CR
                       │            ▲ TSNNic CR        │ TSNStream CR (watch)
[Edge Node]            │            │                  ▼
  └ DRA Plugin (DaemonSet, hostNetwork/privileged)
      ├ NIC Discovery         — TSN-capable NIC → TSNNic CR
      ├ ResourceSlice Publisher — 자기 노드 stream → device 게시 (1 stream = 1 device)
      ├ NodePrepareResources  — claim 할당 → pod→stream config를 BoltDB checkpoint 저장
      ├ NRI Plugin            — pod 기동 시 bpf map insert + veth XDP attach + env 주입
      └ eBPF
          ├ TX (host-side veth TCX HEAD ingress) — map-hit→(SNAT)+L2 rebuild+VLAN/PCP 태깅+skb mark+bpf_redirect(NIC), miss→TCX_NEXT(cilium)
          ├ TX guard (물리 NIC TCX HEAD egress)  — skb mark=TSN_TX_MARK→TCX_PASS(cilium VLAN 필터 우회), else→TCX_NEXT
          ├ RX (물리 NIC XDP ingress)            — VLAN 태그 필수(untagged→PASS)→strip+map-hit→DNAT+devmap redirect, miss→XDP_PASS
          └ veth dummy XDP (pod eth0)            — RX redirect 착지 활성화
```

격리 모델: **스트림 단위 선택 우회 + host-ns 격리.** vPLC는 일반 cilium Pod, bpf map-match 패킷만
전용 평면을 탄다 (map miss = cilium 정상 경로). TX는 pod egress를 host-side veth ingress에서 잡아
host 라우팅/netfilter/conntrack/cilium *前*에 NIC으로 직접 redirect → TSN flow를 host ns에서 격리.
redirect는 skb 경로(`dev_queue_xmit`)라 NIC qdisc(taprio/etf) 통과 = SW 시분할 셰이핑 호환.
cilium 무침습(agent 코드/config/restart 미변경).

---

## 디렉터리 구조 / 컴포넌트 구현 위치

```
DRA-TSN/
├── api/v1alpha1/               # CRD 타입 (Phase 1)
│   ├── groupversion_info.go    #   group tsn.dra.net/v1alpha1, scheme 등록
│   ├── tsnnic_types.go         #   TSNNic   (노드 TSN-capable NIC 인벤토리)
│   ├── tsnstream_types.go      #   TSNStream (CNC stream × NIC join 결과, TX/RX)
│   └── zz_generated.deepcopy.go#   deepcopy (수기 작성, controller-gen 불요)
│
├── cmd/
│   ├── controller/main.go      # TSN Controller (Deployment) — manager + leader election + Mapper
│   ├── kubeletplugin/main.go   # DRA Plugin (DaemonSet) — manager + DRA helper + NIC discovery
│   │                           #   + ResourceSlice republisher + eBPF loader + NRI 배선
│   └── mock-cnc/main.go        # mock CNC — RESTCONF GET + SSE on-change + admin API
│
├── pkg/
│   ├── cnc/                    # Phase 1: CNC observer
│   │   ├── types.go            #   Stream/Endpoint/Event 데이터 모델
│   │   └── client.go           #   CNCClient 인터페이스 + HTTP/SSE mock 구현
│   ├── controller/mapper.go    # Phase 1: Stream Mapper — join + 풀싱크 reconcile
│   ├── inventory/              # Phase 2: NIC discovery (라벨 기반)
│   │   ├── discovery.go        #   Discover/ParseCapableNics → TSNNic spec
│   │   └── reconciler.go       #   NodeReconciler: capable-nics 라벨 watch → TSNNic GC/게시 + NIC (de)attach (ADR-0020)
│   ├── resourceslice/publisher.go # Phase 2: TSNStream → ResourceSlice device 변환
│   ├── dra/driver.go           # Phase 2: NodePrepare/Unprepare → checkpoint 기록
│   ├── checkpoint/store.go     # Phase 2/3: BoltDB pod→stream 체크포인트 (claim 인덱스 포함)
│   ├── nri/                    # Phase 3: NRI hooks
│   │   ├── hooks.go            #   RunPodSandbox(map insert+veth XDP)/CreateContainer(AddEnv)/
│   │   │                       #   StopPod/RemovePod(cleanup)/Synchronize(재시작 복구)
│   │   │                       #   bind 시점만 프로그래밍, 런타임 hot-patch 안 함 (ADR-0023)
│   │   └── netns.go            #   pod netns 진입 → pod IP/MAC + host-veth ifindex 추출
│   └── ebpf/                   # Phase 3: eBPF 로더
│       ├── gen.go              #   //go:generate bpf2go (tx/rx/veth)
│       ├── loader.go           #   load + pin + AttachNIC(RX XDP) + AttachVethTCX(TX) + AttachVethXDP(veth dummy)
│       ├── maps_types.go       #   map key/value Go struct (C 레이아웃 일치) + 생성자
│       └── maps_ops.go         #   PutTX/DelTX/PutRX/DelRX/PutDevmap (NRI가 사용)
│
├── bpf/                        # Phase 3: eBPF C 소스
│   ├── tsn_common.h            #   map struct + TCX 리턴코드 정의 (Go와 byte 단위 일치)
│   ├── tx.c                    #   host-veth TCX HEAD ingress: lookup→(SNAT)→L2 rebuild→vlan_push→bpf_redirect(NIC) / miss→TCX_NEXT
│   ├── rx.c                    #   XDP ingress: (vlan strip)→lookup→DNAT(incremental csum)→redirect_map / miss→XDP_PASS
│   ├── veth_dummy.c            #   XDP_PASS (redirect 착지용)
│   └── headers/vmlinux.h       #   BTF 덤프 (make vmlinux)
│
├── deployments/
│   ├── crds/{tsnnic,tsnstream}.yaml
│   ├── deviceclass.yaml        # DeviceClass tsn.dra.net
│   ├── controller.yaml         # Controller Deployment + RBAC
│   ├── daemonset.yaml          # DRA+NRI DaemonSet (전 노드, privileged/hostNetwork, bpffs/nri/kubelet 마운트)
│   ├── mock-cnc.yaml           # mock CNC Deployment + Service
│   ├── registry.yaml           # 루프용 in-cluster registry:2 (NodePort 30500)
│   ├── registry-certs.yaml     # 각 노드 containerd certs.d insecure 드롭인 DaemonSet
│   ├── examples/vplc-tx.yaml   # 예시 ResourceClaimTemplate + vPLC Pod
│   └── Dockerfile.{controller,mock-cnc,kubeletplugin}
│
├── tests/
│   ├── lib.sh                  # 공통 헬퍼 (PASS/FAIL/SKIP, wait_for, 술어)
│   ├── run_all.sh              # 전 phase 실행
│   ├── smoke_phase{1,2,3,4}_*.sh # phase별 스모크
│   └── loop.sh                 # build→push→deploy→scenario→test 루프 드라이버
│
├── scheduler/                  # Phase 5: 두 번째 스케줄러 (별도 Go 모듈)
│   ├── cmd/tsn-scheduler/      #   upstream kube-scheduler + 플러그인 등록
│   └── pkg/tsnslack/plugin.go  #   TSNSlackScore (802.1Qcc max−accumulated)
│
├── experiments/                # 평가 아티팩트 — figure당 run.sh → results/ → plot.py
│   ├── fig_placement/          #   배치 정확성 (실클러스터)
│   ├── fig_tagging/            #   태깅·선택 우회 ablation (실클러스터, XDP NIC 필요)
│   ├── fig_scale/              #   희소 자원 비율에서의 배치 정확성 (KWOK, docker만)
│   ├── fig_mixed/              #   경로 소실 후 재배치 정확성 (kind 2클러스터, 주 실험)
│   └── kind/                   #   제어평면 전용 재현 경로 (TSN NIC 불요)
│
├── figures/                    # 개념도 D2 소스 + 그림 생성 스크립트 (렌더 산출물 제외)
├── scripts/                    # 노드 준비 + 왕복 지연 측정 파이프라인
├── docs/                       # 운영 런북 (vPLC 노드 / actuator 노드 / 지연 측정 / e2e)
├── isaac-sim/                  # 디지털 섀도 — 측정된 배치를 장면으로 재생 (replay/live)
│
├── Makefile                    # build/generate(bpf2go)/vmlinux/image/push
└── go.mod                      # module tsn.dra.net/dratsn
```

---

## 설계 결정

이 저장소의 설계 결정은 40건의 ADR(맥락/결정/근거/trade-off)로 관리된다. ADR 원본은
개발 저장소에만 두고 공개하지 않으며, 그 내용은 이 README와 각 컴포넌트 디렉터리의
문서로 승계한다. 코드 주석이 결정을 참조할 때 쓰는 `ADR-00NN` 번호는 그 관리 체계의
식별자다.

---

## 데이터 스키마

각 컴포넌트가 실제로 주고받는 데이터 형상. 진실원천은 CNC stream → TSNStream → (ResourceSlice |
BoltDB checkpoint → bpf map | env)로 흐른다.

### CRD — `TSNNic` (DRA 게시 / Controller 소비)

`spec` ([`api/v1alpha1/tsnnic_types.go`](api/v1alpha1/tsnnic_types.go)):

| 필드 | 타입 | 의미 |
|---|---|---|
| `nodeName` | string | NIC 호스트 노드 |
| `ifName` | string | 커널 인터페이스명 (eno5 등) |
| `pciAddress` | string | PCI 주소 (opt) |
| `macAddress` | string | NIC MAC (opt) |
| `hostIP` | string | NIC에 바인드된 host IP = **RX 수신 endpoint** (opt) |
| `tsnCapable` | bool | TSN 판별 결과 |
| `features` | []string | 지원 기능 (taprio, hw-vlan-insert, ptp) |

### CRD — `TSNStream` (Controller 게시 / DRA 소비)

`spec` ([`api/v1alpha1/tsnstream_types.go`](api/v1alpha1/tsnstream_types.go)) — CNC stream × NIC join 결과:

| 필드 | 타입 | 의미 | 분류 |
|---|---|---|---|
| `streamId` | string | **개별** CNC stream 식별자 (예: `s-tx`) | identity |
| `nodeName` / `ifName` | string | join된 NIC | identity |
| `direction` | enum `TX`\|`RX` | 방향 | identity |
| `domainId` | string | TSN **도메인/셀 그룹** (예: `cell-A`, 802.1Qcc TSN domain) — streamId와 별개, **1 domain : N streams** | identity |
| `dst` | {`ip`,`port`,`mac?`} | TX=외부 listener / RX=노드 NIC endpoint | data-plane |
| `vlanId` | int (0–4094) | 802.1Q VLAN | data-plane |
| `pcp` | int (0–7) | 802.1p 우선도 | data-plane |
| `talker`/`listener` | {ip,mac} | endpoint 정보 (opt) | — |

> identity = ResourceClaim selector가 쓰는 불변 필드. data-plane은 pod **bind 시점에만** 적용되고
> 런타임 변경은 hot-patch하지 않는다 — 변경은 pod 재생성으로 반영 (ADR-0023).

### ResourceSlice device attribute (도메인 = `tsn.dra.net`)

1 stream = 1 device, pool = 노드명 ([`pkg/resourceslice/publisher.go`](pkg/resourceslice/publisher.go)).
CEL selector는 `device.attributes['tsn.dra.net'].<key>`로 읽음:

`streamId`(str), `ifName`(str), `direction`(str), `domainId`(str).

> **identity 필드만 발행** (ADR-0022). DRA attribute는 전부 selectable이라, data-plane 필드
> (vlanId/pcp/dst)를 노출하면 selector가 가변값(CNC가 바꿀 수 있음)에 배치를 묶어 flap을 부른다 →
> 안 내보내 불변식을 구조로 강제한다. 그 값들은 TSNStream CR(SSOT)·checkpoint에 있다.

### ResourceClaim selector (사용자 작성)

```yaml
devices:
  requests:
    - name: tx
      exactly:
        deviceClassName: tsn.dra.net
        selectors:
          - cel:
              expression: >-
                device.attributes['tsn.dra.net'].domainId == 'cell-A' &&
                device.attributes['tsn.dra.net'].direction == 'TX'
```

> selector는 **identity 필드만** 사용 (예시: domainId + direction). data-plane 필드(vlan/pcp/dst)는
> 안 씀 (ADR-0022). 예시 전체: [`deployments/examples/mock-vplc.yaml`](deployments/examples/mock-vplc.yaml).

### Pod env — `TSN_STREAMS` (NRI AddEnv 주입)

pod에 bind된 모든 stream의 JSON 배열. vlan/pcp는 미노출(eBPF 투명 태깅):

```json
[{"id":"s-tx","dir":"TX","dst":"10.0.0.5:5000"},{"id":"s-rx","dir":"RX","port":6000}]
```

### BoltDB checkpoint (pod→stream SoT)

[`pkg/checkpoint/store.go`](pkg/checkpoint/store.go). bucket 2개:

- `pods`: key = pod UID → `PodRecord`
  ```
  PodRecord { podUid, namespace, name, streams[]StreamConfig,
              podIp, podMac, hostVethIfindex,   # NRI가 pod 기동 시 채움 (teardown/재시작 복구용)
              updatedAt }
  StreamConfig { streamId, direction(TX|RX), ifName, vlanId, pcp,
                 dstIp, dstPort, dstMac }
  ```
- `claims`: key = claim UID → `[]podUID` (DRA Unprepare용)

### bpf map 스키마 ([`bpf/tsn_common.h`](bpf/tsn_common.h) — Go 미러 [`pkg/ebpf/maps_types.go`](pkg/ebpf/maps_types.go))

pin 경로 `/sys/fs/bpf/tsn/<map>`. IP/port = network-order 바이트, vlan/ifindex = native uint,
padding 제로화(HASH raw 바이트 비교).

| map | type | key | value |
|---|---|---|---|
| `tx_flows` | HASH | `{src_ip(pod), dst_ip, dst_port, proto, _pad}` (4-tuple, src_port 제외) | `{snat_src_ip, nic_ifindex, vlan_id, pcp, flags, dst_mac[6], src_mac[6]}` |
| `rx_flows` | HASH | `{orig_dst_ip(노드), dst_port, _pad}` | `{pod_ip, pod_port, pod_mac[6], veth_ifindex}` |
| `xdp_devmap` | DEVMAP | `veth_ifindex` | 동일 veth ifindex (redirect 메커니즘) |
| `stream_stats` (P4) | PERCPU_HASH | streamId/flow | `{packets, bytes, last_seen_ns}` |

상수: `TX_FLAG_SNAT=0x1`(value.flags bit0), `TSN_TX_MARK=0xDA75`(egress 가드 표식),
TCX 리턴 `TCX_NEXT=-1`(다음 프로그램) / `TCX_PASS=0`(체인 종료=우회).

---

## 참고한 레퍼런스 구현

| 패턴 | 출처 | 차용 위치 |
|---|---|---|
| 스캐폴드 베이스 (kubeletplugin, DRA helper, PrepareResourceClaims) | `dra-example-driver` (K8s SIG) | `cmd/kubeletplugin`, `pkg/dra` |
| `kubeletplugin.Start`/`Helper.PublishResources`/`DriverResources` | `dra-example-driver` `cmd/.../driver.go`, `state.go` | `pkg/resourceslice`, `cmd/kubeletplugin` |
| NRI stub hooks (`stub.New`, RunPodSandbox/CreateContainer 패턴) | `dranet` `pkg/driver/nri_hooks.go`, `driver.go` | `pkg/nri/hooks.go` |
| eBPF TCX/XDP attach·detach, `/sys/fs/bpf` pin | `dranet` `pkg/driver/ebpf.go` | `pkg/ebpf/loader.go` |
| NIC 인벤토리 (netlink/sysfs) | `dranet` `pkg/inventory/db.go` | `pkg/inventory/discovery.go` (라벨 기반으로 축약) |
| BoltDB 체크포인트 | `dranet` `pkg/driver/pod_device_config_bolt.go` | `pkg/checkpoint/store.go` |
| TX VLAN 태깅 eBPF (부분) | `../tutorial/eBPF-tutorial/dratsn-ebpf` | `bpf/tx.c` (4-tuple + SNAT 토글 + RX 신규로 갱신) |

> ref repo(`dranet`, `dra-example-driver`)는 **읽기 전용** — 수정하지 않고 패턴만 차용.

---

## 빌드 / 배포 / 테스트

```bash
# 의존: go(GOTOOLCHAIN=auto로 1.25 자동), clang, bpf2go, docker, kubectl
# 클러스터: K8s 1.34.3, Cilium 1.19.2(XDP accel Disabled), containerd 1.7+(NRI), 커널 6.8

make generate      # bpf2go (tx/rx/veth) 생성
make build         # 바이너리 빌드
make vmlinux       # vmlinux.h 재생성 (BTF)

# 전체 build-test 루프 (registry/certs/매니페스트/시나리오/스모크)
cd tests
./loop.sh all                  # build → deploy → scenario → test
./loop.sh redeploy             # 코드 수정 후 빠른 재빌드+롤아웃
./loop.sh test                 # 스모크만 (phase 1,2 here + phase 3 via debug pod)

# 스모크 단독
PHASES="1 2" ./run_all.sh
NODE=alpha-w1 TSN_IFACE=eno6 ./smoke_phase3_dataplane.sh   # 데이터플레인 (host bpftool)
```

환경 변수 (override): `REG`, `TAG`, `TSN_NODE`, `TSN_IFACE`, `RX_ENDPOINT_IP`, `NS` — [`tests/loop.sh`](tests/loop.sh), [`tests/lib.sh`](tests/lib.sh).

검증 환경 기본값: TSN 노드 `alpha-w1`, NIC `eno6`(192.168.10.12), registry `192.168.110.11:30500`.

---

## 상태

Phase 1~3 구현·배포·검증 완료. Phase 4(health/가시성)는 스켈레톤 명세만.

tsn-scheduler 구현·kind 검증 완료(2026-08-11, ADR-0038): second scheduler
(`scheduler/` 별도 Go 모듈) + `TSNSlackScore` 플러그인. slack = 802.1Qcc
max−accumulated latency(ns, TSNStream spec 신규 필드 — CNC→controller 관통,
ResourceSlice 미발행). 사전식 = PodTopologySpread(100) / slack(1) 가중치 격리.
검증 = `tests/smoke_phase5_scheduler.sh` (slack 착지·spread 지배·기본 스케줄러
무영향, kind 3-TSN-노드 4/4 PASS).

시나리오 A failover 실험 수집 완료(2026-08-12, ADR-0037): `experiments/fig_regime/`
RUN=paper 31 PASS / 0 FAIL (10 trial × 3군). dratsn 20/20 TSN 재착지·축출 정확히
2.0/trial·복구 med 50.1 s / vanilla 0/20 (전건 일반 노드 blind 착지, 선점 미사용) /
spare 20/20 예비 착지·53.7 s, 단 사전 TSN 활용률 60.3% vs dratsn 90.0%. 감지
단계(kube 기본 grace ~44–52 s)가 전 군 지배 — post-detection 복구는 선점 유무와
무관하게 ~4 s 동급이라 군 간 복구 시간 우열 주장은 하지 않음(ADR-0037 §결과).
f15/f16 렌더 완료.

데이터플레인 host-ns 격리 재설계 적용·검증(alpha-w1):
- TX를 물리 NIC egress(TCX)→**host-side veth ingress(TCX) + L2 rebuild + NIC redirect**로 이동,
  cilium VLAN 필터 drop은 **NIC egress 가드(skb mark)**로 우회, RX에 **VLAN strip** 추가.
- **라이브 검증**: 스케줄링(w1 배치), 맵 프로그래밍, TX(trace HIT/redir + tx_packets egress + cilium drop 소멸),
  RX(`BPF_PROG_TEST_RUN`: XDP_REDIRECT + vlan strip + DNAT). verifier 로드 OK.
- 배포를 **전 노드 DaemonSet**으로 전환(nodeSelector 제거). TSN 노드는 `capable-nics` 라벨로 선언.

동적 reconcile 적용·검증(alpha-w1, ADR-0020):
- **TSNNic 라벨 reconcile**(ADR-0020): `capable-nics` 라벨을 Node watch로 level-triggered 처리.
  라벨 제거→TSNNic GC + NIC detach, 재추가→재생성 + attach 라이브 확인. (이전 stale-GC 한계 해소.)
- **데이터플레인 = bind 시점만**(ADR-0023): bpf map·checkpoint는 pod 기동(NRI RunPodSandbox) 시만
  작성, 재시작 시 checkpoint로 복구(Synchronize). 실행 중 pod는 TSNStream 변경에 **hot-patch 안 함**
  — data-plane frozen ⟺ allocation frozen. 변경 반영은 pod 재생성(= workload controller). 결정성
  TSN·DRA frozen-allocation 철학과 정합. (live-update/ADR-0021은 구현했다가 폐기.)

미검증(환경 제약): on-wire 물리 전달 + RX 라이브 수신은 실제 TSN 스위치(VLAN 미구성)·외부 sender 필요.
알려진 한계: 맵 struct 변경 시 노드별 pin 수동 제거(`loop.sh clearpins`). 라벨 제거 시 해당 NIC을
쓰는 실행 중 RX pod는 detach로 끊김(노드 비-TSN 선언 = drain 전제, ADR-0020).
