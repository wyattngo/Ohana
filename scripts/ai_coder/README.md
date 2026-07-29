# Vendored AI-coder map generator

`gen_codebase_map.py` + `conf.py` are **mirrors** of the canonical skill at
`.claude/skills/AI-coder/scripts/` (outside this repo, so CI cannot reach it).

CI runs `gen_codebase_map.py --check` to fail the build if `docs/…-map.md` no
longer matches the tree. When the skill's scripts change, re-copy both files here
byte-for-byte — do not edit these copies in place.
