# Iris's review standards

## Tolerances

A figure **ties out** when the dashboard and the source agree within these limits, after the documented bridge:

| What | Tolerance |
|---|---|
| Counts (orders, lines, units, receipts, cases, readings, events) | Exact |
| Money in the transaction currency | Exact to the cent |
| Money converted to USD | 0.01% (rounding of the AX month-end rate) |
| Rates and percentages | 0.1 percentage points |
| Medians, averages and durations | 0.1 hours, or 1% if that is larger |
| Snapshot balances (inventory on hand, open AR, pipeline) | Exact for the stated as-of time |

Anything outside these limits is a finding, however small. A difference of any size that changes:
- a status colour,
- target attainment,
- a ranking shown on screen (top 10, worst site),
- or the direction of a trend

is a **Blocker**.

## Severity

| Severity | Use it when |
|---|---|
| **Blocker** | A KPI tile or headline total is wrong or unexplained. A KPI in the spec is missing. A KPI has no agreed definition. Personal data is visible to the wrong audience. Totals don't add up on screen. |
| **Major** | A page screenshot, an export (visual or end table) or `bridge.csv` is missing. A visual or filter in the spec is missing or only partly built. A definition cites a document that isn't in the submission. A breakdown, drill or table value is wrong. A bridge item is undocumented or doesn't add up. The freshness or refresh SLA is not met. A filter changes some visuals and not others. The time zone or currency is wrong or unstated. Access rules are not evidenced. |
| **Minor** | Labels, number formats, sorting, missing definitions in tooltips, brand deviations that do not mislead, unclear titles. |
| **Polish** | Suggestions that would make it better but are not required. |

Raise one finding per gap, not one for each rule it breaks. For example, an unbuilt KPI is a single spec-coverage
Blocker that also asks for its catalog entry. Group like with like:
- each KPI that is missing or wrong is its own finding;
- the visuals and filters missing from one page are one Major that lists them;
- presentation points on one page are one Minor that lists them.

## Verdict

| Verdict | Rule |
|---|---|
| `not_ready` | No number could be checked: there is no `submission.yaml`, no screenshot or no KPI export, or the data isn't still. Nothing else was reviewed. |
| `not_signed_off` | At least one Blocker or Major is open. |
| `signed_off_with_conditions` | Only Minor or Polish findings are open, each with a ticket and the engineer's agreement to fix it. |
| `signed_off` | No open findings except Polish. |

Never sign off a dashboard whose KPI tiles you have not re-performed yourself.

## Sampling plan (minimum per round)

1. **KPI tiles.** Re-perform every tile for:
   - the latest complete period shown, at network or all-clients level;
   - one cut (a site, client or carrier: pick a different one each round, and favour the biggest or the one flagged
     red);
   - one earlier period: about three months back if the dashboard covers it, otherwise the oldest period it
     shows.
2. **Headline totals.** Take every count or amount in `bridge.csv` and tie dashboard to source through the bridge.
   If the submission includes a detail export, trace five records each way:
   - five source records in scope must appear in the export;
   - five rows from the export must exist in the source.

   If there is no detail export and a figure does not tie, ask for the detail rows behind that one cell.
3. **Breakdown visuals.** In each one, the parts must add up to the total shown, or the difference must be explained
   (for example a median, which doesn't add up).
4. **Trends.** Take two points from each trend chart: the latest and the oldest visible point.
5. **Filters.** Apply one filter combination to the CSV exports. Every exported visual must agree on that cut.
6. **Round 2 and later.** Re-check every previously open finding, then repeat steps 1 and 2 with different cuts.

If a tile cannot be re-performed from the sources (for example a cost KPI from cloud billing), say so in
`scope.not_checked` and ask for the evidence behind it.

## Reconciliation traps to rule out before raising a finding

- **Time zone.** WMS timestamps are warehouse-local without a zone. AX `modifieddatetime` and `createddatetime` are UTC.
  Salesforce is UTC. Sensor `device_ts_ms` is epoch milliseconds UTC. Compare on the time basis the catalog states.
- **Open periods.** Compare only periods that closed before the data-as-of time. The stream and batches keep
  changing recent data.
- **Hard deletes.** Rows deleted in the source after the dashboard refreshed will make the source lower. Check
  `submission.yaml`'s sim clock against the status endpoint.
- **Posted versus draft.** AX posted documents never change; corrections are new documents (credit notes are
  negative invoices). Unposted journals are drafts.
- **Currency.** The company reports in USD at the AX month-end rate. Client-facing views use the client's billing
  currency.
- **Units.** TOR1 and MTY1 use kilograms and centimetres; the US sites use pounds and inches.
- **Legal entity.** AX data is partitioned by `dataareaid` (`us01`, `ca01`, `mx01`).

If a difference survives all of these, it is a finding. Report it with the samples that show it.

## Presentation checklist (the Harborline brand kit)

Check against the screenshots. Details are in `brand/specs/formats.json`, `brand/do-dont/` and `handbook/brand.html`.

- **Layout.**
  - The header band carries the Harborline lockup, the dashboard title and the data-through time.
  - The footer shows freshness and a link to the metric catalog.
  - The department accent matches the project.
- **KPI tiles.** The spec sets how many; they sit in one row. Each tile has a value, a comparison (target or prior
  period) and a short definition or tooltip. Context is required, not optional.
- **Spec versus brand kit.** Where they seem to disagree, the spec wins.
- **Number formats.**
  - `$` means USD only; every other currency shows its ISO code.
  - Tiles abbreviate (1.28M); tables show full numbers.
  - Rates have one decimal (96.4%); changes in a rate are in pp.
  - Finance statements use parentheses for negatives.
- **Time.** Every timestamp states its zone:
  - Operations: warehouse local time;
  - Platform: UTC;
  - Executive: Eastern.
- **Colour.**
  - Status colours are used only for status.
  - No red and green as the only signal.
  - Categorical colours come from the brand palette, with one colour per series rather than per bar.
- **Charts.**
  - No pie with more than five slices.
  - No dual-axis charts.
  - No truncated bar axes.
  - Titles say what the chart answers.
- **Readability.**
  - Readable at 1920×1080 without zooming.
  - Legends are not needed to decode the main message.
  - Empty and zero states are labelled.

## Writing a finding

- **Title.** One line saying what is wrong, not what to do. For example: "On-time ship for ATL1 is 1.3 pp higher than
  the WMS for week 2027-W02".
- **Where.** The visual › tile or column › cut.
- **Values.** The dashboard value, your value and the difference (absolute and relative). Format them the way the
  dashboard does.
- **Detail.** What you compared and what you ruled out (time zone, open period, deletes). No SQL, no table names
  unless the engineer needs them to find the records.
- **Samples.** Up to five business keys the engineer can look up: order number, invoice id, tracking number, sensor
  id plus timestamp, Salesforce record id.
- **Ask.** What evidence or result would close it. Never how to fix it.
