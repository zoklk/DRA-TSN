# fig_regime — 빠듯한 체제 failover: 활용률×가용성 3-비교군 (f15, f16)

> **대체됨 — `fig_mixed/`가 이 실험을 잇는다 (ADR-0039, 2026-08-30).**
> 기록으로 보존한다: `results/paper`·`results/paper2`가 논문에 인용되고
> 아래 절차로 재현된다. 새 수집은 `fig_mixed/`에서 한다.

## 목적

시나리오 A "예비 없는 밤의 failover"(ADR-0035)의 본 실험. TSN 노드가 request 기준
~90% 찬 클러스터에서 TSN 노드 1대를 죽이고(docker stop), 세 전략이 활용률×가용성
평면의 어느 코너에 앉는지 잰다:

| 비교군 | vPLC claim | 스케줄러 | TSN 예비 | 기대 |
|---|---|---|---|---|
| `spare` | O | tsn-scheduler | 1대 비움 | 무선점 복구, 활용률 손해 |
| `vanilla` | X | default | 없음 | 복구는 되나 blind 착지(비-TSN) — PriorityClass만으로 부족 |
| `dratsn` | O | tsn-scheduler | 없음 | DRA Filter가 선점을 생존 TSN 노드로 유도, it-ai만 최소 축출 |

vanilla에도 PriorityClass는 준다 — 변수는 스케줄러의 TSN 인지 하나뿐이다.
이 드라이버가 검증하는 유일 미검증 경로(ADR-0035 §결과): **claim 보유 pod가
선점자일 때 PostFilter 선점 시뮬레이션의 dynamicresources Filter 통과.**

## 전제

- kind 6워커 형상(cp1 + TSN3 + 일반3, ADR-0037): `../kind/create-cluster.sh` →
  `../kind/deploy.sh`. kind-* 컨텍스트가 아니면 실행을 거부한다(docker stop 때문).
- `../.venv` (run_all.sh와 동일: matplotlib numpy — plot만 필요, 드라이버는 stdlib).
- docker CLI (kind 노드 정지/재기동).

## 실행

```bash
REGIME_TRIALS=10 RUN=paper ./run.sh          # 원샷: 3 비교군 × 10 trial (~2h)
ARMS=dratsn REGIME_TRIALS=1 ./run.sh         # 게이트: 선점×DRA 경로만 빠르게
../.venv/bin/python plot.py results/paper
```

단계 손호출(데모/시연 — figure 경로와 같은 코드, ADR-0037):

```bash
RUN=demo ./run.sh setup
RUN=demo ./run.sh fill dratsn        # 체제 구성 완료 상태에서 멈춤
RUN=demo ./run.sh inject-fault       # vPLC 2대 노드 docker stop
RUN=demo ./run.sh record dratsn 1    # 관찰·기록·노드 복원
RUN=demo ./run.sh clean
```

노브: `UTIL_TSN=90 UTIL_GEN=90 VPLC_MCPU=2000 AI_MCPU=2000 HIST_MCPU=1000
VPLC_TOL_S=2 WATCH_TIMEOUT=240`.

## 동작 요약

- fill 순서 = historian(일반) → vPLC → AI(TSN): 일반 노드를 먼저 채워 blind
  스케줄러도 초기엔 vPLC를 TSN에 앉힌다 — 3군의 pre-fault 출발점이 같고, 차이는
  failover 재배치에만 생긴다.
- 90%는 request 산수(ADR-0037): 단위 filler + 트리머 pod가 노드 잔여를 정확히
  (100−UTIL)%로 고정한다. 트리머 없이는 내림 잔량 때문에 vPLC가 무선점 진입한다
  (gate 런에서 실측된 버그).
- 일반 노드는 축출 AI 수용분(2×AI_MCPU)을 뺀 값까지만 채운다 — vanilla의 blind
  착지가 "선점 없이 일반 노드에 들어가기"로 나타나는 이유이기도 하다.
- 장애 주입 = `docker stop`(진짜 노드 다운): 감지(~40s, kube 기본
  node-monitor-grace) → taint 축출(vPLC tolerationSeconds=2) → 선점 포함 스케줄
  → 기동 전 구간이 측정에 들어온다. 복구는 detect/evict/sched/ready 단계로 분해
  (watch_fault.py, 0.25s 폴 — wait_for의 1s 폴은 단계분해에 부족).
- 노드 재기동은 eth0 MAC을 재생성해 CNC 스트림 조인을 끊는다 → 매 trial 후
  `repair_streams`가 CNC 사본을 새 MAC으로 교체(mock-cnc admin upsert). mock-cnc/
  tsn-controller/tsn-scheduler는 setup이 control-plane에 고정한다(장애 대상
  노드와 함께 죽으면 in-memory CNC 스토어가 증발).

## 출력

`results/<RUN>/`:
- `regime.csv` — vPLC별 복구: `arm,trial,vplc,node0,on_tsn0,node1,on_tsn1,detect_ms,evict_ms,sched_ms,ready_ms,resched_ms,pending` (ms는 docker stop 기준)
- `evictions.csv` — 선점 희생자: `arm,trial,pod,priority_class,node,correct,ai_resched_ms` (correct = it-ai 여부)
- `util.csv` — 풀별 활용률: `arm,trial,phase,pool,requests_mcpu,allocatable_mcpu,pct` (post는 생존 노드 기준)
- `raw/` — 이벤트/pod 스냅샷 (로컬 시계 교차검증용)
- `topology.env` — 측정 당시 풀 형상
- `f15_recovery.png` — 복구 단계분해 + 착지 분포
- `f16_plane.png` — 활용률×가용성 평면 세 코너 (주장 그림)
- `scenario_feed.json` — Isaac twin feed (`build_feed.py results/<RUN>`으로 생성)

