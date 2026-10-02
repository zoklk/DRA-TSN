# fig_mixed — 이종 자원 제약 아래의 경로 소실 재배치 (ADR-0039)

`fig_regime`을 대체한다. `fig_regime/`은 paper·paper2 수집분과 함께 기록으로 보존한다.

이 실험이 답하는 질문은 하나다.

> **경로가 이중화되어 있을 때, 살아남은 경로를 누가 찾는가.**

두 arm 모두 HA를 제대로 갖춘 클러스터를 쓴다. 워크로드의 TSN 스트림이 M대 중 k대에
미리 배포돼 있고, 그중 1대가 죽는다.

| arm | 배치 | 스트림 | GPU |
|---|---|---|---|
| `vanilla` | 노드 라벨 required nodeAffinity, 기본 스케줄러 | claim 없음 + env 하드코딩 | **DRA claim** |
| `dratsn` | `tsn-scheduler` | **DRA claim** | **DRA claim** |

**변수가 하나로 분리된다.** 같은 클러스터, 같은 파드에서 GPU는 두 arm 모두 자원으로
모델링돼 있고 TSN만 한쪽에서 라벨이다. GPU 배치는 양쪽 다 맞고 TSN만 틀린다.

---

## 라이브 우선 — 이 디렉토리의 산출물은 수집본이 아니라 절차다

씬은 kind 클러스터의 **현재 상태**를 보여준다. 10회를 수집해 평균을 굽고 재생하는
경로가 아니다. `scenario_loop.sh`가 살아있는 클러스터를 같은 동작 시퀀스로 반복
구동하고, `live_serve.py`가 그것을 칠하면서 동시에 기록한다.

**재현되는 것은 절차이지 결과가 아니다.** `vanilla`는 회차마다 다른 노드에 착지하고,
그 분산이 곧 결과다. 평균으로 구우면 유일하게 볼 가치가 있는 것이 사라진다.

```
반복 N회:
  clean + fill            초기 상태를 매번 동일하게 재구성
  T_NORMAL 대기           정상 운영 구간
  POST /inject            vplc-line이 앉은 노드를 죽인다 (두 arm 병렬)
  T_FAULT 대기            복구가 이 안에서 일어난다. 소요는 측정값
  POST /restore           노드 기동 + MAC 핀백 → CNC×TSNNic 조인 자가 재형성
  run.sh streams          되살아난 노드의 스트림 재배포
```

---

## 토폴로지와 스트림 레이아웃

```
w1  TSN                 cell-A            ┐ 생존. 대칭으로 채운다
w2  TSN + GPU           cell-A, cell-B    ← 죽는 노드 (vplc-line 거주)
w3  TSN + GPU           cell-B            ┘ 생존. ai-inference 거주
w4  GPU     w5  GPU     w6  일반           흡수층
```

| 워크로드 | 자원 | 성격 | 구성 |
|---|---|---|---|
| `vplc-line` | TSN (`cell-A`) | guaranteed | **파드 1개**, 2000m |
| `ai-inference` | TSN + GPU (`cell-B`) | guaranteed | **파드 1개**, 2000m + GPU 1 |
| `ai-training` | GPU | elastic | 노드당 1~2개, 1500m + GPU 1 |
| `logging` | 없음 | elastic | 채움 산술이 정한 개수, 1000m + trim 1개 |

guaranteed는 파드 1개여야 판정이 모호해지지 않고, elastic은 여러 개여야 "몇 개가
밀렸나"가 측정되고 노드를 90%까지 정확히 채울 수 있다.

`deploy.sh`가 깔아두는 기본 스트림은 전 TSN 노드가 `cell-A`(사실상 k=3)라 이 실험에
맞지 않는다. `run.sh streams`가 그것을 **지우고** 위 레이아웃을 깐다. 노드별 기대
device 수가 다르므로 `>=`가 아니라 **정확히 일치**로 검사한다 — 안 그러면 기본값이
남아 k=3이 조용히 복원된다.

---

## 설계에서 반드시 지켜야 하는 두 가지

둘 다 어겨봤고, 둘 다 **조용히** 실험을 무효화했다.

