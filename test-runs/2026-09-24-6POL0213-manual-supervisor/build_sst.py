"""Build sst_context.json exactly as worker._fetch_sst_context would from the live
Airtable record for 6POL0213 (values pulled read-only via the Airtable MCP)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/mriechers/Developer/cardigan/.claude/worktrees/linear-dreaming-bumblebee")
from api.services.worker import _extract_speakers_from_sst  # noqa: E402

RUN = Path(__file__).resolve().parent

NOTES = (
    "**WPM News Politics Roundup**\nWPR News and PBS Wisconsin News are collaborating to launch a new political "
    "news product in 2026. We know reporting on Wisconsin state government and politics news is one of the primary "
    "areas of interest for our readers, listeners and viewers. Our coverage in this area rivals or is superior to "
    "anything else in the market and there is an opportunity to reach new audiences by tailoring the delivery of "
    "this content to their needs. The weekly show will build on areas of strength for both teams and serve highly "
    "engaged audiences across a variety of platforms.\n\nThe show will be a 17-18 minute discussion of the most "
    "significant political and government news of the week. It will focus on diving deep into what various actions "
    "by state leaders mean for residents and what significance their actions might have politically. The "
    "conversational round table will typically be led by WPR Capitol Bureau Chief Shawn Johnson and include PBS "
    "Wisconsin News Senior Political Reporter Zac Schultz and WPR Political Reporters Anya van Wagtendonk and Rich "
    "Kremer. Conversations will be capped at four participants and typically include at least three of the core "
    "team. Other reporters may rotate in as their coverage overlaps with the scope of the round table. \n\n"
    "_Fundraising for this project is attached to the Focus Fund for Journalism (as of 4.2.26 LB)_\n\n"
)
DESC = (
    "WPR News and PBS Wisconsin News are collaborating to launch a new political news product in 2026. We know "
    "reporting on Wisconsin state government and politics news is one of the primary areas of interest for our "
    "readers, listeners and viewers. Our coverage in this area rivals or is superior to anything else in the market "
    "and there is an opportunity to reach new audiences by tailoring the delivery of this content to their needs. "
    "The weekly show will build on areas of strength for both teams and serve highly engaged audiences across a "
    "variety of platforms.\n"
)

sst = {
    "title": "Republican mega-donors, gas costs and fighting 'corruption' | Inside Wisconsin Politics",
    "short_description": "Mega-donors fund Republican candidates, gas prices, and corruption as an issue.\n",
    "long_description": (
        "Mega-donors fund Republican candidates, gas prices cause growing economic pain, and corruption emerges as "
        "an issue — Inside Wisconsin Politics looks at how each development impacts the 2026 election."
    ),
    "keywords": None,  # General Keywords/Tags: field exists, empty on this record
    "host": None,  # Host: field exists, empty
    "presenter": None,  # Presenter: field exists, empty
    "media_id": "6POL0213",
    "social_media_description": None,  # field name does NOT exist on SST table (422) -> always None
    "program": "Inside Wisconsin Politics",
    "project_notes": NOTES,
    "project_description": DESC,
}
if not sst.get("host"):
    ex = _extract_speakers_from_sst(sst)
    print("extracted speakers:", ex)
    if ex.get("host"):
        sst["host"] = ex["host"]
    if ex.get("panelists") and not sst.get("presenter"):
        sst["presenter"] = ", ".join(ex["panelists"])
sst = {k: v for k, v in sst.items() if v is not None}
(RUN / "sst_context.json").write_text(json.dumps(sst, indent=2))
print(json.dumps({k: (v[:70] + "..." if isinstance(v, str) and len(v) > 70 else v) for k, v in sst.items()}, indent=2))
