# dratsn.live — 라이브 클러스터 오버레이 (Kit 확장)

kind 클러스터에서 fig_regime 시나리오 A(failover)를 실제로 실행하면, 이미 열려
있는 v11 씬의 `/World/ClusterOverlay` 프림이 실시간으로 따라 움직인다.
ADR-0031의 live 모드: baked replay와 같은 feed v2 이벤트 스키마(ADR-0037),
같은 프림 명명 계약(ADR-0033 §6)을 쓰되, 데이터가 굽는 시점이 아니라 지금
클러스터에서 온다.

```
kind×N ──(kubectl 폴 0.25s)── live_serve.py(통합: 클러스터 poller N + PLC 상태머신)
                                   │ HTTP /state (v3: arms + plc)
                                   ▼
                            dratsn.live 확장 ──(세션 레이어)── v11 씬
                              ├─ overlay: 코어/배치/레인/게이지/안무
                              └─ floor: 3셀 컨베이어·제품·와이어를 직접 구동
```

- **live_serve.py** (`experiments/fig_mixed/`): 통합 서버 — 암당
  kubeconfig 고정 poller + 선언된 물리 PLC 상태머신. stdlib만 사용.

  ```bash
  python3 live_serve.py \
      --cluster dratsn=$HOME/.kube/kind-dratsn.kubeconfig \
      --cluster vanilla=$HOME/.kube/kind-vanilla.kubeconfig \
      --state-dir results/demo --plc-repair-s 90
  # POST /inject  {"cluster":"dratsn","node":"..."}   # node 생략 = vPLC 노드
  # POST /restore {"cluster":"dratsn"}                # MAC 핀백 포함
  # POST /plc/fail | /plc/repair                      # 선언된 수리시간 카운트다운
  ```
- **라이브 바닥 (하이브리드)**: 리듬(플라이트 바·로봇 스윙)은 전용 베이크
  `*_v11_live_base.usda`(2880프레임 무이음새 루프, `build_factory_scene.py
  --live-base`)를 **타임라인이 재생** — Hydra 네이티브 보간이라 매끄러움.
  스토리(제품 파이프라인·Good/Bad 판정·와이어/pod 마커·PLC 캐비닛)는
  확장(`floor_map.py`)이 타임라인 프레임을 마스터 클록으로 세션 default
  구동. vPLC가 Running이 아니면: 리듬 속성은 현재 프레임 값으로
  freeze(고정), 파이프라인은 일시정지, 와이어 탈색 — 복구 시 clear로
  타임라인이 되찾고 다음 사이클 경계부터 재개. 제품 판정은 게이지와
  **같은 선언 모델**(ADR-0036: P0 무조건 양품, P1/P2 lognormal draw) —
  lane → 지연 → 불량률 인과가 화면에서 닫힌다. 라이브 씬 루트는
  `factory_three_cells_v11_live.usda`(기존 overlay를 이름으로 재사용,
  replay 3파일 무수정); 루트 파일명에 `_live`가 없으면 확장은 자동으로
  물러난다(baked replay 관람 모드).
- **dratsn.live** (이 확장): 백그라운드 스레드가 URL을 ~2 Hz로 pull, Kit 메인
  스레드의 update 콜백이 스냅샷 diff를 **세션 레이어 default**로 저작.
  세션 default가 baked 타임샘플을 마스킹하므로(레이어 강도) replay 애니메이션
  위에 라이브 상태가 덮인다. 디스크의 usda 파일은 절대 쓰지 않는다 —
  확장을 끄면(또는 서버가 없으면) 씬은 정확히 baked 파일 그대로다.

## 배치 레이아웃

전송이 HTTP라 위치 무관 — URL만 바꾼다.

**single-host** (GitHub 재현 표준: docker + kind + kubectl + Isaac Sim이 한 GPU 머신):

```bash
cd experiments/kind && ./create-cluster.sh && ./deploy.sh
cd ../fig_regime
RUN=demo ./run.sh setup && RUN=demo ./run.sh fill dratsn
python3 live_serve.py --arm dratsn --state results/demo/state.env &
# Isaac Sim 실행 인자에 (워크스테이션이면 isaac-sim.sh 뒤에 동일하게):
#   --ext-folder <repo>/isaac-sim/exts --enable dratsn.live \
#   --/app/extensions/registryEnabled=0 \
#   --/exts/dratsn.live/url=http://127.0.0.1:8787/state \
#   --/exts/dratsn.live/scripts_dir=<repo>/isaac-sim \
#   --/exts/dratsn.live/auto_stage=<scene>/factory_three_cells_v11.usda
RUN=demo ./run.sh inject-fault      # 이후 화면에서 detect→evict→sched→ready
RUN=demo ./run.sh record dratsn 1   # 노드 복구 + CNC 스트림 수리
```