**1. 생존 노드는 교환 가능해야 한다.** `vanilla`가 라벨로 구분하지 못한다면 클러스터도
다른 무엇으로도 구분되면 안 된다. w1과 w3은 같은 목표·같은 단위로 채워 여유 CPU와
victim 구성을 맞춘다. `fill`이 여유가 어긋나면 경고한다.

**2. `ai-inference`는 이 장애로 밀리면 안 된다.** 죽는 노드에 두었더니 생존
TSN+GPU 노드로 강제되면서 `vplc-line`의 선택지 하나를 깎았고, `vanilla`가
**10/10 정답**으로 축퇴해 arm 자체가 소멸했다. 그래서 처음부터 w3에 앉힌다.

그리고 **선점이 필요해야 한다.** 예비를 넉넉히 두면 밀 것이 없어 elastic×guaranteed
축이 아무 일도 하지 않는다(실측: 예비를 두었을 때 축출 0건, 없앴을 때 매 회차
`logging` 축출).

---

## 판정

이진 하나: **재배치된 노드가 그 워크로드의 살아있는 TSN 스트림을 보유하는가.**

kind는 `-dataplane=false`라 eBPF도 태깅된 프레임도 없다. 판정은 제어평면 사실로 한다
(`TSNStream` CR의 `nodeName` × `domainId` × `direction=TX`). 파드가 TSN 라벨 노드에서
**Running인데 경로가 없는** 상태가 `vanilla`가 만드는 실패이고, 그것을 보는 유일한
방법이 이것이다.

`vanilla` 파드의 `TSN_STREAMS` env는 **표시용 원장**이다. 대역폭 주석과 같은
선언/측정 경계.

---

## 실행

### 사전 조건

```bash
../kind/create-cluster.sh                    # 7노드 (cp + TSN 3 + 일반 3)
../kind/deploy.sh                            # DRATSN 스택 (-dataplane=false)
../kind/deploy-gpu.sh                        # 두 번째 DRA 드라이버 (gpu.example.com)
KIND_CLUSTER=vanilla ../kind/create-cluster.sh   # 두 번째 arm용 클러스터
KIND_CLUSTER=vanilla ../kind/deploy.sh
KIND_CLUSTER=vanilla ../kind/deploy-gpu.sh
```

> **타이밍을 수집하려면 클러스터를 먼저 재생성하라.** kind 노드의 DRA claim 준비는
> 사용과 함께 조용히 열화한다. 같은 클러스터·같은 배치에서 `sched→ready`가 1.1초였던
> 것이 세션 후반에는 240초 안에 Running에 도달하지 못했다. 에러도 경고도 없다.
> 라이브 구성에서는 arm이 곧 클러스터이므로, 두 클러스터를 **같은 시점에 같은 절차로**
> 만들어야 클러스터 이력 차이가 arm 차이로 위장하지 않는다.

### 라이브 (주 경로)

```bash
python3 live_serve.py \
  --cluster dratsn=$HOME/.kube/kind-dratsn.kubeconfig \
  --cluster vanilla=$HOME/.kube/kind-vanilla.kubeconfig \
  --state-dir results/live --timeline auto --domains "vplc-line=cell-A" &

CYCLES=5 T_NORMAL=60 T_FAULT=60 RUN=live ./scenario_loop.sh
../.venv/bin/python plot.py results/live
```

Isaac Sim의 `dratsn.live` 확장이 같은 서버의 `/state`를 폴링해 씬을 칠한다.

> `RUN`은 매번 새 이름을 쓴다. `timeline-<arm>.csv`와 `cycles.csv`는 **append**라
> 기존 RUN을 재사용하면 두 수집이 이어붙고 `plot.py`가 섞어 그린다.

### 배치 (논문 수치용 부산물)

```bash
MIXED_CYCLES=10 RUN=batch ./run.sh            # 두 arm, verdict 포함
ARMS=dratsn MIXED_CYCLES=1 RUN=gate ./run.sh  # 게이트 1회
```

단계 실행: `setup` / `streams` / `fill <arm>` / `inject-fault` / `record <arm> <cycle>` / `clean`

