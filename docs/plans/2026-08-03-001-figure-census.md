# Figure Census — Class 6 Templates

Companion artifact to `docs/plans/2026-08-03-001-feat-partner-figure-support-plan.md` (U1). It records which templates are figure-dependent and which kind each one renders as. The two content waves and the coverage guard both derived from this list; the census is now fully annotated.

**Census: 78 of 238 templates.** The pre-audit annotation had 55. The audit flipped 27 templates to `diagram_required: true` and 4 off it; no `template_version` changed (KTD3).

## Corrections applied

**Flipped on — genuinely figure-dependent (27).** Measurement was the largest gap: every area, perimeter, and ruler-reading item was annotated text-only despite needing a scale or a labelled shape.

| Group | Templates | Why they were missed |
|---|---|---|
| Measurement area/perimeter | 9 `TPL.MEAS.*` | Stems describe a labelled shape or shaded grid in prose |
| Ruler reading | 3 `TPL.MEASLENGTH.RULER.*` | The whole skill is reading a scale; one item hinges on not starting at zero |
| 2D shape ID and rotation | 6 `TPL.GEO.SHAPES2D.*` | "This shape…" with no figure; rotations are inherently visual |
| Figurate and counting patterns | 5 `TPL.NUM.*` | Dot arrangements described in words |
| Tally charts | 2 `TPL.DATA.TALLY.*` | Stems verbalise bundles and loose strokes |
| Part-whole fractions | 2 `TPL.FRAC.PARTWHOLE.*` | Unequal-part diagrams described in prose |

**Flipped off — definitional, no figure to draw (4).** All `TPL.GEO.CIRCLEPARTS.*`: `COMPASS.SWAP`, `DIAMETER.TO.RADIUS`, `RADIUS.TO.DIAMETER`, `RADIUS.DIAMETER.DIFF.HARD`. These are `r` ↔ `2r` conversions that name a circle without depending on seeing one.

## Kind assignments

| Kind | Count | Templates |
|---|---|---|
| `shape` | 23 | `GEO.CONSTRUCTBASIC.{COMPASSSLIP.PERIMETER.HARD, RECTANGLE.PERIMETER, SQUARE.PERIMETER}`, `GEO.SHAPES2D.{ID.RECTANGLE, ID.TRIANGLE, ROTATE.SQUARE, ROTATE.SQUARE.HARD, ROTATE.TRIANGLE}`, `GEO.SYM.LINE.{ISOSCELES-TRI, PARALLELOGRAM, RECT-NONSQUARE, REGULAR-NGON, SQUARE-NGON-SUM}`, `GEO.SYM.ROTATIONAL.{NGON-SQUARE-SUM, RECTANGLE, RECTANGLE-OFFBYONE, REGULAR-NGON, REGULAR-NGON-OFFBYONE}`, `MEAS.AREA.CONCEPT.SAMEPERIM.DIFFAREA`, `MEAS.AREA.FORMULA.{LSHAPE.COMPOSITE, TRIANGLE.HALFRECT}`, `MEAS.PERIMETER.{PENTAGON.IRREGULAR, RECT.MISSEDSIDE}` |
| `angle` | 11 | `GEO.ANGLECONCEPT.{ARMLENGTH.COMPARE, ARMLENGTH.SUM.HARD, CLOCK.HOURTURN, TURN.COMPARE.SIZE}`, `GEO.ANGLEMEASURE.{CLOCKTURN, DRAWN.CHECK, REFLEX.HARD, SCALE.INNER, SCALE.OUTER}`, `GEO.ANGLETYPES.{RIGHTANGLE.MULTIPLE.HARD, RIGHTANGLE.TILTED}` |
| `number-line` | 6 | `FRAC.NUMLINE.{DEPTH, IDENTIFY, READ, SEGMENT, TRAIL.HARD}`, `INT.CONCEPT.POINT` |
| `point-line` | 6 | `GEO.ANGLECONCEPT.SHARED.VERTEX.RAYS`, `GEO.POINTLINE.{ENDPOINTS.SEGMENTS, LINES.SEGMENTS.MIX.HARD, NOTATION.POINTSONSEGMENT, RAYS.SEGMENTS.MIX, SEGMENTS.COLLINEAR}` |
| `pictograph` | 5 | `DATA.PICTOGRAPH.{CATCHUP.GAP, HALFSYMBOL.TOTAL, SYMBOL.SCALE.TOTAL, THREECATEGORY.TOTAL, TWOCATEGORY.DIFFERENCE}` |
| `tally-chart` | 5 | `DATA.TALLY.{BUNDLES.TOTAL, HIGHEST.MINUS.LOWEST, STROKES.DESCRIBED, THREECATEGORY.TOTAL, TWOCATEGORY.DIFFERENCE}` |
| `array` | 5 | `NUM.COUNT.{GROUPS.WORD, OBJECTS.DIRECT}`, `NUM.PATTERN.FIGURATE.{SQUARE, TRIANGULAR, TRI.PICTORIAL}` |
| `grid-region` | 4 | `MEAS.AREA.CONCEPT.{FLOORTILES.MCQ, GRID.COUNT, HALFSQUARES, LSHAPE.GRIDCOUNT}` |
| `bar-graph` | 3 | `DATA.BARGRAPH.{COMPARE.DIFFERENCE, GRIDLINE.VALUE, THREEBAR.TOTAL}` |
| `region` | 3 | `FRAC.PARTWHOLE.{DIRECT, MEDIUM, HARD}` |
| `circle` | 3 | `GEO.CIRCLEPARTS.POINTS.ONCIRCLE`, `GEO.CONSTRUCTBASIC.CIRCLE.FROMSEGMENT`, `GEO.SHAPES2D.ID.CIRCLE` |
| `ruler` | 3 | `MEASLENGTH.RULER.{READING, READING.MCQ, TWOBLOCKS}` |
| `circle` + `shape` | 1 | `GEO.CONSTRUCTBASIC.SQUARE.FROMRADIUS` — the only mixed-kind multi-figure item |

