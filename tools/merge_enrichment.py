#!/usr/bin/env python3
"""
Merge swarm-generated structured metadata into the extracted corpus.

Reads enrichment records (one object per post, keyed by integer `index`) from
data/enrichment.json, then:
  * adds the enrichment fields to every post entry in data/index.json
  * writes the same fields back into each post's data/posts/<folder>/meta.json

Enrichment record fields: sighting_year, location, state, craft_shape,
occupants, effects[], source_type, summary, keywords[].
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")  # served by GitHub Pages from /docs
ENRICH = os.path.join(DATA, "enrichment.json")
INDEX = os.path.join(DATA, "index.json")

FIELDS = ["sighting_year", "location", "state", "craft_shape",
          "occupants", "effects", "source_type", "summary", "keywords"]


def main() -> int:
    if not os.path.exists(ENRICH):
        print(f"missing {ENRICH}", file=sys.stderr)
        return 1
    raw = json.load(open(ENRICH))
    records = raw["records"] if isinstance(raw, dict) and "records" in raw else raw
    by_index = {}
    for r in records:
        if isinstance(r, dict) and "index" in r:
            by_index[int(r["index"])] = r
    print(f"loaded {len(by_index)} enrichment records")

    # index.json
    index = json.load(open(INDEX))
    matched = 0
    for p in index["posts"]:
        e = by_index.get(int(p["index"]))
        if e:
            for f in FIELDS:
                p[f] = e.get(f)
            matched += 1
    index["enriched"] = True
    index["enriched_count"] = matched
    json.dump(index, open(INDEX, "w"), ensure_ascii=False, indent=2)
    print(f"index.json: enriched {matched}/{len(index['posts'])} posts")

    # per-post meta.json
    meta_updated = 0
    for meta_path in glob.glob(os.path.join(DATA, "posts", "*", "meta.json")):
        m = json.load(open(meta_path))
        e = by_index.get(int(m.get("index", -1)))
        if e:
            for f in FIELDS:
                m[f] = e.get(f)
            json.dump(m, open(meta_path, "w"), ensure_ascii=False, indent=2)
            meta_updated += 1
    print(f"meta.json updated: {meta_updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
