# DRA-TSN 운영·실험 가이드 (docs/)

설계 결정(`context/`, ADR)과 별개로, **직접 따라 하는 셋업·검증 절차**를 모은다.
"왜"는 ADR/SSOT, "어떻게(손으로)"는 여기.

## 재현 가능한 평가 실험 (논문 5장 figure)
- [`../experiments/`](../experiments/README.md) — figure당 `run.sh → results/ → plot.py` 파이프라인.
  배치 정확률(fig_placement)은 **kind로도 재현 가능**,
  태깅/DNAT 2×2(fig_tagging)는 실 NIC 필요. `tests/`(스모크: 배포가 맞는가)와 달리
  experiments/는 **측정·figure 생성**이 목적.

## 데이터플레인 검증
- [`e2e_run_book.md`](e2e_run_book.md) — tcpdump로 TX/RX 경로 위치별 VLAN/SNAT/DNAT/strip 수동 확인 런북.

## 지연 측정 (스케줄링 정확도 → 실 지연 보장)
- [`vplc-setup.md`](vplc-setup.md) — vPLC 노드 셋업 따라치기 런북 (코어 격리 isolcpus + NIC IRQ 핀 + C-state 캡 + Guaranteed QoS). provisioning과 무관.
- [`actuator-setup.md`](actuator-setup.md) — actuator(로봇팔) 노드 셋업 따라치기 런북 (NIC 코어 격리 + C-state 캡 + IRQ 핀 + rxvlan off + XDP reflect attach). arm-1/arm-2 빌드 차이 포함.
- [`latency-measurement.md`](latency-measurement.md) — 호스트 내부 + 크로스노드 end-to-end 지연 실측 (eBPF TAI 타임스탬프). Isaac-sim twin 입력.

관련 결정: ADR-0024(측정 vs netem 모델 분리), ADR-0028(격리망 크로스노드 실측), ADR-0019(노드 전제 = DRA scope 밖).

## 자동화 스크립트
위 셋업 절차의 실행 스크립트는 [`../scripts/`](../scripts/):
- 환경 조성: `nic-irq-pin.sh`, `vplc-deploy.sh` (grub 격리는 수동 — vplc-setup.md §1)
- round-trip 측정: `probe-loop.py`(pod 송수신), `echo-ext.py`(외부 echo), `measure-collect.sh`(eBPF 맵 bpftool 덤프),
  `measure-stats.sh`(seq join → 구간별 p50/p99/max + CYCLE). **PTP 불요**(전부 w1 클럭).

문서가 "왜/무엇", scripts가 "실행".
