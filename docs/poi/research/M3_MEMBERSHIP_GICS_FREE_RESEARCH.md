# M3 free-source research: S&P 500 membership and GICS

**Research status:** documentation only; not an admission decision  
**Original knowledge / URL review date:** 2026-10-31; **methodology edition-date verification:** 2026-11-01
**Scope:** public, no-key sources that might help corroborate effective-dated S&P 500 membership or sector labels

## Conclusion

None of the sources below is admitted to M3 or to a backtest. No source in this memo is tagged `OFFICIAL_PUBLIC` or `PIT_STRICT`. The existing M3 snapshot remains unchanged, including its evidence, datasets, and strategy/trading behavior.

The free-source landscape splits into two different problems:

1. **Membership history:** a current list plus a table of announced changes can help reconstruct intervals, but it is not itself a complete, point-in-time constituent ledger.
2. **Sector classification:** SEC SIC is a regulatory filing classification and is not GICS. A current GICS label is also not evidence of the GICS label that was effective on an earlier backtest date.

Accordingly, the candidates are useful for research and cross-checking only. They do not meet a strict point-in-time standard without dated, independently corroborated records, reproducible version pinning, and resolved reuse terms.

## Existing M3 snapshot baseline

The dated-sector cross-check resolved **417 of 503** constituent rows: **417 / 503 = 82.90% achieved coverage**; **86 / 503 = 17.10%** were not covered. All **417 covered assignments agreed (417/417, 100% agreement among covered names)**. The candidate nonetheless **failed the frozen 90% minimum coverage gate**, so it remains NOT_ADMITTED. This memo preserves the prior finding, without filling missing labels from current GICS or lowering the threshold.

The baseline also exposes two separate notions of time:

- **As-of/effective date:** when a constituent membership or classification was true in the market.
- **Knowledge date:** when the record became publicly knowable to the research process.

Neither a repository commit date nor a web-page retrieval date proves the underlying effective date. Conversely, a press-release announcement date is a knowledge date and may precede the change's effective date. Both dates are required for strict PIT use.

## Candidate comparison

