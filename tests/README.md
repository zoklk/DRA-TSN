# DRATSN 스모크 테스트

각 페이즈의 인수 기준(`context/phases/*.md` §검증)에 대응하는 페이즈별 셸
스모크 테스트. **테스트 우선(test-first)**: 페이즈가 구현되기 전까지 해당
검사는 `[FAIL]`이 아니라 `[SKIP]`(CRD/DaemonSet/맵/파드 누락)을 보고한다.
`[FAIL]`은 배포는 됐지만 동작이 잘못됐다는 뜻이다.

## 실행
```bash
cd tests
./run_all.sh                 # 전체 페이즈
PHASES="1 2" ./run_all.sh    # 일부만
./smoke_phase3_dataplane.sh  # 단일 페이즈
```

페이즈 3은 호스트 데이터 플레인(`bpftool`, `tcpdump`)을 검사한다 — 컨트롤
플레인이 아니라 **워커 노드에서**(또는 권한 있는 디버그 파드에서) 실행할 것.

## 배포 모델 (production)

DRA 드라이버 DaemonSet은 **모든 노드**에 뜬다(`nodeSelector` 없음). 노드가 TSN
노드인지는 **노드 라벨** `tsn.dra.net/capable-nics=<iface,...>`(또는 `TSN_CAPABLE_NICS`
env)로 선언한다. 라벨 있는 노드만 해당 NIC을 발견→`TSNNic` CR + 자기 노드 스트림을
ResourceSlice(pool=노드명)로 광고하고, 없는 노드는 빈 ResourceSlice(liveness)만 낸다.
스케줄러는 매칭 device가 있는 노드에만 vplc 파드를 배치한다.

> 정상/비정상 배치 비교: 단 하나의 노드/NIC만 라벨링하면, 그 device가 그 노드
> ResourceSlice에만 광고되어 vplc는 거기에만 배치된다(다른 노드 강제 시 Pending).
> `loop.sh deploy`가 `TSN_NODE`(기본 alpha-w1) 하나만 라벨링한다.

> 라벨을 제거해도 실행 중 파드는 시작 시 읽은 NIC 목록을 유지하고 stale `TSNNic` CR도
> 자동 GC되지 않는다(현 한계) — 파드 재시작 + `kubectl delete tsnnic <name>` 수동 정리.

## 데이터 플레인 E2E (`dataplane_e2e.sh`)

맵 프로그래밍 존재가 아니라 **실제 패킷 변환**을 검증. `vplc` 노드에서 root로:
```bash
sudo NS=dratsn-test TSN_IFACE=eno1 RX_ENDPOINT_IP=192.168.1.1 ./dataplane_e2e.sh all
```
- `tx` — 파드가 UDP 송신 → TSN NIC `tcpdump`로 VLAN/PCP 태그 + L2(src=NIC, dst=listener) + src IP(SNAT on/off) 확인.
- `rx` — `BPF_PROG_TEST_RUN`으로 VLAN strip + DNAT(→pod) 결정적 검증(스위치 불요).
- `inject-rx` / `capture-tx` — **peer 노드**(같은 TSN 세그먼트)에서 실 wire RX 주입 / TX 캡처 (`PEER_IFACE`, `TSN_NIC_MAC`).

사람이 직접 tcpdump로 위치별 VLAN/SNAT/DNAT/strip을 눈으로 확인하는 절차는
[`../docs/e2e_run_book.md`](../docs/e2e_run_book.md) 참고.

## 설정 (환경변수 오버라이드, `lib.sh` 참고)
| 변수 | 기본값 | 의미 |
|---|---|---|
| `NS` | `dratsn-test` | 테스트 워크로드용 네임스페이스 |
| `DRIVER` / `APIGROUP` | `tsn.dra.net` | DRA 드라이버 / CRD 그룹 |
| `DS_NAME` / `DS_NS` | `dratsn` / `kube-system` | DRA 드라이버 DaemonSet |
| `CTRL_NAME` | `tsn-controller` | 컨트롤러 Deployment |
| `MOCK_CNC_NAME` | `mock-cnc` | 목(mock) CNC 디플로이/서비스 |
| `TSN_IFACE` | `eno1` | 워커의 TSN 지원 NIC |
| `RX_ENDPOINT_IP` / `RX_ENDPOINT_PORT` | _미설정_ / `4840` | RX 수신 지점 |

## 페이즈별 커버리지
- `smoke_phase1_controller.sh` — CRD, 컨트롤러, 목 CNC, 스트림→`TSNStream` CR
- `smoke_phase2_dra.sh` — DaemonSet, `TSNNic`, DeviceClass, ResourceSlice, 배치(placement)
- `smoke_phase3_dataplane.sh` — bpf 맵/어태치(NIC RX XDP + host-veth TX TCX `$PIN/tcx`), `TSN_STREAMS` env, tx_flows VLAN 태깅
- `smoke_phase4_health.sh` — `stream_stats`, health 속성 (스켈레톤)
- `dataplane_e2e.sh` — 실제 패킷 변환 E2E (TX 라이브 / RX prog_test_run / 크로스노드)

`loop.sh` 보조 타깃: `clearpins`(맵 struct 변경 후 노드별 pin 제거), `e2e`(TX+RX claim 파드 배치).

`TODO:` 줄은 라이브 트리거(퍼블리셔, 가짜 송신기, 맵 토글)가 필요한 단계를
표시한다 — 각 페이즈가 안착할 때마다 연결할 것.
