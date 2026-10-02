# experiments/ — 재현 가능한 평가 실험

논문 5장(검증·평가)용 실험. figure당 디렉토리 하나, 각각
`run.sh`(수집) → `results/<RUN>/*.csv|pcap`(원시 데이터) → `plot.py`(figure PNG)
파이프라인으로 완결된다. 원시 데이터와 figure가 같은 `results/<RUN>/`에 남으므로
모든 그림은 데이터부터 재생성 가능하다.

| 실험 | 검증 대상 | 필요 환경 |
|---|---|---|
| `fig_placement/` | 배치 정확률 (baseline vs DRATSN) | 실클러스터 **또는 kind** |
| `fig_tagging/` | per-flow selective bypass: TX 태깅 / RX DNAT·redirect (without/with ablation) | **실클러스터 전용** (XDP NIC) |
| `fig_scale/` | 스케줄러 확장성: 10/100/1000 노드에서 배치 정확률·makespan | **KWOK** (docker만 필요) |
| `fig_mixed/` | 경로 소실 후 재배치 정확성 (이종 자원, HA 스트림) — ADR-0039 | **kind 2대** + GPU 드라이버 |

기록으로 보존된 것 (대체됐으나 수집분이 인용된다):

| | 대체한 것 |
|---|---|
| `fig_regime/` | `fig_mixed/` (ADR-0039). `results/paper`, `results/paper2`가 인용됨 |
| `fig_twin/` | `fig_regime/` (ADR-0037) |

## 요구 사항

**공통 (control plane 실험):**
- kubectl + 대상 클러스터 (K8s ≥ 1.34, DRA GA), DRATSN 스택 배포 완료
  (CRDs, deviceclass, mock-cnc, tsn-controller, dratsn DaemonSet)
- python3 venv: `python3 -m venv .venv && .venv/bin/pip install matplotlib numpy`

**kind 경로 (TSN NIC 불필요 — 실습/재현용):**
- docker + kind. `fs.inotify.max_user_instances >= 1024` 권장(멀티노드 join 실패 예방):
  `sudo sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=1048576`
```bash
cd kind
./create-cluster.sh      # 1 control-plane + 6 workers: TSN 3 + 일반 3 (ADR-0037)
./deploy.sh              # 이미지 빌드→로드→배포 (-dataplane=false), 스트림 push
./deploy-gpu.sh          # 두 번째 DRA 드라이버 (fig_mixed 전용, 그 외엔 불필요)
./run-experiments.sh     # fig_placement + plot
kind delete cluster --name dratsn
```
kind 경로는 `-dataplane=false`로 eBPF/NRI를 끄고 control plane만 검증한다.
ResourceSlice·ResourceClaim은 일반 API 오브젝트이므로 스케줄링 결과는
실클러스터와 동일하다 (같은 스케줄러 바이너리).

**fig_tagging (실클러스터 전용):**
- TSN 노드(native XDP NIC)와 주입 노드가 같은 L2 세그먼트에 연결
- 노드에 `tcpdump`, `xdp-tools`(xdpdump); 로컬에 `tshark`, `sshpass`(비밀번호 ssh 시)
- ssh 접근: `export SSH_PASS='<password>'` (키 인증이면 비워둠). 비밀번호는
  커밋되지 않는다 — 환경변수로만 전달.

## 실행

원샷(artifact evaluation 진입점):
```bash
export SSH_PASS='...'                          # 비밀번호 ssh인 경우만
EXPS="placement tagging scale" RUN=paper ./run_all.sh
EXPS="scale" ./run_all.sh                      # 실클러스터 없이 재현 가능한 부분만
```

개별 실행(각 디렉토리 README.md에 상세):
```bash
TRIALS=30 RUN=myrun ./fig_placement/run.sh
FRAMES=200 RUN=myrun ./fig_tagging/run.sh && ./fig_tagging/parse.sh fig_tagging/results/myrun
NODES_LIST="10 100 1000" RUN=myrun ./fig_scale/run.sh
.venv/bin/python fig_<name>/plot.py fig_<name>/results/myrun
```

