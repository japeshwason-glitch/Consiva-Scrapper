# CRM schema for the Auto Email Campaign

Read-only discovery, 2026-10-12. Every query was a `SELECT` against
`INFORMATION_SCHEMA`, `sys.*` or an aggregate; nothing was written, altered or
created. No email addresses, names of contacts or credentials were printed or
stored. The raw output was kept outside the repo (`/tmp/crm_discover.out`,
`/tmp/crm_counts.out`).

Scope: the lead table is **`dbo.CRM_Scrapper`**. The server is SQL Server, one
database (`CookieConsentDB`, 238 tables/views); `CRM_LEADS_DB_NAME` resolves to
the same database, so there is no separate leads database.

## Tables that matter

| Table | Rows (approx.) | Role |
|---|---|---|
| `dbo.CRM_Scrapper` | 14,024 | the leads (companies); report columns live here |
| `crm.CrmCompanyContacts` | 6,764 | the people: names, emails, job titles |
| `crm.CrmCompanyOwnership` | 14,756 | which user owns a company (the "rep") |
| `crm.CrmUsers` | 11 | reps/users |
| `crm.CrmCompanyStatus` | 1,417 | lead status per company |
| `crm.CrmCompanyActivities` | 6,566 | notes, calls, emails sent per company |
| `crm.CrmCompanyEmailEvents` | 12,635 | SendGrid-style events per email activity |
| `crm.CrmEmailCampaigns` | 5 | existing email campaign feature (AI-personalised, UTM fields) |
| `crm.vw_CrmCompanyEmailThreads` | view | email threads per company |
| `crm._Backup_079_PartnerImportOwnership` | 3,536 | previous owners of PartnerImport leads |
| `crm.CrmLeads` | 113 | a **different** lead table: inbound/Meta/LinkedIn/partner leads |
| `dbo.Leads` | 14 | website contact-form leads, with UTM columns |

## 1. Leads: `dbo.CRM_Scrapper`

`Id int` is the primary key. This is the same `Id` the report job stores as
`lead_ref`, and the same value `CompanyId` holds in the `crm.CrmCompany*`
tables (verified: every `CompanyId` in contacts, ownership and status exists
in `CRM_Scrapper.Id`).

| Column | Type | Notes |
|---|---|---|
| `Id` | int, PK | |
| `CompanyName` | nvarchar(250) NOT NULL | |
| `Website` | nvarchar(500) | the domain source; 863 rows have none |
| `Source` | nvarchar(50) | see below |
| `SourceMarketingPartnerId` | uniqueidentifier | one distinct partner; null on 10,485 rows |
| `ScrapeStatus` | nvarchar(20) | `done`, `failed`, `no_website`, or NULL |
| `ScrapeAttempts` | int NOT NULL | |
| `ScrapedAt`, `CreatedAt` (NOT NULL), `UpdatedAt` | datetime2 | created 2026-09-11 to 2026-10-12 |
| `CompanyScale` | nvarchar(100) | **the only size field**; mixed vocabulary, see flags |
| `Industry` | nvarchar(200) | |
| `ContactName`, `ContactTitle` | nvarchar(200) | a single contact, from the MCA pipeline |
| `LushaContactId`, `LushaCompanyId`, `LushaWorkEmail`, `LushaPhone`, `LushaLinkedInUrl`, `LushaEnrichedAt`, `LushaMatchStatus`, `LushaMatchStrategy` | various | Lusha enrichment; `LushaWorkEmail` is empty on every row |
| `TargetTitle1..3` | nvarchar(120) | the persona titles to hunt |
| Consent/report columns | | `GapScore int`, `TrackersPreConsent int`, `ThirdPartiesCount int`, `CookieBanner nvarchar(100)`, `CollectsViaForm nvarchar(max)`, `ConsentFinding nvarchar(max)`, `DownloadReportUrl nvarchar(1000)`, `ReportStatus nvarchar(50)`, `ReportGeneratedAt datetime2` |

**Source values and counts**

| Source | ScrapeStatus | Rows |
|---|---|---|
| NULL (the MCA pipeline) | NULL | 9,758 |
| CsvImport | NULL | 727 |
| PartnerImport | done | 2,351 |
| PartnerImport | NULL | 861 |
| PartnerImport | failed | 325 |
| PartnerImport | no_website | 2 |

