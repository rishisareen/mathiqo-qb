# Structured figures — review packet

**To:** partner engineering
**Re:** your `fetch_batch` figure-support request
**Asks of you:** confirm two deviations from your proposal, and tell us whether four gaps we hit are worth closing before we freeze.

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

## One gap you should push back on

**Side indices are not defined, and you cannot render `shape` figures correctly without them.**

`side_labels[].side` is a zero-based edge index, but nothing in the contract says which edge index 0 is, or which way the numbering winds. On a labelled rectangle you can guess. On the composite L-shape you cannot: it has six edges, we label indices 0, 1, 3, 4, and skip 2 and 5. Which four edges carry those lengths is genuinely unknowable from the data.

We can fix this by pinning a convention — side `i` runs from vertex `i` to vertex `i+1`, counter-clockwise from a stated origin — but we would rather agree it with you than assert it, since your renderer has to implement it. **Tell us what is natural on your side and we will match it.** Until then, treat side indices on anything more complex than a rectangle as unreliable.

## Four smaller gaps — tell us which matter

Each is a place our contract is thinner than the content. None blocks adoption; all are cheap to add now and awkward later.

1. **No way to mark a circle's centre.** `circle` marks the centre only under `construction: segment-as-diameter`. One item counts points on a circle versus points elsewhere, and its centre O should be visible but is not expressible — we refused to fold it into the off-circle count, since the stem counts those as distinct things.

2. **No way to say two angles share a vertex.** Two comparison items draw angles at a shared vertex; we currently emit them as two independent figures, so the shared vertex is lost.

3. **No angle whose measure crosses the reflex boundary.** `bare` requires ≤180°, `reflex` requires >180°. An angle measured in hour-marks ranges 30°–330° depending on the variable, so neither fits across its domain. We used `clock` (accurate — those stems really do say hour-marks), but an item about arm-length contrast now renders on a clock face where hands have fixed length.

4. **No altitude on a triangle, and no caption on a figure.** One area item states the enclosing rectangle's height, which is not a triangle side; another draws two rectangles the stem calls "A" and "B", and their identity currently rests entirely on array order.

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

Some placement is deliberately ours to leave open, because no variable determines it: which corner the grid cutout occupies, which cells the half-squares shade, where two abutting blocks join on the ruler, and how two figures in one array are arranged relative to each other. Our build makes these deterministic per instance, but the contract does not dictate them, so your rendering and ours may look different while both being correct. Say if you would rather we pinned any of them.

## What we need back

1. Does the array shape work for you?
2. Does your renderer handle rational strings, reflex angles above 180°, clock and protractor contexts, and half symbols?
3. What side-index convention do you want?
4. Which of the four smaller gaps do you want closed before we freeze?

We would rather change the contract now than after you have built against it.
