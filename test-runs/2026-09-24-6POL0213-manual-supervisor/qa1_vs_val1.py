"""Would QA-1 alone unblock prod's paused backlog, or does VAL-1 have to land with it?

Read-only against cardigan01. For every paused job with a stored validation_result, re-run
prod's classify_qa_failure under three variants and report escalate-vs-pause:

  A  current NONFIXABLE_FLAG_PATTERNS (baseline = what prod did)
  B  QA-1 alone: drop "review note" / "needs_review" / "needs review" from the pattern list
  C  QA-1 + VAL-1: additionally drop any flag whose text is *about* a missing/absent review-notes
     block (the validator false positive VAL-1 removes at the source)

If FORMATTER_URL_TEMPLATE is given (e.g. "http://cardigan01:8100/api/jobs/{id}/outputs/formatter_output.md"),
the formatter artifact is fetched so the FORMATTER_CONTRACT_MARKERS half of the classifier runs too;
otherwise that half is skipped and reported as such.
"""

import json
import os
import re
import subprocess
import sys


class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text

    def json(self):
        return json.loads(self.text)


class _Curl:
    """curl-backed GET: the sandbox allows curl to the LAN host but not Python sockets."""

    def get(self, url):
        r = subprocess.run(["curl", "-s", "-m", "20", "-w", "\\n%{http_code}", url], capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        return _Resp(int(code or 0), body)


HTTP = _Curl()

REPO = "/Users/mriechers/Developer/cardigan/.claude/worktrees/linear-dreaming-bumblebee"
sys.path.insert(0, REPO)
from api.services import escalation as esc  # noqa: E402

BASE = os.environ.get("CARDIGAN_API_URL", "http://cardigan01:8100")
FMT_TPL = os.environ.get("FORMATTER_URL_TEMPLATE")

jobs = HTTP.get(f"{BASE}/api/queue/?limit=200").json()
jobs = jobs if isinstance(jobs, list) else jobs.get("jobs") or jobs.get("items")
paused = [j for j in jobs if j.get("status") == "paused"]

ORIG = list(esc.NONFIXABLE_FLAG_PATTERNS)
if os.environ.get("MARKERS_NEEDS_REVIEW_ONLY"):
    esc.FORMATTER_CONTRACT_MARKERS[:] = ["status:** needs_review", "status: needs_review"]
    print("variant D: artifact markers = needs_review status only (review-notes block presence ignored)")
QA1 = [p for p in ORIG if p not in ("review note", "needs_review", "needs review")]
MISSING_RN = re.compile(r"(missing|absent|no|lacks|does not (include|contain)|without)[^.]{0,60}review notes?", re.I)


def classify_with(patterns, vr, ctx):
    esc.NONFIXABLE_FLAG_PATTERNS[:] = patterns
    try:
        return esc.classify_qa_failure(vr, ctx)
    finally:
        esc.NONFIXABLE_FLAG_PATTERNS[:] = ORIG


def strip_val1(vr):
    """VAL-1: remove flags that only complain a review-notes block is missing."""
    out = json.loads(json.dumps(vr))
    for ph, r in out.get("phase_results", {}).items():
        flags = r.get("flags") or []
        kept = [f for f in flags if not MISSING_RN.search(f or "")]
        r["flags"] = kept
        if r.get("status") == "fail" and flags and not kept:
            r["status"] = "pass"
    out["overall"] = "fail" if any(r.get("status") == "fail" for r in out["phase_results"].values()) else "pass"
    return out


rows = []
for j in paused:
    d = HTTP.get(f"{BASE}/api/jobs/{j['id']}").json()
    vr = d.get("validation_result")
    err = (d.get("error_message") or "")[:70]
    if not vr:
        rows.append((j["id"], err, "no validation_result", "", "", ""))
        continue
    ctx = {}
    if FMT_TPL:
        r = HTTP.get(FMT_TPL.format(id=j["id"]))
        if r.status_code == 200:
            ctx["formatter_output"] = r.text
    a = classify_with(ORIG, vr, ctx)
    b = classify_with(QA1, vr, ctx)
    vr_c = strip_val1(vr)
    c = classify_with(QA1, vr_c, ctx) if vr_c.get("overall") == "fail" else {"escalate": None, "nonfixable": []}

    def tag(x):
        if x.get("escalate") is None:
            return "PASS-now"
        return "escalate" if x["escalate"] else "pause"

    rows.append((j["id"], err, tag(a), tag(b), tag(c), "; ".join(a["nonfixable"])[:110]))

print(f"paused jobs: {len(paused)}; formatter artifacts fetched: {'yes' if FMT_TPL else 'NO (marker half skipped)'}\n")
print(
    f"{'job':>4} {'prod error_message':<70} {'A:now':<10} {'B:QA-1':<10} {'C:QA-1+VAL-1':<12} first non-fixable flag (A)"
)
for r in rows:
    print(f"{r[0]:>4} {r[1]:<70} {r[2]:<10} {r[3]:<10} {r[4]:<12} {r[5]}")

from collections import Counter  # noqa: E402

for label, idx in (("A (prod today)", 2), ("B (QA-1 alone)", 3), ("C (QA-1 + VAL-1)", 4)):
    print(label, dict(Counter(r[idx] for r in rows if r[idx])))
