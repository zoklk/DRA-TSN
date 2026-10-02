# fig_scale — 스케줄러 확장성 (KWOK, 10/100/1000 노드)

**목적**: 클러스터 규모가 커져도 TSN-aware 배치의 정확성(오배치 0, capacity+1
Pending)과 스케줄링 지연(batch makespan)이 유지됨을 보인다. baseline은 규모가
커질수록 우연히 TSN 노드에 착지할 확률이 붕괴 → 대비.

**범위**: kwokctl이 실제 kube-apiserver+kube-scheduler에 가짜 노드를 붙인다
(kubelet 없음). DaemonSet이 못 돌므로 ResourceSlice를 합성 주입 — 측정 대상은
**스케줄러의 DRA 할당**이며, controller/publisher 파이프라인은 kind 경로와
실클러스터가 담당.

**전제**: docker + kwokctl (https://kwok.sigs.k8s.io). K8s 버전은
`KWOK_KUBE_VERSION` env로 결정(기본 v1.34.3, DRA GA 필요 — ≥1.34).

**실행**:
```bash
NODES_LIST="10 100 1000" RUN=myrun ./run.sh   # KEEP_CLUSTER=1로 클러스터 유지
../.venv/bin/python plot.py results/myrun
```

**출력**: `results/<RUN>/scale.csv`
(`nodes,arm,pods,makespan_ms,on_tsn,mispl,overflow_pending`),
`fig_scale.png` (정확도 bar + makespan line).

**검증**: 모든 규모에서 dratsn arm이 `mispl=0`, `overflow_pending=yes`면 PASS.