There is **no email, first/last name or assigned-rep column** on this table. Those
live in the tables below.

## 2. Contacts: `crm.CrmCompanyContacts`

`Id uniqueidentifier` PK. `CompanyId int` -> `dbo.CRM_Scrapper.Id`.

`FirstName` (NOT NULL), `LastName`, `Email nvarchar(256)`, `Phone`,
`JobTitle nvarchar(150)`, `IsPrimary bit`, `CreatedAt`, `UpdatedAt`,
`CreatedBy`, `UpdatedBy`, `IsDeleted bit`. No unique constraint on the email.

## 3. Reps: `crm.CrmUsers` and ownership

`crm.CrmUsers`: `Id uniqueidentifier` PK, `FirstName`, `LastName`, `Email
nvarchar(255)` NOT NULL, `Phone`, `JobTitle`, `Team`, `ManagerUserId`, `TeamId`
(-> `crm.CrmTeams`), `IsActive`, `LastLoginAt`, `IsDeleted`, plus audit columns
and `PasswordHash` (never select it). 11 users, all active, all with a valid-looking email.

**Lead -> rep:** `crm.CrmCompanyOwnership (Id, CompanyId int, OwnerUserId
uniqueidentifier -> CrmUsers.Id, IsDeleted, audit columns)`. Join:
`CRM_Scrapper.Id = CrmCompanyOwnership.CompanyId AND IsDeleted = 0`, then
`OwnerUserId = CrmUsers.Id`. Contacts do not carry an owner; they inherit the
company's. `crm.CrmEmailCampaigns.OwnerUserId` and
`crm.CrmCompanyActivities.OwnerUserId` also point at `CrmUsers`.

## 4. Reports

Not a table: the report is three columns on the lead row. `DownloadReportUrl`
(R2 URL), `ReportStatus` (`generated`, `no_gap`, `unscannable`, `failed`),
`ReportGeneratedAt`, with `GapScore` and the other five finding columns beside
them. The richer data (scan date, findings, evidence) is in Postgres
(`report_scans`, `report_findings`, `reports`), keyed by `lead_ref` =
`CRM_Scrapper.Id`. Join across the two databases is on that integer only.

## 5. UTM and site activity

There is **no table of site visits or page views**. UTM values are stored only
where a person submits something:

| Table | UTM columns | Rows with data |
|---|---|---|
| `dbo.Leads` (website form) | `UtmSource, UtmMedium, UtmCampaign, UtmContent, UtmTerm`, plus `PageUrl`, `IPAddress` | 14 rows, 3 with UTMs |
| `crm.CrmLeads` | `UtmSource, UtmMedium, UtmCampaign` only (no content/term) | 113 rows, 18 with a source |
| `dbo.Tenants` | `UtmSource ... UtmContent` | 0 rows filled |
| `dbo.BlockedSignupAttempts` | `UtmSource ... UtmContent` | 0 rows filled |
| `crm.CrmEmailCampaigns` | configured UTM fields for outbound links | 5 test campaigns |

`utm_content`: stored in its own column (`dbo.Leads.UtmContent nvarchar(200)`),
not packed into another field. In `dbo.Leads` there are two distinct values, the
longest 24 characters, and none contain characters outside `A-Za-z0-9_.-`; that
fits "stored as sent", but only three rows exist, so it is not proof the capture
code does no transformation. Whether the visitor identity can be tied back to a
lead (a lead id in `utm_content`) is not recorded anywhere yet.

## 6. Existing status and activity fields

- `crm.CrmCompanyStatus (CompanyId, Status, LostReason, ...)`: `Contacted`
  1,407, `Unqualified` 7, `Lost` 2, `Converted` 1 (non-deleted).
- `crm.CrmCompanyActivities`: `Type` (`Note` 4,343, `Email` 2,191, `Call` 32),
  `Subject`, `Notes`, `ActivityDate`, `OwnerUserId`, and for emails `ToEmail`,
  `ContactId`, `ParentActivityId`, `FollowUpTrigger`, `RepliedAt`.
