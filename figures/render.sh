#!/usr/bin/env bash
# Render all DRATSN figures from D2 source to SVG (and PDF if supported).
# Requires: d2 (https://d2lang.com). Install: download the release binary to ~/.local/bin.
set -euo pipefail
cd "$(dirname "$0")"

D2="${D2:-d2}"
# 한글 라벨: d2 기본 번들 폰트에 CJK 글리프가 없어 전부 두부로 찍힌다.
# venv에 설치된 NanumGothic을 regular/bold/italic 모두에 넘긴다
# (italic까지 넘기지 않으면 엣지 라벨만 두부로 남는다).
KFONT="${KFONT:-../experiments/.venv/lib/python3.12/site-packages/koreanize_matplotlib/fonts}"
FONT_ARGS=()
if [ -f "$KFONT/NanumGothic.ttf" ]; then
  FONT_ARGS=(--font-regular "$KFONT/NanumGothic.ttf"
             --font-bold "$KFONT/NanumGothicBold.ttf"
             --font-italic "$KFONT/NanumGothic.ttf")
fi
LAYOUT="${LAYOUT:-elk}"   # elk = orthogonal routing (cleaner block diagrams); dagre also works
FMT="${FMT:-svg}"         # svg | pdf | png

for f in f*.d2; do
  out="${f%.d2}.${FMT}"
  echo "render $f -> $out"
  "$D2" --layout "$LAYOUT" --pad 30 "${FONT_ARGS[@]}" "$f" "$out"
done
echo "done."
