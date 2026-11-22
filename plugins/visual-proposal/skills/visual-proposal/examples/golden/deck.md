---
marp: true
theme: proposal
paginate: false
---

<!-- slide: opening -->
<!-- _class: title -->
# A useful draft.<br>A smaller first release.

**Release-note review, before automatic publishing.**

A visual PRD and level-zero design.<br>Fictional example. Decisions remain open.

<p class="refs">Golden example - adapt the method, not the domain rules</p>

---

<!-- slide: decision-outcome -->
<!-- _class: decision -->
## What should the first release earn?

<div class="columns">
<div class="option"><h3>A. Reviewable draft</h3><p>One selected release, a sourced draft, and a visible result.</p><span class="badge">Recommended pilot</span></div>
<div class="option"><h3>B. Scheduled digest</h3><p>Repeated summaries across sources. Adds timing and identity rules.</p></div>
<div class="option"><h3>C. Publishing service</h3><p>Approved content reaches channels. Adds external-effect recovery.</p></div>
</div>

<p class="caption">Choose the outcome before the orchestration. A postpones scheduling and publication; it does not prove they are unnecessary.</p>
<p class="refs">D1 UNACCEPTED - recommendation only - brief R1/R2</p>

---

<!-- slide: prd -->
## The product is a result the owner can verify

```mermaid
%% title: Product contract links input to observable evidence
flowchart LR
  I["Selected changes"] --> D["Sourced draft"]
  D --> V{"Every claim<br>has evidence?"}
  V -- yes --> R["Reviewable result<br>+ run receipt"]
  V -- no --> B["Blocked<br>missing evidence"]
  R --> H(["Owner review"])
```

<p class="caption">Success is a useful draft with traceable claims, not merely a completed model call.</p>
<p class="refs">Proposed PRD - brief R1/R3 - source identity and limits unresolved</p>

---

<!-- slide: scope -->
## Remove breadth, not the evidence boundary

```mermaid
%% title: First-release scope separates retained controls from deferred breadth
flowchart LR
  P["Proposed pilot"] --> M["One manual request"]
  P --> S["One source adapter"]
  P --> E["Evidence + access checks"]
  L["Later, by decision"] -.-> T["Scheduling"]
  L -.-> U["Multiple sources"]
  L -.-> X["External publishing"]
```

<p class="caption">Solid = proposed first release. Dashed = deferred, not deleted from the reference.</p>
<p class="refs">D1 unaccepted - brief R2/R3 - no existing policy is amended by this diagram</p>

---

<!-- slide: context -->
## Level zero: keep ownership visible

```plantuml
@startuml
' title: System context separates the human, local system and external source
!pragma layout smetana
left to right direction
skinparam defaultFontName Arial
skinparam defaultFontSize 22
skinparam backgroundColor #F5F7F8
skinparam shadowing false
actor "Release owner" as Owner
rectangle "Review workspace\nproposed system boundary" as System
rectangle "Source repository\nexternal read-only system" as Source
database "Run records\nlocal evidence store" as Store
Owner --> System : select / request / cancel
System --> Owner : draft / receipt / blocker
System --> Source : read selected changes
Source --> System : items / failures
System --> Store : record outcome
Store --> System : recover / inspect
@enduml
```

<p class="refs">L0 proposal, not implementation - brief R1/R3/R4 - no publishing edge</p>

---

<!-- slide: services-stores -->
## Modules do not need to become microservices

```mermaid
%% title: In-process modules separate validation, drafting, adapters and records
flowchart LR
  U["Local UI"] --> G["Admission checks"]
  subgraph Process["One proposed process"]
    G --> R["Run controller"]
    R --> D["Drafting step"]
    R --> A["Read-only adapter"]
    R --> S[("Run store<br>authoritative outcome")]
    D --> V["Evidence validator"]
    V --> S
  end
  S --> P["UI projection<br>not authority"]
```

<p class="caption">Add deployment boundaries only when scale or isolation earns their operational cost.</p>
<p class="refs">Proposed placement - brief R1/R3 - store durability and access contract still to decide</p>

---

<!-- slide: owner-usecases -->
## The owner gets control as well as output

