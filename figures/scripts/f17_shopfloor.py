#!/usr/bin/env python3
"""figures/f17_shopfloor.{svg,png} — 경로 등급이 현장 결과로 환산되는 모습 (5.6절).

입력은 측정이 아니라 **등급**이다. 측정 타임라인이 등급(P0/P1/P2)을 고르고,
선언 모델이 그 등급을 불량률로 바꾼다 (ADR-0031, ADR-0036). 이 그림은 그 두
번째 단계만 보여 준다 — 즉 "경로가 P0이면 현장에서 무엇이 달라지는가".

  P0  인식 + 집행. CNC가 예약한 보호 창 안 (지연 < QBV_BOUND_MS)
  P1  TSN 노드에 있으나 무태깅. 같은 물리 경로를 최선형으로
  P2  경로 없는 노드. 더 긴 최선형 경로

좌: 선언 지연 중앙값 lambda 스윕에 따른 부품 불량 확률. P0는 예약 경계 안이라
    평평하고, 무보호 등급은 전이 구간에서 솟는다. sim_model이 P1과 P2를 같은
    분포로 다루므로 한 곡선으로 그린다.
우: 선언 운전점에서의 10분 환산 — 정상품과 불량품의 개수.

실행:
    experiments/.venv/bin/python figures/scripts/f17_shopfloor.py
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(REPO, "figures")
# sim_model은 isaac-sim 쪽에 있고 stdlib만 쓴다. 그림과 장면이 같은 모델을
# 공유해야 하므로 재구현하지 않고 import 한다. 두 레이아웃을 모두 지원한다:
# 개발 워크스페이스는 isaac-sim이 레포의 형제, 공개 레포는 레포 하위.
for _cand in (os.path.join(REPO, "isaac-sim"),
              os.path.normpath(os.path.join(REPO, "..", "isaac-sim"))):
    if os.path.exists(os.path.join(_cand, "sim_model.py")):
        sys.path.insert(0, _cand)
        break
import sim_model as M  # noqa: E402

# palette (f8_cycle_ccdf.py와 동일)
BLUE, AQUA, YEL, RED = "#2a78d6", "#1baf7a", "#eda100", "#d64545"
GREY, INK, INK2 = "#8a8a86", "#0b0b0b", "#52514e"
# 한글 글리프: 시스템 폰트에 CJK가 없으므로 venv에 설치된 NanumGothic을
# 직접 등록한다 (pip install koreanize-matplotlib). 없으면 영문만 렌더된다.
for _cand in ("koreanize_matplotlib/fonts/NanumGothic.ttf",):
    _p = os.path.join(os.path.dirname(os.path.dirname(matplotlib.__file__)), _cand)
    if os.path.exists(_p):
        matplotlib.font_manager.fontManager.addfont(_p)
        plt.rcParams["font.family"] = "NanumGothic"
        break

plt.rcParams.update({
    "font.size": 11, "axes.edgecolor": "#cfcfca", "axes.linewidth": 0.8,
    "axes.unicode_minus": False,   # NanumGothic에 U+2212 글리프가 없다
    "text.color": INK, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "figure.facecolor": "white", "axes.facecolor": "white",
})

# 운전점은 fig_mixed/plot.py의 LAM_MS와 같은 값이어야 한다. 두 그림이 같은 장에
# 실리는데 운전점이 다르면 그 자체가 지적 대상이다.
LAM_OP = 0.7
MINUTES = 10
# sim_model은 P1과 P2를 구분하지 않는다(sample_delay: P0만 예약 경계 안의 균일
# 추출, 나머지는 같은 로그정규). 등급을 셋으로 그리면 모델에 없는 차이를 그리는
# 것이 되므로, 무보호 두 등급을 한 곡선으로 둔다. 둘의 차이는 지연 분포가 아니라
# "어느 노드에 있느냐"이고, 그것은 5.5의 배치 결과가 말한다.
CLASSES = ((M.P0, "P0  경로 인식 + 집행 (예약 경계 안)", BLUE),
           (M.P1, "P1 · P2  무보호 (무태깅 또는 경로 없음)", RED))


def defect_curve(cls, lams):
    """등급 cls의 부품 불량 확률 곡선.

    P0는 예약 경계(QBV_BOUND_MS) 안에서 균일 추출이라 lambda와 무관하다.
    P1/P2는 선언 로그정규에서 추출하므로 lambda를 따라간다.
    """
    n = M.n_control_cycles()
    out = []
    for lam in lams:
        # P0: 모든 추출이 예약 경계(QBV_BOUND_MS < T_CYCLE_MS) 안이므로 마감
        # 초과가 구조적으로 불가능하다. 나머지는 선언 로그정규.
        q = 0.0 if cls == M.P0 else M.miss_rate(lam)
        out.append(M.defect_prob(q, n, M.RUN_THRESHOLD))
    return np.array(out)


def main():
    lams = np.logspace(np.log10(0.05), np.log10(5.0), 160)
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(11.2, 4.1),
                                 gridspec_kw={"width_ratios": [1.45, 1]})

    # --- (a) lambda 스윕 -----------------------------------------------------
    for cls, label, color in CLASSES:
        ax.plot(lams, defect_curve(cls, lams) * 100.0, color=color, lw=2.2,
                label=label)
    ax.axvline(LAM_OP, color=GREY, lw=1.0, ls="--")
    ax.annotate(f"선언 운전점 λ = {LAM_OP} ms", xy=(LAM_OP, 70),
                xytext=(LAM_OP * 1.3, 72), color=INK2, fontsize=10)
    ax.axvspan(M.QBV_BOUND_MS, M.T_CYCLE_MS, color=RED, alpha=0.06)
    ax.annotate("예약 경계 1 ms ~ 주기 2 ms", xy=(M.QBV_BOUND_MS, 8),
                xytext=(M.QBV_BOUND_MS * 1.02, 8), color=INK2, fontsize=9)
    ax.set_xscale("log")
    ax.set_xlabel("선언 지연 중앙값 λ (ms)")
    ax.set_ylabel("부품 불량 확률 (%)")
    ax.set_ylim(-3, 103)
    ax.set_title(f"경로 등급별 불량 확률 (연속 초과 런 R = {M.RUN_THRESHOLD})",
                 fontsize=12, loc="left")
    ax.legend(frameon=False, fontsize=10, loc="center left")
    ax.grid(axis="y", color="#ececea", lw=0.8)
    ax.set_axisbelow(True)

    # --- (b) 운전점에서의 10분 환산 -----------------------------------------
    rate = 60.0 / (M.DWELL_REAL_MS / 1000.0 + M.MOVE_REAL_MS / 1000.0)
    total = rate * MINUTES
    labels, good, bad = [], [], []
    for cls, label, _c in CLASSES:
        p = float(defect_curve(cls, [LAM_OP])[0])
        labels.append("P0" if cls == M.P0 else "P1 · P2")
        good.append(total * (1 - p))
        bad.append(total * p)
    y = np.arange(len(labels))
    bx.barh(y, good, color=AQUA, height=0.52, label="정상품")
    bx.barh(y, bad, left=good, color=RED, height=0.52, label="불량품")
    for i, (g, b) in enumerate(zip(good, bad)):
        bx.text(total * 1.02, i, f"{g:.0f} / {g + b:.0f}", va="center",
                fontsize=10, color=INK2)
    bx.set_yticks(y, labels)
    bx.invert_yaxis()
    bx.set_xlim(0, total * 1.25)
    bx.set_xlabel(f"{MINUTES}분간 생산 부품 수  (takt {M.CYCLE_REAL_MS / 1000:.0f} s)")
    bx.set_title(f"운전점 환산 (λ = {LAM_OP} ms)", fontsize=12, loc="left")
    bx.legend(frameon=False, fontsize=10, ncols=2,
              loc="lower center", bbox_to_anchor=(0.5, -0.33))
    bx.grid(axis="x", color="#ececea", lw=0.8)
    bx.set_axisbelow(True)

    fig.text(0.005, 0.015,
             "등급은 측정 타임라인이 고르고, 불량률은 선언 분포가 만든다. "
             "절대 수치를 산업 사례와 직접 비교하지 않는다.",
             fontsize=9, color=GREY)
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    for ext in ("svg", "png"):
        path = os.path.join(OUT, f"f17_shopfloor.{ext}")
        fig.savefig(path, dpi=170)
        print("wrote", path)


if __name__ == "__main__":
    main()