- `crm.CrmCompanyEmailEvents`: `CompanyActivityId`, `EventType`, `EventAt`,
  `ProviderEventId` (UNIQUE), `ProviderMessageId`, `RecipientEmail`, `Url`,
  `Reason`, `RawPayload`. Types seen: Processed 4,369, Delivered 4,160, Open 2,885,
  Click 701, Deferred 274, Bounce 245, Dropped 1. No spam-report or unsubscribe events.
- `dbo.Leads.Status` is `new` on all 14 rows; `AssignedTo` (free text) is empty on all 14.
- `crm.CrmEmailCampaigns`: `Status`, `RelatedToType`, `RecipientCount`,
  `DispatchedCount`, `CompositionMode` (all five are `AiPersonalized`), `Objective`,
  `Offer`, `PreferredCta`, `Tone`, and the UTM columns above.
- There is no suppression, unsubscribe or opt-out table (the only name match was
  `dbo.BlockedSignupAttempts`, which is unrelated).

## 7. How the tables join

```
dbo.CRM_Scrapper.Id  ──── crm.CrmCompanyContacts.CompanyId      (1 company : N contacts)
                     ──── crm.CrmCompanyOwnership.CompanyId  ── OwnerUserId ──> crm.CrmUsers.Id
                     ──── crm.CrmCompanyStatus.CompanyId
                     ──── crm.CrmCompanyActivities.CompanyId ──< crm.CrmCompanyEmailEvents.CompanyActivityId
Postgres: report_scans.lead_ref / reports.lead_ref  =  dbo.CRM_Scrapper.Id   (integer, cross-database, no FK)
```

## 8. Counts

All figures are from 2026-10-12.

**Leads**

| | Count |
|---|---|
| Total `CRM_Scrapper` rows | 14,024 |
| PartnerImport | 3,539 (done 2,351, failed 325, not yet scraped 861, no_website 2) |
| CsvImport | 727 |
| Source NULL | 9,758 |
| No `Website` | 863 |
| Websites shared by more than one row | 39 websites over 83 rows |
| Company names shared by more than one row | 18 names over 36 rows |

**Reps**

| | Count |
|---|---|
| Leads with no active owner | **0**, in every Source |
| Leads with more than one active owner | 0 |
| PartnerImport leads owned by one user | **3,539 of 3,539** (a single user, "Executive") |
| Source NULL and CsvImport leads | also all owned by that same user |
| Previous owner of 3,536 PartnerImport leads (backup 079) | a user named "CRM" |
| Users | 11, all active |

**Emails** (on `crm.CrmCompanyContacts`; the lead row has none)

| | Count |
|---|---|
| Contacts | 6,764 over 4,292 companies |
| Contacts with no email | 155 |
| Free-mail (gmail, yahoo, outlook, hotmail, rediffmail) | 1,472 (21.8%) |
| Contacts with no job title | 8 |
| Average contacts per company (the CRM has no separate domain key) | 1.58 |
| Distribution | 1 contact: 2,797 companies; 2: 875; 3: 416; 4: 142; 5 or more: 62 |
| Companies with contacts, by Source | PartnerImport 3,539 (5,487 contacts), CsvImport 727 (1,207), NULL 26 (70) |
| Email addresses on more than one contact row | 3 addresses, 6 rows |
| Contacts of "generated" PartnerImport leads with a non-free email | 1,705 over 957 companies |
| `LushaWorkEmail` filled on the lead row | 0 of 14,024 |

**Reports** (PartnerImport; first generated 2026-10-07)

| `ReportStatus` | Rows | With URL |
|---|---|---|
| generated | 1,188 | 1,188 |
| no_gap | 865 | 0 |
| unscannable | 291 | 0 |
| failed | 7 | 0 |
| NULL | 11,673 | 0 |

Reports (rows with a URL) per `GapScore` band: **0-24: 375; 25-49: 309; 50-74: 452;
75-90: 36; 91-100: 16**. All 1,188 were generated between 07:32 and 09:45 on
2026-10-07, so all are inside 14 days today and all pass 14 days on 2026-10-21.

`GapScore` across all rows: PartnerImport 0-24 379, 25-49 316, 50-74 472, 75-90 153,
91-100 1,031, NULL 1,188; Source NULL 0-24 2,365, 25-49 1,941, 50-74 3,860, 75-90
1,473, 91-100 119; CsvImport all NULL.

