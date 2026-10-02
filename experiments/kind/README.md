# kind — control plane 기능 재현 경로 (하드웨어 불필요)

**목적**: DRATSN driver 파이프라인 **전체**(mock-cnc → controller → TSNNic
discovery → ResourceSlice publish → 스케줄링)를 실제로 돌려 fig_placement를
재현한다. `-dataplane=false`로 eBPF/NRI만 끈다.
(fig_scale의 KWOK는 가짜 노드라 driver가 못 돌고 ResourceSlice를 합성 주입 —
역할이 다르다: kind = 파이프라인 기능 재현, KWOK = 스케줄러 확장성.)

**전제**: docker + kind (https://kind.sigs.k8s.io) + go(이미지 빌드).
멀티노드 join 실패 시: `sudo sysctl -w fs.inotify.max_user_instances=1024
fs.inotify.max_user_watches=1048576`.

**실행**:
```bash
./create-cluster.sh      # 1 control-plane + 6 workers: TSN 3 + 일반 3 (v1.34, ADR-0037)
./deploy.sh              # build → kind load → 배포(-dataplane=false) → 스트림 push
./deploy-gpu.sh          # 두 번째 DRA 드라이버 gpu.example.com — fig_mixed가 요구
./run-experiments.sh     # fig_placement + plot
kind delete cluster --name dratsn
```

같은 클러스터 위에서 `../fig_mixed/`(경로 소실 재배치, ADR-0039)도 돈다 —
단 라이브 경로는 arm마다 클러스터가 하나씩 필요하다:
`KIND_CLUSTER=vanilla ./create-cluster.sh` + `deploy.sh` + `deploy-gpu.sh`.
`fig_mixed`는 `deploy-gpu.sh`를 **하드 요구**한다(없으면 preflight에서 die).

`../fig_regime/run.sh`와 `../fig_twin/run_fault.sh`도 여전히 돌지만 둘 다
대체된 기록이다(ADR-0039 / ADR-0037).

감지 상수는 `kind-config.yaml`이 조여둔다(KCM grace 8s + kubelet lease 8s →
감지 ~8s, kube 기본값은 ~44s). ADR-0039 §10.

**검증**: run-experiments.sh에서 placement 정확률 100%.
