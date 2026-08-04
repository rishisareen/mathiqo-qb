# Structured figures — review packet

**To:** partner engineering
**Re:** your `fetch_batch` figure-support request
**Status:** live. A bank carrying figures is built and active — `fetch_batch` returns real `figures` data today, not a preview.
**Asks of you:** confirm two deviations from your proposal, and tell us if our proposed side-index convention works for your renderer.

You reported that every field in the batch response was text or choice-based, so Strand D and E skills could not be routed to us without re-authoring each visual yourselves. That is fixed. `figures` now ships on every item instance, materialized offline behind the same verification gate as the answer.

Full field reference is in `docs/partner-integration-guide.md`. This packet covers only what needs your judgement.

## What we built, in one paragraph

Twelve kinds — `bar-graph`, `pictograph`, `tally-chart`, `number-line`, `angle`, `circle`, `point-line`, `shape`, `region`, `array`, `grid-region`, `ruler` — cover 78 templates. That is more than the 55 we first quoted you: auditing the corpus found measurement had been systematically mis-annotated, so every area, perimeter and ruler-reading item was marked text-only despite needing a scale or a labelled shape. Your report surfaced a bigger gap than either of us thought.

## Two deviations from your proposal — please confirm or object

**1. `figures` is an array, not a single optional object.**

You proposed `figure?: Figure | null`. Five items genuinely show two distinct figures: three compare two angles, and two draw a square alongside a regular n-gon whose symmetry counts get summed. A composite kind would have worked, but an array is simpler for you to loop over.

Repetition of *one* shape does **not** use the array — an item showing `m` tilted right angles carries a count inside the `angle` kind, so array length stays fixed per template and only varies when the figures genuinely differ.

**2. Non-integer quantities are exact rational strings, not JSON numbers.**

```json
{ "kind": "number-line", "min": 0, "max": 2, "divisions": 3,
  "points": [ { "position": "5/3" } ] }
```

`position` is `"5/3"`, not `1.667`. This is the convention our `answer` field already uses, and it is deliberate: a number line divided into thirds cannot be represented in binary floating point, and a child marking 5/3 must not be graded against 1.6666666666666667. Integers stay plain JSON integers; only non-integers become strings.

**This is the deviation most likely to break you silently.** If your renderer assumed numbers, arithmetic on a position will produce `NaN` or string concatenation rather than an error. Worth checking before you build.

## Side indices — we picked a convention; tell us if it doesn't fit

The first draft of this packet flagged that side indices were undefined and asked what you wanted. We have since pinned one rather than leave `shape` figures unrenderable while we waited:

**Side `i` runs from vertex `i` to vertex `i+1` (wrapping), walking the outline counter-clockwise from `vertex_labels[0]` when given, or from the shape's bottom-left vertex in its default upright orientation when not.** Every currently-shipped `side_labels` array already conforms to this — it is what the content was authored against, not a change to existing data.

`composite-l` is the one shape whose six sides are not a single labelled loop: sides `0`–`1` are the lower section's two given sides, `2` is the derived step between sections, `3`–`4` are the upper section's two given sides, `5` is the derived outer closing edge. `2` and `5` are never labelled.

```json
{
  "kind": "shape", "shape": "composite-l",
  "side_labels": [
    { "side": 0, "value": 3, "unit": "m" },
    { "side": 1, "value": 3, "unit": "m" },
    { "side": 3, "value": 6, "unit": "m" },
    { "side": 4, "value": 5, "unit": "m" }
  ]
}
```

**Say so if this collides with something on your side** — a different winding direction, a different origin, or a renderer that already assumes a convention of its own. We would rather change it now than have you build against it and find out later.

## Four gaps from the first draft — closed, not just noted

The first draft of this packet listed these as open questions. We closed all four rather than wait, since none required your input to resolve on our side:

1. **Circle centre.** `circle` gained `centre_marked: true` — draws and labels the centre point O independent of `construction`. The point-classification item that was the reason for this gap now marks O explicitly, matching its stem's own "besides the centre" framing.

2. **Shared vertex.** `angle` gained `shared_vertex`, a string tag: two entries in the same `figures` array carrying the same tag meet at one point rather than floating independently. The two comparison items that needed this now carry it.

3. **Reflex-boundary angles.** `context: "bare"` now accepts any measure up to (not including) 360°, not just ≤180°. The three arm-length and turn-comparison items that previously borrowed the clock face just to express a turn over 180° now use `bare` where the stem has no clock framing — a bare angle carries `arm_length` freely, where a numbered clock face's fixed-length hands would have undercut the exact contrast those items test. `context: "protractor"` stayed capped at 180°, since a single protractor scale genuinely can't show more.

