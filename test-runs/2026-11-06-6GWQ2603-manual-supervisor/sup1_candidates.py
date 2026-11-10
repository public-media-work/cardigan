"""SUP-1: rebuild the timestamp prompt with `- Name:` speaker labels added as candidates.

Uses the code-under-test's own parse_srt / format_youtube / _excerpt and widens only
_SPEAKER_TRANSITION_RE to also accept a leading `- Name:` label.
Args: code_dir srt_path src_dir dst_dir
"""

import re
import shutil
import sys
from pathlib import Path

code_dir, srt_path, src_dir, dst_dir = (Path(p) for p in sys.argv[1:5])
sys.path.insert(0, str(code_dir))

from api.services.style_engine import pre_stage  # noqa: E402
from api.services.utils import parse_srt  # noqa: E402

pre_stage._SPEAKER_TRANSITION_RE = re.compile(r"^(?:>>\s*|\*\*[A-Za-z][\w.'\s-]*:\*\*|-\s*[A-Z][\w.'\s-]*:)")
caps = parse_srt(srt_path.read_text(encoding="utf-8"))
cands, _ = pre_stage._subsample_candidates(pre_stage._extract_boundary_candidates(caps))
lines = [f"- {c['formatted']} — {c['label']} ({c['kind']})" for c in cands]

user = (src_dir / "user.md").read_text(encoding="utf-8")
old = "**Boundary candidates:** choose boundaries ONLY from this candidate list:\n- 0:01 — [bright music] (music_cue)\n"
assert old in user, "anchor not found"
new = "**Boundary candidates:** choose boundaries ONLY from this candidate list:\n" + "\n".join(lines) + "\n"
dst_dir.mkdir(parents=True, exist_ok=True)
(dst_dir / "user.md").write_text(user.replace(old, new), encoding="utf-8")
shutil.copy(src_dir / "system.md", dst_dir / "system.md")
print(f"{len(cands)} candidates")
print("\n".join(lines))
