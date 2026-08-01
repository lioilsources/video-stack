#!/usr/bin/env bash
set -u
cd ~/Code/video-stack
OUT=reports/phase3_bench.tsv
echo -e "workflow\tres\tframes\tsec\tpeak_mb\tstatus" > "$OUT"
echo -e "t2v_final_14b_lightning.json\t832x480\t33\t135\t83610\tok" >> "$OUT"
for wf in workflows/t2v_draft_5b.json workflows/t2v_final_14b_lightning.json workflows/i2v_final_14b_lightning.json; do
  for res in "832 480" "1280 704"; do
    for len in 33 81; do
      [ "$wf" = "workflows/t2v_final_14b_lightning.json" ] && [ "$res" = "832 480" ] && [ "$len" = 33 ] && continue
      python3 bench_matrix.py "$wf" $res "$len" >> "$OUT" 2>&1 || echo -e "$wf\t${res// /x}\t$len\t-\t-\tFAILED" >> "$OUT"
    done
  done
done
echo "MATRIX DONE" >> "$OUT"