Twelve kinds cover the census, matching KTD6 once `grid-region` and `ruler` are counted.

## Kind requirements the audit fixed

- **`angle` carries a context.** Bare angle, clock face with numbered hour marks (5 templates), protractor overlay naming which scale is emphasised (4 templates), and a reflex arc drawn on the >180° side (1 template, up to 350°).
- **`region` needs unequal parts.** Two of the three part-whole items divide a whole into `s` small pieces *plus* one piece of relative size `k`. The kind carries per-part weights, a count expression per group, and a shaded flag on each -- not "n equal parts, m shaded". `FRAC.PARTWHOLE.DIRECT.001` also says only "A shape" with no variable naming it — the figure must carry a shape identifier the template does not currently declare.
- **`circle` needs construction context.** `CIRCLE.FROMSEGMENT` draws a segment as the diameter with endpoints and centre marked; a bare circle does not express it.
- **`shape` needs per-side labels and rotation state, and must NOT draw symmetry.** `PERIMETER.RECT.MISSEDSIDE` works only when exactly one length and one width are labelled; the rotation items need an applied rotation. The symmetry items authored `symmetry: none` deliberately: drawing the fold lines or marking the rotational order answers the question the item asks, and the verification gate cannot catch it, since its leak rule reads label maps and shapes carry none.
- **`ruler` needs scale plus object span.** Range, numbered major ticks, minor ticks, and the measured object's start and end marks — where the start is deliberately non-zero in two of the three.
- **Repetition is in-kind, not array-level.** `ANGLETYPES.RIGHTANGLE.MULTIPLE.HARD` shows `m` tilted right angles where `m` is a variable (2–9). A kind that repeats one element takes a count expression internally; the `figures` array is reserved for genuinely distinct figures (`SQUARE.FROMRADIUS`'s circle plus square, the five two-figure comparisons).

## Layout freedom exercised

Per R7, these need the renderer to choose a concrete arrangement no variable pins down: `MEAS.AREA.CONCEPT.HALFSQUARES` (where the half-shaded squares sit) and `MEASLENGTH.RULER.TWOBLOCKS` (where the two blocks join). Both choices must be deterministic so rebuilds stay byte-identical.

## Out of census

`TPL.NUM.PATTERN.FIGURATE.CUBE.001` asks the student to picture a solid cube of unit cubes, `n` per edge. No 2D kind expresses it; a figure would need an isometric projection with per-face gridlines. It stays `diagram_required: false` and text-only rather than adding a 3D kind to the partner contract for one template.