**cross-host** (랩 구성: kind=dev 호스트, Isaac=gpu-spark2): url만
`http://<dev-host-ip>:8787/state`. 컨테이너 재생성 커맨드는
`isaac-sim/gpu-spark2_Isaac-Sim_세팅.md`의 live 절 참고.

## 이중 클러스터 동시 시연 + 인터랙티브 주입

클러스터 2개(dratsn + vanilla)를 같은 호스트에 띄우면 두 존이 **동시에**
움직인다 — 같은 순간 같은 fault를 넣고 DRATSN 존은 TSN 재착지, VanillaK8s
존은 blind 착지가 한 화면에서 갈라지는 것이 시연의 핵심.

```bash
cd experiments/kind
./create-cluster.sh && ./deploy.sh                       # dratsn 클러스터
KIND_CLUSTER=vanilla ./create-cluster.sh && KIND_CLUSTER=vanilla ./deploy.sh
kind export kubeconfig --name dratsn  --kubeconfig ~/.kube/kind-dratsn.kubeconfig
kind export kubeconfig --name vanilla --kubeconfig ~/.kube/kind-vanilla.kubeconfig

cd ../fig_regime
KUBECONFIG=~/.kube/kind-dratsn.kubeconfig  RUN=demo ./run.sh setup
KUBECONFIG=~/.kube/kind-dratsn.kubeconfig  RUN=demo ./run.sh fill dratsn
KUBECONFIG=~/.kube/kind-vanilla.kubeconfig RUN=demo ./run.sh setup
KUBECONFIG=~/.kube/kind-vanilla.kubeconfig RUN=demo ./run.sh fill vanilla

KUBECONFIG=~/.kube/kind-dratsn.kubeconfig  python3 live_serve.py --arm dratsn \
    --state results/demo/state.env --port 8787 &
KUBECONFIG=~/.kube/kind-vanilla.kubeconfig python3 live_serve.py --arm vanilla \
    --state results/demo/state-vanilla.env --port 8788 &
# 확장 설정: --/exts/dratsn.live/urls="http://<ip>:8787/state,http://<ip>:8788/state"
```

**인터랙티브 fault** — 스크립트 없이 운영자가 직접:

```bash
# 동시 주입: 기본 = vPLC가 앉은 노드 (body에 {"node": "..."} 지정 가능)
curl -X POST localhost:8787/inject -d '{}' & curl -X POST localhost:8788/inject -d '{}' & wait
# 관전 포인트: 일반 노드를 죽이면 아무 일도 안 일어난다 — 그것도 스토리
curl -X POST localhost:8787/inject -d '{"node": "dratsn-worker5"}'
# 복구 (docker start; 스트림 수리는 다음 setup이 수행)
curl -X POST localhost:8787/restore -d '{}'
```

가드: 진행 중 fault가 있으면 409 (`{"force": true}`로 우회), 죽은 노드 재사살
409, 마지막 TSN 생존 노드를 위협하면 응답 `notes`에 경고(vPLC Pending 가능 —
정직한 실패 모드라 시연해도 됨).

씬은 한 클러스터(paper2, dratsn-worker*) 기준으로 구워졌지만 라이브 클러스터
이름이 달라도 됨 — 존의 `Nodes/` 자식 프림을 **인덱스 기반 별칭**으로 매핑
(랙=클래스, 레벨=순번이라는 ADR-0033의 위치 기반 정체성 그대로. 예:
`vanilla-worker2` → baked `dratsn_worker2`).

## 설정 (`--/exts/dratsn.live/<key>=`)

| key | default | 의미 |
|---|---|---|
| `url` | `http://127.0.0.1:8787/state` | live_serve 엔드포인트 |
| `poll_hz` | 2.0 | pull 주기 |
| `scripts_dir` | `/isaac-sim/Documents/dratsn_factory/scripts` | `sim_model.py` 위치 (게이지 SIGMA) |
| `auto_stage` | `""` | ClusterOverlay 없는 스테이지가 열려 있으면 이 씬을 오픈 (30s 쿨다운) |

