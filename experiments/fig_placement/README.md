# fig_placement — 배치 정확률 (baseline vs DRATSN)

**목적**: TSN ResourceClaim이 있는 Pod가 TSN 경로 보유 노드에만 배치됨을 보인다.
claim 없는 동일 Pod(baseline)는 스케줄러가 TSN 경로에 blind → 노드 분산.

**전제**: DRATSN 스택 배포 완료(실클러스터 또는 `../kind/`), mock-cnc에 cell-A
스트림 존재(run.sh가 `ensure_streams`로 자동 복원). 실험 시작 시 `mock-vplc`
fixture가 있으면 삭제된다(4 device 점유 해제).

**실행**:
```bash
TRIALS=30 RUN=myrun ./run.sh
../.venv/bin/python plot.py results/myrun
```

**출력**: `results/<RUN>/placement.csv` (`arm,trial,node,sched_ms`),
`fig_placement.png` (노드별 착지 grouped bar + on-TSN-node rate).

**검증**: dratsn arm의 모든 trial이 TSN 노드(`TSN_NODE`)에 착지하면 run.sh가
`[PASS] dratsn placement correctness 100%` 출력, 오배치가 있으면 FAIL(비0 exit).
