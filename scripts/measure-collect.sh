#!/usr/bin/env bash
# Collect eBPF timestamp maps into CSV (docs/latency-measurement.md, ADR-0028).
# Dumps the pinned tx_ts / rx_ts ARRAY maps via `bpftool map dump` (no Go reader
# needed — script-based). Emits tx_ts.csv "seq,t1_ns,t2_ns" and rx_ts.csv "seq,t3_ns",
# keeping only entries with a non-zero timestamp. Run ON the vPLC node, as root.
#
# Stale entries from a prior run are NOT cleared here; the stats step filters them by
# absolute TAI time (any ts < run start = leftover). See measure-stats.sh.
#
# Usage: sudo ./measure-collect.sh            # dump tx_ts + rx_ts
#        sudo PIN_DIR=/sys/fs/bpf/tsn ./measure-collect.sh
set -euo pipefail
. "$(dirname "$0")/env.sh"
need_root
have bpftool || die "bpftool required"
have python3 || die "python3 required"
mkdir -p "$MEASURE_DIR"

dump_ts() { # <map name> <field csv (t1,t2)> <out csv> <header>
  local name="$1" fields="$2" out="$3" header="$4" pin="$PIN_DIR/$1"
  [ -e "$pin" ] || { warn "$pin not found (data plane loaded? maps pinned?)"; return 1; }
  local tmp; tmp=$(mktemp "$MEASURE_DIR/.${name}.json.XXXX")
  bpftool map dump pinned "$pin" -j >"$tmp" 2>/dev/null || { warn "bpftool dump $name failed"; rm -f "$tmp"; return 1; }
  IN="$tmp" FIELDS="$fields" HEADER="$header" OUT="$out" python3 - <<'PY'
import os, json, struct
data=json.load(open(os.environ["IN"]))
fields=os.environ["FIELDS"].split(",")        # e.g. ["t1","t2"]
nf=len(fields)

def as_int(x):
    return int(x,16) if isinstance(x,str) else int(x)

def parse_key(k):
    # ARRAY key (__u32 index): formatted int, or LE byte-string list.
    if isinstance(k,list): return int.from_bytes(bytes(as_int(b) for b in k),"little")
    if isinstance(k,dict):                      # {"index":N} or single-field dict
        v=next(iter(k.values())); return as_int(v)
    return as_int(k)

def parse_vals(v):
    # value: formatted dict {"t1":N,"t2":N} | byte-string list | nested {"value":[...]}.
    if isinstance(v,dict):
        if all(f in v for f in fields): return [as_int(v[f]) for f in fields]
        if "value" in v: return parse_vals(v["value"])
    if isinstance(v,list):
        b=bytes(as_int(x) for x in v)
        return list(struct.unpack_from("<%dQ"%nf, b))
    return None

rows=[]
for e in data:
    seq=parse_key(e.get("key"))
    ts=parse_vals(e.get("value"))
    if not ts or not any(ts): continue          # all-zero slot = never stamped
    rows.append(str(seq)+","+",".join(str(t) for t in ts))
with open(os.environ["OUT"],"w") as f:
    f.write(os.environ["HEADER"]+"\n"); f.write("\n".join(rows)); f.write("\n")
print(f"{os.environ['OUT']}: {len(rows)} stamped entries")
PY
  rm -f "$tmp"
}

dump_ts tx_ts "t1,t2" "$MEASURE_DIR/tx_ts.csv" "seq,t1_ns,t2_ns" || true
dump_ts rx_ts "t3" "$MEASURE_DIR/rx_ts.csv" "seq,t3_ns" || true
ok "collected -> $MEASURE_DIR/{tx_ts,rx_ts}.csv"