설정은 전부 env 변수 (`env.sh` 참조): `TSN_NODE`, `TSN_NODE_IP`, `INJECT_NODE_IP`,
`TRIALS`, `FRAMES`, `RUN` 등. `tests/lib.sh`·`scripts/env.sh`를 source하므로
기존 스모크 테스트와 같은 값 체계를 쓴다.

## 실험 설계 요약

**fig_placement** — 동일 Pod를 claim 없이(baseline) / TSN ResourceClaim과 함께
(DRATSN) 각 `TRIALS`회 생성, 착지 노드와 생성→스케줄 시각(클라이언트 측, ~100ms
해상도)을 기록. 기대: baseline은 노드 분산(TSN 경로 무시 = blind), DRATSN은
100% TSN 노드.

**fig_tagging** — OT/IT 트래픽을 **동시에 interleave**로 흘려(짝수=OT, 홀수=IT)
단일 캡처에서 패킷별로 분류. 메인 figure는 TSN metadata proxy 논문
(arXiv:2503.12878 Fig.2, `figures/f11_example.png`) 스타일의 상하 ablation:
- **메인(f11) = taprio 사이클 산점도** (`run_taprio.sh`): tx.c가 map hit 시
  `skb->priority`도 세팅(ADR-0008) → w1 eno1의 software taprio 게이트에 걸림.
  x=사이클 내 방출 오프셋, y=사이클 #. (a) claimless pod는 전부 BE 윈도우,
  (b) claimed pod는 OT만 TSN 윈도우 — 참고 논문 Fig.2와 동일 문법
- 보조: TX 분류(fig_tagging.png — x=캡처 시각, y=관측 PCP lane)
- 보조 figure(`fig_tagging_rx.png`): RX 주입 프레임의 운명 — OT(등록 talker):
  XDP entry→pod socket(DNAT), host stack 0 / IT(미등록): XDP_PASS→host stack, pod 0
- 측정점: w1 eno1 tcpdump(`-Q in/out`), w1 xdpdump, pod 내부 tcpdump.
  `parse.sh`가 summary.csv(집계) + packets.csv(패킷별) 생성.
  캡처 필터는 반드시 plain 절을 `vlan` 절보다 앞에("`udp ... or (vlan and udp ...)`") —
  커널이 VLAN을 skb 메타데이터로 strip한 프레임은 `vlan` 선행 필터에 걸리지 않는다.

**fig_scale** — kwokctl이 실제 kube-apiserver+kube-scheduler에 가짜 노드
10/100/1000개를 붙인다(kubelet 없음 — 대규모 스케줄링 실험의 표준 도구).
DaemonSet은 가짜 노드에서 못 돌므로 ResourceSlice를 합성 주입 — 측정 범위는
**스케줄러의 DRA 할당 정확성·지연**이고, controller/publisher 파이프라인은
kind 경로와 실클러스터가 담당. 각 규모에서: TSN 노드 5개×device 2 = 용량 10,
claim Pod 10개 배치 정확률 + batch makespan(baseline 대비) + capacity+1 Pending.
실행: `NODES_LIST="10 100 1000" ./fig_scale/run.sh` (kwokctl 필요:
https://kwok.sigs.k8s.io — docker만 있으면 됨).

## 주의

- run.sh는 실험 전 `mock-vplc`를 삭제/재배포할 수 있다 (4 device 점유 해제).
- mock-cnc는 in-memory — 각 run.sh가 시작 시 `ensure_streams`로 canonical
  스트림 4개를 검증·복원한다.
- 논문 figure의 원본 실행은 각 `results/paper/` (placement TRIALS=30,
  tagging FRAMES=200/클래스, scale 10/100/1000). taprio 시나리오만
  `fig_tagging/results/paper-taprio/`로 분리(qdisc 설정이 필요한 별도 실행).
  `.pcap`은 용량 때문에 커밋 제외(`.gitignore`) — CSV·figure로 재현 가능.
