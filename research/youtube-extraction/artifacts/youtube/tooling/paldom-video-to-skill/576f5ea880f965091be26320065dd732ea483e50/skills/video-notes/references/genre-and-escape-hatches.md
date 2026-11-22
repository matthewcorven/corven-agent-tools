# Genre routing and escape hatches

Reference for `video-notes`. Read when deciding how much visual work a video
deserves, or when frames plus transcript are demonstrably not enough.

**Contents:** [Genre routing](#genre-routing) · [Long videos](#long-videos) ·
[When frames are not enough](#when-frames-and-transcript-are-not-enough) ·
[Jargon fusion](#ocr-driven-asr-hotwords) · [Anti-hallucination](#anti-hallucination-prompting)

## Genre routing

The single biggest cost decision. Transcript-only processing fits interviews and
podcasts; visually instructional video requires looking.

| Genre | Tell | Frames | Emphasis in the note |
| --- | --- | --- | --- |
| Screencast / CLI | terminal, editor | yes | verbatim commands, file paths, error text |
| Slides / talk | deck, diagrams | yes | the slide claims; quote the speaker against them |
| GUI walkthrough | app chrome, cursor | yes | explicit navigation paths (`Settings > API key`) |
| Physical / lab demo | hands, hardware | yes | ordered actions and their success signals |
| Podcast / interview | two faces, no artefacts | **no** | argument structure, disagreements, quotes |

A transcript signal worth trusting: if nobody says "as you can see", "here",
"this line", "notice that", the visual channel is probably carrying nothing.

Do not spend vision on speech-only material. Say in the note that there are no
figures and why — an unexplained absence reads as an omission.

## Long videos

Prefer whole-transcript reasoning: a 60-minute talk is roughly 9–10k words,
about 13k tokens, which fits comfortably. Chunk only past roughly 150k tokens,
and when you do, split on chapter boundaries and prepend a document-level
summary to each chunk so the big picture survives. Models recall the beginning
and end of a context better than the middle: put the transcript in a labelled
block first, then the instruction.

## When frames and transcript are not enough

Sampled frames plus ASR miss **motion that is itself the content**: an animated
derivation, a value changing mid-transition, a chart being drawn. A keyframe
catches the start and the end, not the becoming.

The escape hatch is a long-context multimodal model that ingests the video
directly — Gemini can read a public YouTube URL natively, and reportedly
recovers formulas and arithmetic that only exist mid-animation.

It is deliberately **not** the default here:

- it needs an API key and sends the video to a third party;
- its timestamps are coarse and can be fabricated, which breaks the deep-link
  contract this skill is built on;
- it does not produce word-exact quotes;
- URL ingestion is provider-specific.

Use it only when the user asks for it, or when you have concluded from the
transcript and frames that the substance is animated. If used, keep the
timestamped transcript as the citation source and treat the model's output as
commentary — never as the provenance record.

## OCR-driven ASR hotwords

For jargon-heavy technical video with **no captions**, on-screen text can repair
the transcript. Extract recurring terms from the OCR (slide titles, acronyms,
identifiers), check which never appear in the ASR output — those are the likely
mis-hearings — and re-transcribe passing them as hotwords / `initial_prompt`.

Worth it only when captions are absent *and* the video is slide- or code-heavy.
It costs a second ASR pass, so make it a deliberate choice, not a default.

## Anti-hallucination prompting

When writing the note, the guards that measurably help:

1. base the analysis solely on the supplied transcript and frames — do not
   import outside knowledge about the speaker or topic;
2. cite only timestamps that exist in the source data;
3. flag uncertainty rather than resolve it;
4. quote verbatim in the Quotes section, paraphrase everywhere else and say so.

The failure this prevents is the plausible-sounding note: fluent, well
structured, and citing a timestamp where nothing of the kind was said. A wrong
deep link is worse than no link, because it looks checkable.
