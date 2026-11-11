# Race and Hispanic-origin coverage

The hosted question “How many Whites are in Fenton Village?” returned a
generic refusal on revision 01f396df4e7cfc3e851f5a09591a4522e910a60c.
The partner race fields used 2010 geography and an unconfirmed data period.
Only one of Fenton's three current mapped tract IDs existed in that table;
joining matching IDs could silently omit residents or combine wrong areas.

This change adds official ACS 2020–2024 tables B02001 (Race) and B03002
(Hispanic or Latino Origin by Race), on geography matching primary ACS.
All 232 county tracts and the official Silver Spring place are represented.
There are 7,223 estimate/MOE observations covering 31 measures, including
parent/subtotal rows. Those 31 rows are not 31 disjoint population groups.

Official labels and parent relationships are retained, with file URLs,
SHA-256 checksums, source Last-Modified and actual download times. The data
are extracted from the Census public summary files without an API key.
This does not change the model provider or require runtime downloads.

## Verified Fenton example

For the three tracts already mapped to Fenton Village, B02001 White alone:

| Current tract | Estimate | Published margin of error |
| --- | ---: | ---: |
| 24031702402 | 1,465 | 459 |
| 24031702502 | 555 | 198 |
| 24031702503 | 1,118 | 274 |

The sum is **3,138**, an ACS estimate for the whole-tract approximation,
not an exact district count or a current headcount. White alone includes
Hispanic residents. B03002 non-Hispanic White alone sums to **2,907** for
the same three tracts. Do not substitute one definition for the other.
The displayed margins above are individual tract MOEs; they are not added
to imply an aggregate MOE. Unknown/sentinel values are retained as NULL.

## Answer behavior

The existing text-to-SQL flow receives a catalog generated from installed
measures and category labels. There is no keyword query router or fixed
answer value. The prompt explains current tract/place filtering, specific
race/origin definitions, missing estimates, and overlapping parent groups.
One complete, labeled demographic lookup can be rendered from its returned
estimate without another model call. Other comparisons/lists still use
the existing general model path.

Answers cite B02001/B03002 with the actual tract or official-place scope,
rather than a generic population table. District-approximation and category
warnings remain visible. Official-place queries highlight the official
boundary. The source/measure catalog is exposed through GET /coverage.

## Validation and rollout

All 64 automated tests pass, including eight new demographic regressions.
Checks cover current geography completeness, reconciliation of race/origin
totals with every primary population, category hierarchy, wording variations,
exact Fenton examples, correct citations/map scope, missing estimates,
offline repeat imports, primary/partner preservation, and failed-import
rollback. The bundled data makes deployment immediately queryable without
a full external rebuild. Rebuilds reapply the validated extract.

Local provider stand-ins validate query execution and result formatting;
they do not establish fresh model SQL generation. After deployment, test
the original wording plus White residents, non-Hispanic White residents,
Black residents, Asian residents, and Hispanic residents, with both named
district and official-place scopes. Inspect SQL, labels and citations. The
hosted site continues serving its prior database until this branch is
merged and the deployment updates.