## 무엇이 라이브인가

| 스냅샷 필드 | 프림 | 표현 |
|---|---|---|
| `cluster.nodes[].pods` | `Nodes/<n>/Cores/Core_0..7` 바인딩 | blade 채움 (largest-remainder, 1 blade=1500m) |
| `fault_node` + NotReady | Platform/Lane/Trunk/Cap 리바인드 + Dead 재질 desaturate | 노드 사망; 복귀 시 역방향 |
| `tracked` / `peer` | `Actors/Tracked·OtherLoad` | 0.8 s 러프 이동, down 시 DEAD_WIRE |
| `evictions[]` | `Actors/Victim_i` | 선점 도미노, 빈 슬롯을 vPLC가 재사용 |
| `stages.detect/sched` | Platform/LaneProt emissive 펄스 | Filter/Score 모멘트 |
| tracked 위치 | `Ribbon/Stream_0` 가시성, `Binding` 곡선 | 보호 레인 점유, 수직 바인딩 |
| (매 tick) | `DelayGauge` | ADR-0036 선언 lognormal 리샘플, >1 ms=적색 |

vanilla 암은 `--arm vanilla`로 서버를 재시작하면 VanillaK8s 존이 움직인다.
spare 암은 존이 없어 서버가 거부한다. 라이브 배치가 baked 대표 trial과 달라도
정상 — 없는 프림(Stream_n, Victim_i)은 세션 레이어에 정의된다.

## 검증

로컬(usd-core venv, omni 불필요):

```bash
STAGE=<scene>/factory_three_cells_v11.usda python3 isaac-sim/test_overlay_map.py
```

상수 parity(ast로 build_factory_scene.py 대조), 골든 ops(명명 계약·blade 보존·
착지 노드), 세션 마스킹(실물 v11에서 timecode 0 해석값 = 라이브 값) 전부 PASS여야 한다.

## 스크린샷 (디버그/시연 스틸)

`documents/dratsn_factory/capture.req` 파일을 만들면 확장이 현재 뷰포트를
`preview/live_capture.png`로 저장한다. 파일 내용에 카메라 프림 경로를 넣으면
그 카메라로 전환 후 캡처 (스트림 뷰도 같이 바뀜):

```bash
echo /World/Cameras/OverviewCluster | sudo tee documents/dratsn_factory/capture.req
```

## 알려진 주의점

- `extension.toml` deps에 `omni.kit.app`을 넣지 말 것 — kit 코어라 확장
  레지스트리 원격 해석에 빠져 부팅이 죽는다(exit 55). 컨테이너에선
  `--/app/extensions/registryEnabled=0`으로 원천 차단.
- 확장 startup이 앱 부팅보다 빠르므로 씬 오픈은 update 루프에서 자가 치유
  방식으로 수행된다(`auto_stage`). **재오픈은 URL 비교로 막는다** — 로딩 중인
  씬에 open을 한 번 더 쏘면 스테이지 객체가 교체되며 이미 칠한 세션 레이어가
  통째로 사라진다 (증상: 라이브 fault가 안 그려지고 baked 스토리의 fault
  노드만 까매짐). 스테이지 교체는 stage_id로 감지해 전체 재도색 + 타임라인
  정지로 대응한다 — replay 애니메이션이 라이브 밑에서 돌면 안 된다.
- `_tick` 안에서 `import omni.xxx` 금지 — 함수 스코프에서 `omni`가 지역변수로
  가려져 UnboundLocalError. 모듈 상단에서 import.
- **타임라인은 라이브 모드가 잠근다** — 클라이언트에서 Play를 눌러도 즉시
  정지+프레임 0 복귀. 재생하면 baked 스토리가 라이브가 아직 저작 안 한 속성
  으로 되살아나 한 프림에 두 스토리가 겹친다(부품+완성품 동시 표시). baked
  replay를 보려면 확장을 끄고(또는 서버 없이) 재생할 것.
- 장수 kind 클러스터에서 DRA claim prepare가 wedge되면 해당 노드의 드라이버
  파드를 재생성하고 ResourceSlice 재발행을 기다린 뒤 setup/fill을 재시도
  (ADR-0037 후기).
- `nerdctl restart`가 healthcheck timer unit 충돌로 실패하면 stop → start로 우회.