### 주요 환경변수

`MIXED_CYCLES` `ARMS` `RUN` `UTIL_TSN` `UTIL_GEN` `VPLC_MCPU` `INFER_MCPU`
`TRAIN_MCPU` `LOG_MCPU` `DOMAIN_VPLC` `DOMAIN_INFER` `GPU_DRIVER` `VPLC_TOL_S`
루프: `CYCLES` `T_NORMAL` `T_FAULT` `SERVER` `LOOP_ARMS`

---

## 산출물

| 파일 | 내용 |
|---|---|
| `results/<RUN>/timeline-<arm>.csv` | 상태 전이 (`t_ms,arm,workload,state,node,stream_ok`). **그림의 유일한 입력** |
| `results/<RUN>/cycles.csv` | 회차 창 (`t_ready_ms,t_inject_ms,t_restore_ms`) |
| `results/<RUN>/mixed.csv` | 배치 경로의 단계 분해 + 판정 (라이브 경로는 안 씀) |
| `results/<RUN>/{evictions,util}.csv` | 배치 경로 전용 — 축출 대상과 pre/post 사용률 |
| `results/<RUN>/f_mixed_production.png` | 분당 정상품 파형 |

씬을 칠하는 프로세스가 timeline을 쓰므로 **그림과 애니메이션이 어긋날 수 없다.**

### 단계 시각은 폴 시각이 아니라 서버 타임스탬프다

폴은 **사건이 일어났음을 알아채는** 용도로 돌지만, 기록하는 값은 API 객체가 들고 있는
시각이다.

| 단계 | 출처 |
|---|---|
| detect | Node `.status.conditions[Ready].lastTransitionTime` |
| evict | Pod `.metadata.deletionTimestamp` |
| sched | Pod `.status.conditions[PodScheduled]` |
| ready | Pod `.status.conditions[Ready]` — **`phase == Running`이 아니다** |
| t0 | 서버 시각 없음. `docker stop`은 클러스터 밖이다 |

이걸 안 하면 조용히 틀린다. 0.25초 폴 루프는 네임스페이스에 파드가 60개쯤 되면
따라가지 못하고(매 반복이 `kubectl get pods`), `sched→ready`를 4~6초로 부풀렸다 —
파드 자신의 condition은 1~2초였다. Ready를 `phase == Running`으로 트리거하는 것도
같은 부류의 오차다: 그 시점 `Ready` condition은 아직 False라 엉뚱한 전이 시각을 읽는다.

`t0`만 로컬인 것이 문제가 안 되는 이유는 **kind가 단일 호스트**이기 때문이다 — 노드
컨테이너가 호스트 시계를 공유하므로 apiserver 시각이 곧 t0 시각이다. 실 다중 노드
클러스터에서는 성립하지 않는다. 해상도는 1초다(`metav1.Time`은 RFC3339 초 단위).

검증은 `raw/pods-*.json` 덤프와 대조하면 된다. 그 덤프에는 정착된 condition이 들어
있으므로 CSV 값과 일치해야 한다.

### y축은 측정이 아니라 합성이다

```
측정   착지 노드 / 스트림 보유 여부 / 전이 시각
  ×
선언   지연 분포(ADR-0036 lognormal), run threshold, 조립 시간
  ↓
분당 정상품
```

`sim_model.py`를 임포트해 쓴다 — 다시 구현하지 않는다. 씬의 지연 게이지와 같은 모델이다.

```
조립 4s/부품 → 15 부품/분,  제어 주기 2ms → 부품당 2000 제어주기
무결 경로    15.0/분   (GCL이 지연을 예약창 안으로 묶는다)
최선형 경로   6.8/분   (median 0.7ms lognormal → 마감 초과 14.7% → 4연속 초과 54.8%)
정지          0
```

λ=0.7ms는 **전이 대역**에서 고른 값이다. λ=0.3이면 15.0(무결과 구분 불가), λ=1.0이면
0.1(정지와 구분 불가). 대역이 좁은 것이 ADR-0036이 이 값을 단정하지 않고 스윕하는 이유다.