| Candidate and original links | Source date / version to retain | What it can establish | Reported limits | License / terms finding | M3 disposition |
|---|---|---|---|---|---|
| **Wikipedia, “List of S&P 500 companies”**: [article](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies), [revision history](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&action=history), and [MediaWiki revision API](https://en.wikipedia.org/w/api.php?action=query&prop=revisions&rvprop=ids%7Ctimestamp&titles=List_of_S%26P_500_companies&format=json) | Dynamic source reviewed 2026-10-31. A reproducible capture must retain the numeric `oldid`, revision timestamp, retrieval/knowledge timestamp, and content hash; “latest” is not a version. Each change row's stated effective date must remain distinct from the page revision time. | Current table supplies current membership and current GICS labels; the changes table supplies many additions/removals and stated dates, generally with citations to announcements. | The page does not claim to be a complete canonical effective-dated ledger. Editorial revisions, ticker/name changes, omissions, citation drift, and same-day replacement semantics must be handled. Current GICS values are not historical GICS values. | Page footer links the [Wikimedia Terms of Use](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use); text is offered under **CC BY-SA 4.0**, with **GFDL** also applying unless otherwise indicated. Reuse requires attribution/share-alike compliance and attention to third-party material. | Research cross-check only; neither `OFFICIAL_PUBLIC` nor `PIT_STRICT`; not admitted. |
| **DataHub / frictionlessdata, “S&P 500 Companies with Financial Information”**: [repository](https://github.com/datasets/s-and-p-500-companies), [data package descriptor](https://github.com/datasets/s-and-p-500-companies/blob/main/datapackage.json), and [license file](https://github.com/datasets/s-and-p-500-companies/blob/main/LICENSE.md) | Mutable `main` reviewed 2026-10-31. Any experiment must pin a full Git commit SHA and hashes of the descriptor and CSV; a download date alone is insufficient. The package metadata identifies upstream provenance and should be retained with the data. | Convenient machine-readable **current snapshot** and sector field; useful for symbol/schema comparisons against another current list. | Repository history is publication history, not guaranteed constituent effective history. It does not provide a complete interval ledger or effective-dated GICS history. Financial fields and constituent/sector fields can have different upstream dates. | Repository `LICENSE.md` declares the packaged database under the **Open Data Commons Public Domain Dedication and License (PDDL) v1.0**. That declaration must not be generalized to upstream text, logos, trademarks, or independently protected third-party content. | Research cross-check only; current snapshot is not PIT; neither `OFFICIAL_PUBLIC` nor `PIT_STRICT`; not admitted. |
| **SEC EDGAR company metadata and SIC documentation**: [company tickers JSON](https://www.sec.gov/files/company_tickers.json), [EDGAR submissions API documentation](https://www.sec.gov/edgar/sec-api-documentation), [SIC lookup](https://www.sec.gov/search-filings/standard-industrial-classification-sic-code-list), [privacy/copyright policy](https://www.sec.gov/privacy.htm), and [automated-access policy](https://www.sec.gov/about/webmaster-frequently-asked-questions) | Live endpoints/documents reviewed 2026-10-31. A capture would require retrieval timestamp, response hash, CIK mapping version, and filing accession/acceptance timestamp where a filing is the evidence. | CIK/ticker/name identity support, filing knowledge timestamps, and the registrant's SEC SIC value. It can independently challenge an issuer mapping. It does **not** establish index membership or GICS. | SEC asks automated clients to comply with fair-access guidance (currently stated as no more than 10 requests/second and with declared user-agent contact information); service can throttle or block noncompliant traffic. This research performed no bypass. SIC can be issuer-level, stale, broad, or changed through filings. | SEC states that, except where otherwise noted, information created by the federal government is public domain; the policy also warns that third-party material can be protected and that SEC names/logos have restrictions. Provenance and notices must travel with a capture. | Identity/SIC cross-check only; **SIC must never populate GICS**; neither `OFFICIAL_PUBLIC` nor `PIT_STRICT`; not admitted. |
| **S&P Dow Jones Indices public methodology and announcements**: [S&P U.S. Indices Methodology](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-us-indices.pdf), [index announcements](https://www.spglobal.com/spdji/en/index-announcements/), [GICS methodology](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-gics.pdf), and [website terms](https://www.spglobal.com/spdji/en/terms-of-use/) | **Verified cover dates (PDF first page, 2026-11-01):** S&P U.S. Indices Methodology = **July 2026**; GICS Methodology = **April 2026** in the rendered PDF rendition. These are *methodology edition months*, NOT constituent effective dates or company-level knowledge dates. An alternate indexed rendering of the mutable GICS URL displayed **October 2026**, so a stable present-day GICS edition is NOT proven without pinning exact PDF bytes and SHA256. The announcements index has no single date; each event announcement needs its publication and effective dates. No raw S&P file or hash was obtained and these documents remain research-only. | Primary documentation for index rules, announced constituent changes, and the GICS taxonomy. Individual announcements can corroborate knowledge and effective dates. | Public announcements are event fragments, not a guaranteed complete bulk historical membership/GICS dataset. Methodology explains rules but does not assign every issuer for every date. Site availability does not imply bulk redistribution or model/backtest rights. | S&P materials and GICS are proprietary; the linked terms reserve rights and impose use/redistribution conditions. No paid product was accessed and no entitlement is inferred from public URL access. | Citation-level corroboration only; no bulk ingestion; neither `OFFICIAL_PUBLIC` nor `PIT_STRICT`; not admitted. |

## Official source-date verification and version limits (2026-11-01)

The official S&P DJI document covers were checked as rendered first-page PDF snapshots:

| Methodology | Source | Printed edition date | Verification observation | Evidence boundary |
|---|---|---|---|---|
| S&P U.S. Indices Methodology | https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-us-indices.pdf | **July 2026** | Official PDF, rendered cover/page 1, viewed 2026-11-01 | Edition date only; no security-specific effective or public-knowledge date |
| Global Industry Classification Standard (GICS) Methodology | https://www.spglobal.com/spdji/en/documents/methodologies/methodology-gics.pdf | **April 2026** on the rendered cover | Official PDF, rendered cover/page 1, viewed 2026-11-01 | An indexed textual rendition of this same mutable URL reports **October 2026**. Editions may differ by snapshot/cache; no stable immutable revision was pinned. Verify exact bytes before relying on one edition |

The **S&P index-announcements listing** (https://www.spglobal.com/spdji/en/index-announcements/) is not one dated document. An individual announcement must be linked to its *actual announcement/publication date* (knowledge date) and *separate effective date*. Neither methodology cover date establishes historical membership or GICS assignments at the company level.

**Reproducibility limit:** On 2026-11-01, the Oracle host returned HTTP 403 for both S&P PDF URLs. No access controls were bypassed, no local raw PDF files or content SHA256 were obtained, and the older 2026-10-31 review cannot be retroactively claimed to have captured exact PDF bytes. Source-date observations above come from public rendered covers, NOT an archived immutable primary artifact. Before any official admission, an authorized source capture must save permissible original bytes, SHA256, retrieval timestamp, printed edition, any revision discrepancy, and event-specific publication/effective timestamps. No data-source status is promoted by this memo.

## SIC is not GICS; current GICS is not historical GICS

These fields must remain distinct in schema and reasoning:

- **SEC SIC** is a four-digit industry code used for SEC registrant/filing administration. It is not maintained as GICS and has no valid mechanical one-to-one conversion to a GICS sector, industry group, industry, or sub-industry.
- **GICS** is the proprietary S&P Global/MSCI hierarchy. A label such as “Information Technology” is part of that taxonomy, not an SEC SIC label.
- **Current GICS** answers how a source classifies an issuer at the source snapshot's current time.
- **Effective-dated GICS** requires a classification value together with its valid-from/valid-to boundary (or a dated change event), plus when that evidence became knowable. Taxonomy revisions and company reclassifications mean today's label cannot be backfilled onto yesterday's constituent row.

SIC may be retained as contradictory or supporting identity evidence, but must not be renamed, cross-walked, or used as a GICS substitute merely to improve coverage. Even a curated SIC-to-GICS mapping would be an analyst model, not original historical GICS evidence.

## Independent dated cross-checks and their limitations

A defensible research cross-check would compare at least two independently maintained, date-bearing records for each event, for example a pinned Wikipedia revision/citation against a dated S&P announcement, with SEC filing metadata used only for issuer identity. The comparison must preserve:

- announcement/knowledge timestamp and effective membership date separately;
- source revision/commit/accession and cryptographic content hash;
- old and new ticker, legal/entity identity (preferably CIK), and corporate-action context;
- addition and removal as a paired event where appropriate;
- classification taxonomy/version and classification effective dates independently of membership dates.

Important limits remain:

1. **Shared provenance is not independence.** DataHub reports Wikipedia/upstream sources; agreement between a repackaged CSV and Wikipedia may be the same evidence twice.
2. **Archive time is not event time.** A commit, page revision, or web-archive timestamp proves availability no earlier than that observation; it does not prove when membership or GICS became effective.
3. **Announcements may be incomplete.** Missing removals, delayed changes, mergers, spin-offs, ticker reuse, multiple share classes, and corrections can break interval reconstruction.
4. **Current snapshots create survivorship and look-ahead risk.** They omit former members and may apply later names or classifications to earlier dates.
5. **Dated agreement can still be wrong.** Sources can copy each other, repeat a typo, use announcement versus close/effective dates inconsistently, or silently revise content.
6. **Licensing remains source-specific.** A permissively licensed wrapper does not necessarily grant rights in proprietary GICS assignments or upstream third-party content.

## Contradictory evidence to retain, not overwrite

The research surfaced contradictions that should remain explicit:

- The existing M3 cross-check achieved **417/503 = 82.9% coverage**, below the **90% floor**, despite **417/417 agreement** within the covered subset; current free tables may appear to have broader coverage. That is not proof that M3 can pass: the free labels are generally current, not effective-dated.
- SEC may report a SIC category whose plain-language industry differs from a current GICS sector. This is expected because the taxonomies, purposes, granularity, and maintenance processes differ; SIC does not override GICS.
- A Wikipedia change row may cite an S&P announcement but carry a date interpreted as the effective date, while page revision history records a later knowledge date. Both must be kept rather than collapsing them into one timestamp.
- DataHub may agree with Wikipedia on symbols and sectors while not being independent evidence because its documented provenance derives from upstream public sources.
- Ticker/name agreement does not guarantee entity agreement. Reused tickers, reorganizations, share-class changes, acquisitions, and CIK changes require explicit resolution.

## Remaining owner gaps

No owner is assigned or changed by this memo. The following decisions/evidence remain owner gaps before any future admission proposal:

- **Data owner:** define the authoritative membership interval schema, inclusion boundary (announcement, prior close, opening, or stated effective date), and same-day event semantics.
- **Research/evidence owner:** produce a complete event-by-event exception ledger with two genuinely independent dated sources, pinned versions, hashes, and contradiction resolution.
- **Taxonomy owner:** obtain legally usable effective-dated GICS assignments and taxonomy-version history; document treatment of company and taxonomy reclassifications.
- **Legal/licensing owner:** approve reuse and redistribution for each exact artifact, especially GICS/S&P material and any downstream package with third-party provenance.
- **Entity-resolution owner:** validate ticker/name/CIK/share-class/corporate-action mappings and ticker reuse.
- **Model-risk owner:** define PIT and coverage admission tests, including whether unknown classifications remain null and how the 90% requirement is evaluated by date.
- **Operations owner:** define immutable capture, hashing, provenance, correction, and reproducibility controls without bypassing access restrictions.

Until those gaps are closed, **417/503 = 82.9% achieved coverage** remains **NOT_ADMITTED**, because this is below the frozen **90% coverage floor**; no free-source enrichment should enter the backtest.

## Guardrails / non-actions

- No dataset, evidence record, source tag, strategy, or trading capability was changed.
- No candidate was assigned `OFFICIAL_PUBLIC` or `PIT_STRICT`.
- No candidate was admitted to a backtest or used to patch missing GICS values.
- No paid provider, subscription data, API key, or credential was used.
- No robots rule, rate limit, access control, paywall, or scraping restriction was bypassed.
- URLs are recorded as research references; a future ingestion review must re-check HTTP availability, redirects, document revision dates, terms, and hashes at capture time.