```plantuml
@startuml
' title: Primary and secondary owner use cases
!pragma layout smetana
left to right direction
skinparam defaultFontName Arial
skinparam defaultFontSize 22
skinparam backgroundColor #F5F7F8
skinparam shadowing false
actor Owner
rectangle "Review workspace" {
  usecase "Request a draft" as Request
  usecase "Inspect claims\nand run receipt" as Inspect
  usecase "Cancel or triage\na stalled run" as Recover
  usecase "Review a template\ncandidate" as Change
}
Owner --> Request
Owner --> Inspect
Owner --> Recover
Owner --> Change
@enduml
```

<p class="refs">Primary outcome + secondary control paths - brief R1/R4/R6</p>

---

<!-- slide: team -->
## Each role has a responsibility, not implied authority

```mermaid
%% title: Individual team responsibilities combine into a reviewed candidate
flowchart LR
  O(["Owner"]) -- outcome --> L["Lead<br>scope + constraints"]
  L -- bounded task --> B["Builder<br>candidate + evidence"]
  B -- review request --> R["Reviewer<br>check + challenge"]
  R -- rejection --> B
  R -- recommendation --> O
  O -- exact decision --> A["Activation boundary"]
```

<p class="caption">Collectively: propose, implement, challenge, decide. A review recommendation is not an activation command.</p>
<p class="refs">Illustrative roles - brief R5 - use real charters and per-member diagrams when the target needs more detail</p>

---

<!-- slide: lifecycle -->
## A timeout is uncertainty, not success

```mermaid
%% title: Run lifecycle includes rejection, cancellation, timeout and recovery
stateDiagram-v2
  direction LR
  [*] --> Idle
  Idle --> Checking: request
  Checking --> Blocked: invalid / denied
  Checking --> Running: valid + admitted
  Running --> Done: validated + recorded
  Running --> Stopping: cancel / revoke
  Stopping --> Blocked: settled / cancelled
  Running --> Unknown: timeout / crash
  Stopping --> Unknown: outcome unclear
  Unknown --> Blocked: reconcile + triage
  Done --> Idle: new request
  Blocked --> Idle: corrected request
```

<p class="caption">Reject a second request while active. Restart reconstructs from records; unknown work is never retried blindly.</p>
<p class="refs">Proposed transition contract - brief R4 - detailed triggers in appendix</p>

---

<!-- slide: gates -->
## Exact rules stay outside the drafting step

```mermaid
%% title: Deterministic flow gates work and verifies output before completion
flowchart LR
  I["Request"] --> A{"Source + access<br>+ limits valid?"}
  A -- no --> B["Block + explain"]
  A -- yes --> R["Record admission"]
  R --> D["Read + draft"]
  D --> V{"Claims grounded<br>+ receipt saved?"}
  V -- no --> B
  V -- yes --> O["Return result"]
```

<p class="caption">A model may propose wording. It does not decide access, numeric limits or whether missing evidence is acceptable.</p>
<p class="refs">Brief R3/R7 - recording failure prevents the next step; admission failure performs no source read</p>

---

<!-- slide: human-seams -->
## Every handoff returns an observable answer

```mermaid
%% title: Human-to-system sequence includes success and refusal feedback
sequenceDiagram
  actor Owner
  participant UI
  participant Run
  participant Source
  participant Records
  Owner->>UI: Request selected release
  UI->>Run: Submit selection
  Run->>Records: Record validated admission
  alt Admission cannot be recorded
    Run-->>UI: Blocker, no source read
  else Admitted
    Run->>Source: Read selected changes
    Source-->>Run: Items or failure
    Run->>Records: Store draft evidence or blocker
    Run-->>UI: Result or explicit uncertainty
  end
  UI-->>Owner: Receipt and next available action
```

<p class="refs">Operation seam - brief R1/R3/R4 - no hidden publication or human approval inferred</p>

---

<!-- slide: change-at-rest -->
## Change the next run, not the one already admitted

```mermaid
%% title: Version change handshake prepares an exact candidate and waits for rest
sequenceDiagram
  actor Owner
  participant Builder
  participant Runtime
  participant Reviewer
  Owner->>Builder: Request improvement
  Builder->>Reviewer: Candidate + fixtures
  Reviewer-->>Owner: Evidence + recommendation
  Owner->>Runtime: Quiesce affected work
  Runtime-->>Owner: Drained or blocked
  Owner->>Runtime: Submit exact-version approval
```

<p class="caption">Urgent restrictions apply immediately. Routine template changes wait for a known boundary.</p>
<p class="refs">D2 UNACCEPTED - illustrative at-rest policy - brief R6</p>

---