4. **Altitude and caption.** `shape` gained `altitude` (with `altitude_unit`) for a height that is not itself a side, and `caption` to distinguish figures in a multi-shape array. The triangle-in-rectangle item now draws its stated height; the same-perimeter-different-area item now captions its two rectangles "Rectangle A" / "Rectangle B" instead of relying on array order alone.

## Worked examples

Copied verbatim from a real bank build, one instance per template — not hand-written illustrations. Values vary by variant; the shapes do not.

**Reflex angle** — measure exceeds 180°, so the renderer must draw the major arc:
```json
[ { "kind": "angle", "degrees": 308, "context": "reflex" } ]
```

**Clock context** — a numbered hour-marked face, not a bare angle:
```json
[ { "kind": "angle", "degrees": 30, "context": "clock" } ]
```

**Protractor dual-scale** — both scales legible, `scale` naming the one the item reads:
```json
[ { "kind": "angle", "degrees": 121, "context": "protractor", "scale": "inner" } ]
```

**Bare angle above 180°, with a shared vertex** — the arm-length item that used to borrow the clock face:
```json
[
  { "kind": "angle", "degrees": 120, "context": "bare", "arm_length": 3, "shared_vertex": "O" },
  { "kind": "angle", "degrees": 60,  "context": "bare", "arm_length": 1, "shared_vertex": "O" }
]
```

**Circle with its centre marked:**
```json
[ { "kind": "circle", "points_on": 2, "points_off": 5, "centre_marked": true } ]
```

**Half-symbol pictograph** — trailing half symbol worth half the key; the key is constrained even so the half stays a whole number:
```json
[ { "kind": "pictograph", "key": 2, "unit": "mangoes",
    "rows": [ { "label": "Mangoes", "whole": 3, "half": true } ] } ]
```

**Two-figure comparison** — the array carrying genuinely distinct figures:
```json
[ { "kind": "shape", "shape": "square", "symmetry": "none" },
  { "kind": "shape", "shape": "regular-ngon", "sides": 6, "symmetry": "none" } ]
```

**Triangle with a labelled altitude:**
```json
[ { "kind": "shape", "shape": "triangle",
    "side_labels": [ { "side": 0, "value": 4, "unit": "cm" } ],
    "altitude": 3, "altitude_unit": "cm" } ]
```

**Captioned pair, disambiguated without relying on array order:**
```json
[
  { "kind": "shape", "shape": "rectangle", "caption": "Rectangle A",
    "side_labels": [ { "side": 0, "value": 6, "unit": "cm" }, { "side": 1, "value": 2, "unit": "cm" } ] },
  { "kind": "shape", "shape": "rectangle", "caption": "Rectangle B",
    "side_labels": [ { "side": 0, "value": 7, "unit": "cm" }, { "side": 1, "value": 1, "unit": "cm" } ] }
]
```

**Rational position, and the unlabelled point** — `"1/2"` is a string; the point carries no label because its position is the answer:
```json
[ { "kind": "number-line", "min": 0, "max": 1, "divisions": 4,
    "points": [ { "position": "1/2" } ] } ]
```

**Negative position on a zero-centred line** — integers stay integers, and this point *is* labelled, because "P" names it without giving the answer away:
```json
[ { "kind": "number-line", "min": -2, "max": 2, "divisions": 1,
    "points": [ { "position": -1, "label": "P" } ] } ]
```

## Two things that will look like bugs but are not

**Some figures deliberately omit a label.** Where the answer is the marked value — "what fraction is marked here?" — the point carries no label. Our build refuses any figure whose label renders to that variant's answer, so this is enforced, not incidental. Do not add a value label in your renderer for those.

**Some figures deliberately omit information.** Symmetry items draw the shape and nothing else: rendering the fold lines would answer "how many lines of symmetry does it have?". Likewise the missing-side perimeter item labels only two sides, and the ruler items place the object away from zero. Each is the question, not an oversight.

## One exclusion

`TPL.NUM.PATTERN.FIGURATE.CUBE.001` asks the child to picture a solid cube of unit cubes. No 2D kind expresses it and a 3D projection kind for one template did not seem worth asking you to build. It stays text-only. If you would rather have it, say so — it is one kind away.

## Where two renderers will legitimately differ

Some placement is deliberately ours to leave open, because no variable determines it: which corner the grid cutout occupies, which cells the half-squares shade, where two abutting blocks join on the ruler, and how two figures in one array are arranged relative to each other. Our build makes these deterministic per instance, but the contract does not dictate them, so your rendering and ours may look different while both being correct. This stays open for now — say if you would rather we pinned any of it.

## What we need back

1. Does the array shape work for you?
2. Does your renderer handle rational strings, bare angles above 180°, clock and protractor contexts, and half symbols?
3. Does the side-index convention above match what your renderer expects, or do you need a different origin or winding direction?

We would rather change the contract now than after you have built against it.
