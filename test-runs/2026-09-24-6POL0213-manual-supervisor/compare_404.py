"""What the peer's fix/404-chunk-on-output-budget branch would decide on 6POL0213.
Runs the branch's chunking.py / utils.py copies (exported via git show) side by side with main."""

import importlib.util
import json
import sys
from pathlib import Path

REPO = "/Users/mriechers/Developer/cardigan/.claude/worktrees/linear-dreaming-bumblebee"
sys.path.insert(0, REPO)
RUN = Path(__file__).resolve().parent
SRT = (RUN / "6POL0213.srt").read_text()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


from api.services.chunking import split_transcript as main_split  # noqa: E402
from api.services.speaker_segmentation import split_interior_speaker_changes  # noqa: E402
from api.services.utils import calculate_transcript_metrics as main_metrics  # noqa: E402

u404 = load("utils_404", RUN / "utils_404.py")
c404 = load("chunking_404", RUN / "chunking_404.py")
cfg404 = json.loads((RUN / "llm-config_404.json").read_text())
cfg_main = json.loads((Path(REPO) / "config/llm-config.json").read_text())

print("main  word_count:", main_metrics(SRT, long_form_threshold_minutes=15)["word_count"])
print(
    "#404  word_count (is_srt=True):",
    u404.calculate_transcript_metrics(SRT, long_form_threshold_minutes=15, is_srt=True)["word_count"],
)

split = split_interior_speaker_changes(SRT)
ch_main = main_split(split, is_srt=True, config=cfg_main["routing"]["chunking"])
print(
    "main  chunking:", "single call" if ch_main is None else f"{len(ch_main)} chunks", cfg_main["routing"]["chunking"]
)

chunk404 = cfg404["routing"]["chunking"]
cap = cfg404["backends"]["openrouter"].get("max_tokens")
budget = c404.output_word_budget(cap, chunk404.get("tokens_per_word", 2.0), chunk404.get("safety_factor", 0.8))
print("#404  openrouter max_tokens:", cap, "budget words:", budget, "chunking cfg:", chunk404)
ch404 = c404.split_transcript(split, is_srt=True, config=chunk404, max_output_tokens=cap)
print("#404  chunking:", "single call" if ch404 is None else f"{len(ch404)} chunks")
for tpw in (1.2, 1.4, 1.77, 2.0):
    b = c404.output_word_budget(cap, tpw, chunk404.get("safety_factor", 0.8))
    print(f"   tokens_per_word={tpw}: budget {b} words -> {'chunk' if 3333 > b else 'single'}")
