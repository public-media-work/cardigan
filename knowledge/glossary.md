# PBS Wisconsin Transcript Glossary

Authoritative spelling and naming reference for all transcript processing agents (analyst, formatter, copy-editor). When in doubt, this file is correct — do NOT "autocorrect" names to more common spellings.

## How to Use This Glossary

- **Before formatting**: Scan the glossary for names and terms that appear in the transcript
- **When a name has multiple common spellings**: Use the spelling listed here, not the one the model "prefers"
- **After editor review**: New corrections should be added to the appropriate section

---

## Whisper Prompt Terms

Terms merged into the WhisperX `initial_prompt` for audio transcription jobs.
This is Cardigan's project layer of the workspace glossary (base:
`automations/transcripts/glossary.md` in the pbswi workspace); the project
layer wins on conflict. Only lines beginning with `- ` are injected into
prompts, lowest priority after speaker names and per-job context terms —
keep this header prose bullet-free.

- PBS Wisconsin
- Frederica Freyberg
- Shawn Johnson
- Zac Schultz
- Rich Kremer
- Anya van Wagtendonk
- Janet Protasiewicz
- Brian Hagedorn
- Michael Gableman
- Jim Troupis
- Brad Schimel
- Jill Karofsky
- Rebecca Dallet
- Josh Kaul
- Eric Toney
- Sean Duffy
- Manitowoc
- Waukesha
- Sheboygan
- Oconomowoc
- Fond du Lac
- Wauwatosa
- Menominee
- Ashwaubenon
- Wausau
- Eau Claire
- La Crosse
- Kenosha
- Oshkosh
- Wisconsin Elections Commission
- Act 10
- Tavern League
- Attorney General Kaul

## Place Names

| Correct | Common Misspellings |
|---------|-------------------|
| Manitowoc | Manitowac, Mannitowoc |
| Waukesha | Wakesha, Walkeesha |
| Sheboygan | Sheyboygan, Sheboygen |
| Oconomowoc | Oconowomoc, Oconomowac |
| Fond du Lac | Fond de Lac, Fondalac |
| Wauwatosa | Wawatosa, Wauwautosa |
| Menominee | Menomonie, Menomonee |
| Ashwaubenon | Ashwaubanon |
| Wausau | Wasau |
| Eau Claire | Eau Clair |
| La Crosse | Lacrosse, La Cross |
| Kenosha | Kanosha |
| Oshkosh | Oshcosh |
| Appleton | (rarely misspelled) |
| Green Bay | (rarely misspelled) |

## Political Figures (Current/Recent)

| Correct | Role | Common Misspellings |
|---------|------|-------------------|
| Janet Protasiewicz | Supreme Court Justice | Protasewicz, Protasavich |
| Brian Hagedorn | Former Supreme Court Justice | Hagadorn, Hagedoorn |
| Michael Gableman | Former Supreme Court Justice | Gabbleman, Gabellman |
| Jim Troupis | Attorney | Troupes, Troopis |
| Brad Schimel | Former Atty. Gen. | Schimmel, Shimel |
| Jill Karofsky | Supreme Court Justice | Karovsky, Karofski |
| Rebecca Dallet | Supreme Court Justice | Dallett, Dalet |
| David Prosser | Former Supreme Court Justice | Prossar |
| Tony Evers | Governor | (rarely misspelled) |
| Robin Vos | Assembly Speaker | (rarely misspelled) |
| Josh Kaul | Attorney General | Kohl, Call |
| Dan Kelly | Former Supreme Court Justice | (rarely misspelled) |
| Eric Toney | Politician | Tony, Toni |
| Sean Duffy | Former U.S. Rep. / Secretary of Transportation | Shawn Duffy |

## Legal Cases

| Correct | Common Misspellings |
|---------|-------------------|
| Kaul v. Urmanski | Kohl v. Urmanski, Call vs Urmanski |
| Clarke v. WEC | Clark v. WEC |
| Trump v. Biden (WI) | (rarely misspelled) |

## Institutions

| Correct | Abbreviation | Notes |
|---------|-------------|-------|
| Wisconsin Public Radio | WPR | |
| PBS Wisconsin | | Formerly WPT/Wisconsin Public Television |
| UW-Madison | | Not "University of Wisconsin Madison" in running text |
| Marquette Law School | | Often "Marquette poll" |
| Wisconsin Elections Commission | WEC | |
| Department of Justice | DOJ | Wisconsin state DOJ, not federal |

## PBS Wisconsin Programs & Hosts

| Program | Host/Anchor | Regular Panelists |
|---------|------------|-------------------|
| Inside Wisconsin Politics | Shawn Johnson | Zac Schultz, Rich Kremer, Anya van Wagtendonk |
| Here & Now | Frederica Freyberg | |
| Wisconsin Life | | (various segment producers) |
| University Place | | (various lecturers) |

## Wisconsin-Specific Terms

| Term | Notes |
|------|-------|
| Capitol | The building/district in Madison (not "capital" unless referring to money) |
| Act 10 | 2011 law restricting public employee unions |
| Tavern League | Tavern League of Wisconsin (lobbying group) |
| Dells | Wisconsin Dells (tourism area) |
| Up North | Colloquial for northern Wisconsin |
| FIBs | Colloquial for Illinois visitors (use cautiously) |

## Editor Corrections

Names and terms corrected during human editorial review. These represent cases where the model consistently gets the wrong spelling or the caption source is unreliable.

| Correct | Model Tendency | Context |
|---------|---------------|---------|
| Sean Duffy | Shawn Duffy | Former WI congressman; model confuses with IWP host Shawn Johnson |
| Josh Kaul | Josh Gold | WI Attorney General; 6HNP2511 sign-off - named on mic only once |
| Attorney General Kaul | Attorney General Call | 6HNP2511 open. Keep key as full phrase - a bare Call rewrites the verb |
| Josh Kaul | Josh Kahl | 6HNP2511 Toney interview. Third distinct misrender of the same name - see whisper-ops FINDINGS-2026-09-09-toney |

## Name Disambiguation

Names that appear in PBS Wisconsin transcripts where the model may confuse similar-sounding or similar-looking names. Pay special attention when both names could plausibly appear.

| Name | Role | Do NOT confuse with |
|------|------|-------------------|
| Shawn Johnson | IWP host (PBS Wisconsin) | Sean Duffy (politician) |
| Sean Duffy | Former U.S. Rep. / Sec. of Transportation | Shawn Johnson (IWP host) |
| Anya van Wagtendonk | IWP panelist | — |