## Isaac feed (build_feed.py — feed v2 스키마 SSOT)

`regime.csv`(+`evictions.csv`, `topology.env`) → `scenario_feed.json`. 트윈 3트랙
(physical=선언 / vanilla / dratsn)만 — **spare는 f16 전용 비교군이라 트랙이 아님**.
대표 trial 선정 = modal 착지 그룹 내 resched 중앙값(선정 로직은 fig_twin과 동일).

스키마 v2 = v1(sim_model.load_feed가 읽는 t_ms/down_ms/resume/label) +
disruption별 `stages`(detect/evict/sched/ready 누적 ms) + `evictions`(선점 도미노) +
top-level `cluster`(nodes/util_pct/fault_node — 오버레이 레이어용). **baked와 live
watcher가 같은 스키마를 공유한다(ADR-0037)** — 필드 정의 전문은 build_feed.py
docstring이 SSOT.

## 검증 기준 (run.sh 말미 verdict)

- dratsn: 전 trial `on_tsn1=1`, 축출 전원 `it-ai`, trial당 1–2개(선점 발생 + 최소
  집합 — 0개면 체제가 헐거운 것으로 FAIL).
- vanilla: 다수 trial `on_tsn1=0` (blind 착지 가시화).
- spare: 축출 0, `on_tsn1=1`, TSN 풀 pre 활용률 < dratsn.

## 한계

- 복구 수치는 kind 고유 타이밍(감지 기본값, 이미지 캐시) 포함 — kind 수치로만
  표기한다(ADR-0037, fig_placement의 "논문 수치=실클러스터" 분리 원칙과 대응).
- 축출은 1차 단층(AI→일반 빈자리)만. AI가 historian을 미는 연쇄 선점은 옵션으로
  남긴다(ADR-0037).
- Isaac feed는 `build_feed.py`로 연동됨(위 §Isaac feed). 씬 오버레이(v11)와
  live 모드가 cluster/stages/evictions 필드의 소비자.

## live 모드 (2026-08-13 구현)

`live_serve.py` — watch_fault.py의 폴/분류 로직을 증분 상태머신으로 돌리는
watcher + HTTP 서버(stdlib only). 전체-상태 스냅샷을 `GET /state`로 서빙하고
(필드명 = feed v2), Isaac Sim의 `isaac-sim/exts/dratsn.live` 확장이 pull해서
v11 씬의 ClusterOverlay 프림을 세션 레이어로 재기록한다(ADR-0031 live 모드,
자세한 사용법은 확장의 README).

```bash
RUN=demo ./run.sh setup && RUN=demo ./run.sh fill dratsn
python3 live_serve.py --arm dratsn --state results/demo/state.env &
RUN=demo ./run.sh inject-fault      # state.env 갱신이 스테이지 머신을 arm
RUN=demo ./run.sh record dratsn 1   # 복구(watch/CSV 겸용) — 데모만이면
                                    # docker start <node0> 후 다음 setup이 수리
```

- `--arm dratsn|vanilla`가 존을 정한다(spare는 존 없음 → 거부). 암 전환 =
  서버 재시작.
- inject-fault의 `state.env`(NODE0/T0)가 baked record와 live가 공유하는 handoff
  (ADR-0037: 단계 손호출 = 데모 경로). mtime 변경 = 새 주입으로 간주하고 스테이지
  머신을 리셋한다.
- 이벤트(fault/detect/evict/preempt/sched/ready/victim-resched)는 stdout과
  `/state`의 events 링버퍼(50)에 동시에 남는다 — 데모 나레이션 콘솔로 쓸 것.
- **인터랙티브 주입** (2026-08-14): `POST /inject {node?, force?}` = docker stop
  + state.env 기록(동일 handoff — run.sh와 한 코드 경로), 기본 노드는 vPLC가
  앉은 곳. `POST /restore {node?}` = docker start(스트림 수리는 다음 setup).
  가드: 진행 중 fault 409, 죽은 노드 409, TSN 풀 고갈 경고는 notes로.
- **통합 서버 (2026-08-14 개편)**: 프로세스 하나가 클러스터 poller N개(암당
  kubeconfig 고정, `--cluster arm=kubeconfig` 반복) + 물리 PLC 상태머신
  (`POST /plc/fail`/`/plc/repair`, `--plc-repair-s` 선언 수리시간)을 보유,
  `GET /state`는 v3 통합 스키마(arms+plc). `/restore`는 docker start 후 노드
  MAC을 건강 기준선으로 핀백 — CNC×TSNNic 조인이 자가 재형성되어 TX 부채가
  안 쌓인다. 이중 클러스터는 `KIND_CLUSTER=vanilla`로 생성(kind/ 스크립트
  파라미터화). 상세 절차·바닥 라이브는 `isaac-sim/exts/dratsn.live/README.md`.