**Email already sent from the CRM**

- 2,191 `Email` activities, **all owned by that one user**, 2026-09-23 to 2026-10-09.
- 1,415 PartnerImport companies already have an `Email` activity; 1,405 are `Contacted`.
- 245 Bounce events; no spam-report or unsubscribe events recorded.

## 8b. PartnerImport only (the campaign's scope; lead ownership is out of scope, the CRM owner is handling it)

Emails are on `crm.CrmCompanyContacts`, not on `CRM_Scrapper`. Every one of the
3,539 PartnerImport companies has at least one contact with an email.

Contacts: 5,487 over 3,539 companies; 5 with no email; 1,367 free-mail; 4,115
business-domain emails.

| ReportStatus | Companies | With any email | With a business-domain email | Email domain appears in `Website` | Already has an `Email` activity |
|---|---|---|---|---|---|
| generated | 1,188 | 1,188 | 957 | 758 | **1,188** |
| no_gap | 865 | 865 | 700 | 554 | 177 |
| unscannable | 291 | 291 | 224 | 175 | 46 |
| failed | 7 | 7 | 5 | 4 | 4 |
| NULL (not scanned or not done) | 1,188 | 1,188 | 540 | 134 | 0 |

Every company that already has a report has also had an email logged against it in
the CRM. "Already has an `Email` activity" is a CRM activity row of Type=Email; it
does not say who wrote it or whether it carried the report link.

## 9. Flags: missing or surprising

1. **There is effectively no "assigned rep".** Every lead of every Source is owned
   by the same user. PartnerImport leads were bulk-reassigned from a system user
   named "CRM" (migration 079, backed up in `crm._Backup_079_PartnerImportOwnership`).
   Selecting "leads with an assigned rep" selects everything, and per-rep caps,
   per-rep bounce breakers and "send from the assigned rep's address" would all
   resolve to one person unless real owners are assigned first.
2. **The CRM already sends email.** `CrmEmailCampaigns`, `CrmCompanyActivities`
   (Type=Email) and `CrmCompanyEmailEvents` hold 2,191 sent emails and 12,000+
   SendGrid events (Processed/Delivered/Open/Click/Bounce), ingested by an
   existing webhook that is not in this repo (`ProviderEventId` is unique). A
   second event receiver for the same SendGrid account would need to coexist with
   it, and the new jobs would be a parallel sender to the one the CRM app has.
3. **1,415 PartnerImport companies have already been emailed** and 1,405 are
   `Contacted`. "Not yet processed" has no field to key on today.
4. **The lead row has no email, no name and no size band.** Emails and names are on
   `crm.CrmCompanyContacts`; the only size signal is `CompanyScale`, which mixes two
   vocabularies (`MSME/Startup/SMB/Enterprise` and headcount bands `1-50`, `51-100`,
   `101-250`, ...).
5. **Contacts are keyed by company, not by domain.** There is no domain column
   anywhere: the domain comes from `Website` (863 missing). "Per domain" grouping
   would be a computed value, and 39 websites are shared by 83 rows.
6. **Reports are keyed by the CRM `Id`**, not by domain, so two leads at one domain
   have two separate reports.
7. **No suppression list exists.** Bounces are only events in
   `CrmCompanyEmailEvents` (245). Nothing records unsubscribes or spam complaints.
8. **16 reports have a score of 91-100**, so a "skip over 90" gate would drop
   reports that exist; `no_gap` rows (865) have no report at all. And 1,031
   PartnerImport rows score 91-100 on their own.
9. **No visit tracking.** UTM data exists only at form submission (14 website leads,
   3 with UTMs). "Site visits from UTMs" would need new capture, or a join through
   these 14 rows, which is too few to use.
10. **Three lead concepts overlap:** `dbo.CRM_Scrapper` (scraped companies, 14,024),
    `crm.CrmLeads` (inbound and partner leads, 113) and `dbo.Leads` (website form,
    14). Only the first has reports.
11. **Data hygiene:** 21.8% of contacts use free-mail; 3 emails are duplicated across
    contact rows; 18 company names repeat; 8 contacts have no job title.
12. **`CsvImport` rows (727) have no scores, no reports and no scrape status**, but
    do have contacts, so they would pass a source/rep filter and fail every scan step.
