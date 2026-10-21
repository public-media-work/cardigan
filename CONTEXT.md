# Cardigan

Cardigan turns public-media caption files into metadata and copy for publishing platforms, splitting what a script can produce from what needs inference.

## Language

### Inputs

**Caption file**:
A timed transcript of a program, such as SRT or SCC, and the canonical input to a job.
_Avoid_: transcript file, script, `.txt` input

**SST record**:
The program's row in the AirTable Single Source of Truth table, holding its current published metadata.
_Avoid_: AirTable record, metadata record

### The job

**Job**:
One run of Cardigan over one caption file, producing a set of items.
_Avoid_: task, run, phase run

**Context bundle**:
The shared material assembled once per job that every generator reads from, some of it deterministic and some inferred.
_Avoid_: analyst output, brainstorming document, context

**Generator**:
The unit of work that produces exactly one item from a declared slice of the context bundle.
_Avoid_: phase, agent, step

**Deterministic**:
Produced by a script from the caption file and other inputs, with no model involved.
_Avoid_: automatic, rule-based

**Inferred**:
Produced by a language model.
_Avoid_: AI-generated, generated

### Output and review

**Item**:
One reviewable output of a job, such as a keyword list or a formatted transcript, carrying its proposed value and where it came from.
_Avoid_: deliverable, output document, field

**Suggestion**:
An inferred copy item presented as a proposed change that builds on existing text, never as a replacement for it.
_Avoid_: draft, generated copy

**Job page**:
The single page where every item of a job is reviewed, hand-edited, and pushed.
_Avoid_: job detail, review screen, approval surface

**Push**:
Writing one item's value to one field of a target system on a single human action.
_Avoid_: publish, commit, sync