<!-- slide: activation -->
## Approval is input; current checks decide activation

```mermaid
%% title: Activation checks current candidate identity and settled work before a version switch
flowchart LR
  A["Exact approval"] --> V{"Candidate<br>still matches?"}
  V -- no --> R["Reject + explain<br>keep old version"]
  V -- yes --> I{"Idle + constraints<br>still valid?"}
  I -- no --> R
  I -- yes --> S["Record activation"]
  S --> N["Next run<br>uses new version"]
```

<p class="caption">An old approval cannot activate new bytes. A record failure leaves activation blocked, not partially successful.</p>
<p class="refs">D2 unaccepted - brief R6 - complements the human handshake rather than hiding gates in a long sequence</p>

---

<!-- slide: recovery -->
## Recovery starts with evidence, not repetition

```mermaid
%% title: Incident and restart communication exposes uncertainty to the owner
sequenceDiagram
  participant Runtime
  participant Records
  participant UI
  actor Owner
  Runtime->>Records: Recover admitted run after restart
  Records-->>Runtime: Incomplete outcome
  Runtime-->>UI: Unknown, no automatic repeat
  UI-->>Owner: Last known step + available evidence
  Owner->>UI: Inspect and choose next action
  UI->>Runtime: Explicit triage decision
  Runtime-->>Owner: Recorded resolution or remaining blocker
```

<p class="refs">Incident seam - brief R4 - a new request is distinct from claiming the old one completed</p>

---

<!-- slide: policy -->
## Data policy is a release prerequisite

```mermaid
%% title: Data path includes unresolved retention and access boundaries
flowchart LR
  S["Approved source"] --> A["Access check"]
  A --> D["Draft + references"]
  D --> R[("Run record")]
  R --> C["Current read policy"]
  C --> O(["Owner view"])
  P["Before real data:<br>retention + redaction<br>+ accepted identities"] -. constrains .-> A
  P -. constrains .-> R
  P -. constrains .-> C
```

<p class="caption">Do not inherit privacy rules from a sample. Resolve the real project's obligations before admitting real data.</p>
<p class="refs">Open release gate - brief R3/R7 - dashed edges denote requirements awaiting decisions</p>

---

<!-- slide: delivery -->
## Build the smallest complete evidence loop

```mermaid
%% title: Delivery order proves the vertical slice before adding automation
flowchart LR
  F["Agree fixtures<br>+ source/data rules"] --> M["Manual path<br>+ visible receipt"]
  M --> E["Failure + cancel<br>+ restart evidence"]
  E --> V["Version switch<br>at known rest"]
  V --> H(["Owner evaluates value"])
  H -. separate decision .-> X["Schedule / expand"]
```

<p class="caption">No invented dates. Each step exits on observable evidence; later breadth needs a new value decision.</p>
<p class="refs">Delivery recommendation only - brief R1-R7 - all product functionality remains unbuilt</p>

---

<!-- slide: trigger-catalog -->
<!-- _class: appendix -->
## Check the less visible edges

| Trigger | Guard / next state | Observable output |
| --- | --- | --- |
| Invalid source, denied access, limit reached | Checking to Blocked | Reason, no read |
| Record write fails | No next step; reconcile admitted work | Error / unknown, never success |
| Cancel or revoke | Running to Stopping | Admission closed, settle or unknown |
| Timeout, crash, restart | Unsettled run to Unknown | Last evidence, no blind repeat |
| Candidate changes after approval | Reject activation | Old version remains |
| Corrected request / new run | Only after known settlement | New identity and admission |

<p class="refs">Brief R3/R4/R6/R7 - expiry, schedule and external callbacks are outside this pilot, not assumed implemented</p>

---

<!-- slide: decisions-evidence -->
<!-- _class: appendix -->
## Keep decisions separate from rendering evidence

| Item | Status | What settles it |
| --- | --- | --- |
| D1: manual draft first | Unaccepted recommendation | Owner chooses outcome |
| D2: exact version at rest | Unaccepted recommendation | Owner accepts change policy |
| Source, limits, data policy | Open release prerequisites | Explicit contract + fixtures |
| Product behavior | Unbuilt | Implementation and behavioral evidence |
| Presentation | Locally testable | Build + Playwright + visual inspection |

<p class="caption">Source coverage: brief R1-R7. This fictional deck demonstrates a method; it is not permission to build or publish.</p>
<p class="refs">Rendering tests prove the artifact, never the product</p>
