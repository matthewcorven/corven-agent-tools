# From reference material to a visual decision

This is a reproducible authoring procedure with inspectable intermediate artifacts and concise
decision rationales, not a reconstruction of private reasoning. Its purpose is to let another
agent independently achieve the same quality on a different subject.

## 1. Build an evidence map, not a summary pile

Inventory before writing. Read each in-scope source; headings and search hits are not a full survey.
If source volume exceeds one context window, process bounded batches and retain a compact ledger.
Keep that ledger in session state or the requested proposal appendix, not unrelated planning files.

| Field | What to capture |
| --- | --- |
| Source | Path, section, revision/hash; distinguish committed from working-copy text |
| User need | Human, job, desired result, acceptance signal |
| Authority | Who can decide; binding policy vs aspiration vs example |
| Evidence | Implemented/tested, accepted but unbuilt, proposed, unknown or contradicted |
| Constraint | Safety/privacy/cost/time/integration limit and its rationale |
| Release relevance | Needed for first outcome, enables a later outcome, or unresolved |
| Diagram destination | Slide ID or a deliberate omission with explanation |

Cross-check contradictions explicitly. A PRD may promise something that a validation ledger calls
unbuilt; a later ADR may narrow an earlier design. Preserve both the history and current authority.
Never equate a documented component, class stub or passing unrelated test with working capability.

## 2. Choose a value slice before an architecture

Write one sentence: **For [human], deliver [observable outcome], within [bounds], with [evidence].**
Then compare 2-3 genuine options on the same dimensions: user value, integration seams, uncertainty,
mandatory safety, time to evidence and reversible deferrals. Avoid fabricated numerical estimates.

A useful simplification removes unused breadth, not necessary controls. Ask:

- Can one manual invocation prove the outcome before scheduling and general orchestration?
- Can a deterministic parser/rule do the job before introducing a model?
- Can modules share a process before becoming separately deployed services?
- What can be deferred without breaking the chosen end-to-end outcome or accepted constraints?
- What proposal requires a human policy amendment rather than a technical shortcut?

Finish with a recommendation and its cost/limitation. "Recommended" and "accepted" are different states.

## 3. Turn obligations into diagram questions

| Reader question | Diagram | Required content |
| --- | --- | --- |
| Why this release? | Outcome flow / option columns | Human job, visible result, non-goals, real alternatives |
| What does "done" mean? | PRD outcome chain | Input, result, evidence, bounds and failure/no-op |
| What is outside vs inside? | L0 context (Mermaid flow or PlantUML) | Humans, system boundary, external systems, labeled data/control edges |
| What owns execution and truth? | Component/store view | In-process modules vs deployables, authoritative store vs projection, owners |
| What does each person/role do? | PlantUML use case / role flow | Primary actor, secondary actors, individual responsibilities, collective handoff |
| What changes state? | Mermaid state machine | Initial/terminal states, every applicable trigger, guards, outputs, rejected edges |
| How is an admitted step decided? | Flowchart | Ordered deterministic gates, short-circuit failure, recording and side effect boundary |
| Where does a human talk to a system? | Sequence / swimlane | User intent, UI, orchestrator, policy, storage, adapter, acknowledgement |
| How does change happen safely? | Version/change sequence + state view | Prepare, quiesce/drain or cancel, validate, approve exact candidate, activate, resume |
| What survives failure? | Recovery sequence / policy flow | Timeout, unknown outcome, restart, reconciliation and human decision |
| What can we deliver first? | Dependency flow | Thin vertical slice, proof gates, deferred breadth |
| What still needs choosing? | Decision table | ID, question, options, recommendation, status, owner and impacted slides |

Choose notation for the question, not variety. Use PlantUML for actor/use-case boundaries or familiar UML;
Mermaid for compact flows, state and sequence diagrams. Do not draw the same relationship twice just to
include both languages. L0 stays coarse; put inner gates on a separate slide.

## 4. Close state and seam coverage

Maintain a trigger ledger; the diagram is its readable projection:

`trigger -> source actor/system -> current state -> guard -> next state -> output -> evidence -> failure`

Consider direct **and indirect** triggers: command, denial, scheduled tick, queue admission, dependency
completion, approval expiry, version mismatch, retry exhaustion, budget/capacity changes, external callback,
timeout, cancellation, storage failure, process crash/restart and revocation. Include those that apply;
do not manufacture policies for those that do not.

For each state, inspect incoming and outgoing transitions. Name invalid transitions and the observable
rejection. For every side effect, show authorization, outcome recording, and what happens if its outcome
is unknown. Do not label "retry" safe without an idempotency/reconciliation contract.

For every human seam, show **both directions**: request, feedback, decision, receipt, failure and next
available action. Include operation, approval, change, incident/recovery and team handoffs, not just
the happy path. A bidirectional arrow without payload labels is not a communication contract.

If comprehensive edges produce an unreadable graph, split it into overview + transition families +
trigger table, retaining stable IDs across views. Completeness belongs to the coverage model; no single
slide has to hold all of it.

## 5. Edit each slide as an argument

Draft the **reader question**, **claim**, **diagram**, **one-sentence caption**, **evidence/status line**.
The heading should state the takeaway ("One process owns the effect"), not merely "Architecture".
Use 3-7 words per node as a starting budget and roughly 5-9 major nodes per view; these are readability
heuristics, not correctness limits. Split on authority, time or abstraction boundaries when dense.

Put nouns in boxes, verbs/payloads on edges, guards on decisions. Keep labels and actor names consistent
across slides. Use solid vs dashed lines with textual legends; do not rely on color alone. Prefer a
quiet palette, system fonts, generous whitespace, direct labels and one dominant visual.

Remove text that duplicates the diagram. Keep text that disambiguates authority, evidence, an exception
or a consequential tradeoff. Put references and detailed inventories in a brief appendix. Do not turn
paragraphs into boxes and call the result visual communication.

## 6. Run a decision conversation

Build the opening early. For each decision, render options and consequences, set its stable focus ID,
verify the served revision, then ask **one** matching question through the available question tool.
Record the actual answer, its scope and impacted slide IDs; update affected views before moving on.

If unavailable, leave status `unaccepted` with a provisional recommendation and explicit assumption.
Do not treat praise for the presentation, selecting a slide, or "keep going" as approval of its policies.
Later answers can change the proposed scope; accepted baseline documents remain unchanged unless the
human separately authorizes that change.

## Finish criteria

Every requested view is present or explicitly inapplicable. Every binding constraint is retained or
flagged as a proposed amendment. Every claim has evidence/status. Every decision is accepted by the
appropriate human or remains open. Every diagram renders and can be read. The saved source, HTML and
validation report refer to the same revision. Rendering evidence is never product acceptance.
