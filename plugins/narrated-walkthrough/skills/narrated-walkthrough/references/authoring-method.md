# Authoring method

How to get from "explain this" to a plan the builder can render. Read this before
writing `timeline.json`; read [`rendering.md`](rendering.md) before you change the
deck's HTML or CSS.

## 1. Pick the shape of the answer

Four shapes recur. They differ in what the final scene has to do, and that
difference is the whole reason the disposition contract exists.

| Shape | When | Final scene |
|---|---|---|
| **Answer** | A question with a settled answer | restate the answer, then stop |
| **Findings** | Evidence overturned or confirmed assumptions | the corrected ranking |
| **Briefing** | Status of work in flight | where it sits, what is next |
| **Decision** | The listener must choose | the options, an explicit invitation |

Only the last two usually carry `disposition`. A findings deck that ends on
"here is what is still unknown" is a decision deck in disguise — treat it as one.

## 2. Budget the scenes

- 6–9 scenes for a two-minute walkthrough; 10–14 for four minutes.
- A scene holds one claim and 15–25 seconds of speech. Two ideas means two scenes.
- The first scene earns attention: name the question, promise the turn.
- The second-to-last scene states the disposition honestly. The last scene offers
  the choice.

Estimate before you synthesize: at ~2.6 words per second, a 20-second scene is
about 52 words.

## 3. Write narration for the ear

- One clause per comma. Split at every "and then".
- Contractions, second person, present tense.
- Numbers spoken the way a person says them: "three and a half times", not "3.5×".
- No identifiers, file paths, function names, or version strings.
- Name the actor: "the test showed", not "it was shown".
- The last sentence of the last scene is a question or an invitation, never a
  sign-off.

Anti-pattern: narration that reads like a changelog. "Then we updated X, then we
updated Y" is a list; a walkthrough needs a turn.

## 4. Write captions as the same sentences

Captions are the narration, chunked for reading — not a summary of it. Keep them
within roughly 12% of the spoken length so they do not race the voice. The
builder distributes them evenly across the measured clip, so a three-caption
scene over 18 seconds holds each for six.

## 5. Diagram contracts

| Scene kind | Diagram | Rule |
|---|---|---|
| Contrast | two-column flowchart | label both sides with the claim |
| Sequence | left-to-right flowchart | at most five nodes |
| Scoreboard | grouped subgraphs | one group per verdict, colour-coded |
| Before/after | two subgraphs with dotted edges | show the *move*, not just both states |
| Decision | top-down flowchart from one question | options are leaves, no nesting |

Colour is a contract, not decoration: green = keep or supported, amber = open or
cheap, red = retired or wrong, grey = inert or parked. Use the same four across
every scene so the listener learns them once.

## 6. Disposition honesty

Before writing the last two scenes, answer three questions on paper:

1. What is decided, and by whom?
2. What is still unknown, and what test would settle it?
3. What would the listener plausibly say back?

If (3) has an answer, the deck is a decision deck and must invite it. If (3) is
"nothing", say so plainly and end without a question — an invitation nobody can
act on is worse than silence.

Unknowns and choices are different and should be named separately: an unknown is
settled by evidence, a choice is settled by preference. Mixing them lets a
preference masquerade as a fact-finding task.

## 7. Verify before delivering

Run `--validate`, then build, then drive the page through the browser surface:
seek, play, cross a boundary, confirm the ask panel. Then ask the human about the
voice — that is the one thing you cannot check yourself.