---

## 결과

**`results/paper2`** — 라이브 경로, 재생성 직후 kind 클러스터 2대, 5 cycle (2026-08-31)

```
착지  dratsn  5/5 worker1 stream_ok=1
      vanilla 2/5 worker1 stream_ok=1   ← 우연히 맞은 회차
              3/5 worker3 stream_ok=0   ← Running인데 경로 없음
```

파형에서 `vanilla`는 회차마다 위/아래를 오간다. **그 분산이 결과다.**

**`results/paper2-batch`** — 같은 설정의 배치 경로 1 cycle. 라이브 경로는
`timeline`/`cycles`만 쓰고 `mixed.csv`·`evictions.csv`·`util.csv`를 채우지 않으므로,
축출과 사용률은 이쪽이 근거다.

```
감지  ~8s (kube 기본값 43.7s에서 §10 튜닝)
축출  logging만, guaranteed 0
사용률 TSN 90.0%
```

**arm 간 복구 시간은 주장하지 않는다.** 감지가 arm 공통 상수이고, 라이브 구성에서는
arm과 클러스터가 분리되지 않는다. 그리고 서버 타임스탬프로 재면 `sched→ready`가
dratsn 1.1~4.9s / vanilla 1.0~2.0s로 **분포가 겹친다** — claim 보유 여부는 분리된
차이를 만들지 않는다(`fig_regime` paper2도 같았다: dratsn 1.8s vs vanilla 1.5s).

---

## 한계

- kind이므로 GPU는 가짜 장치이고 자원량은 CPU 요청 산술이다. 절대 수치를 산업 사례와
  직접 비교하지 않는다.
- 사망 노드의 elastic 부하는 생존 노드가 이미 90%라 갈 곳이 없어 Pending으로 남는다.
  **판정이 아니라 비용으로 보고한다** — 빠듯한 체제의 성질이지 결함이 아니다.
- 정적 arm의 (k-1)/(M-1)은 **이상적 상한**이다. 실측이 낮은 것은 선점 후보 구성의
  비대칭 때문이고, 그 편향은 정적 arm에 불리한 방향이라 이 수치가 과장일 수 없다.
- 감지 상수는 kind 고유값이다. 실 클러스터에서 같은 값이 안전한지는 검증하지 않았다.

## 알려진 gotcha

- **DRA prepare 웨지** — 노드에 바인딩됐는데 샌드박스 요청도 이벤트도 없이
  `ContainerCreating`에 고정된다. `fill`이 감지해 해당 노드 kubelet을 재시작하고
  재시도한다. **`Pending`과 구분해야 한다**: Pending은 장치가 없는 것이고 kubelet
  재시작은 무의미하며 파괴적이다.
- **`device_count`는 드라이버를 안 가린다**(`../lib.sh`). GPU 드라이버도 pool 이름이
  노드명이라 TSN+GPU 노드가 2배로 세어진다. 여기서는 `tsn_device_count`를 쓴다.
- **단계 시각을 폴 시각으로 찍지 마라** (위 §단계 시각). 세 겹으로 조용히 틀렸다:
  클러스터 열화 → 폴 지연 → Ready 트리거. 셋 다 에러 없이 수치만 부풀렸다.
- **`docker stop -t 0`** 이어야 한다. 기본 10초 SIGTERM 유예 동안 노드가 리스를 계속
  갱신해 t0가 실제 사망보다 앞서고 감지가 부풀려진다.
- **주입은 병렬로.** 순차로 하면 두 arm의 장애가 ~10초 어긋나 공통 시간축에서 느린
  arm이 빨라 보인다.
- **`t_inject`는 호출 전에 찍는다.** `/inject`가 `docker stop`을 블로킹한다.
- **kubeconfig를 고정한다.** `kind create cluster`가 공유 current-context를 덮어쓰므로,
  동시에 다른 클러스터를 만들면 진행 중인 스크립트가 엉뚱한 클러스터를 가리킨다.
- 도메인 값은 `cell-A`지만 K8s 오브젝트 이름은 소문자여야 한다(`claim_name`).
