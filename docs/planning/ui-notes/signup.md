# Signup — UI and UX review

**Route:** `/signup`  
**Review date:** 1 October 2026  
**Current state observed:** blank registration form, company section at top; the
browser accessibility tree exposed the complete form contract. Visual review at
785×827 in dark theme. The previous implementation was also checked at desktop,
tablet and mobile widths in both themes.  
**Audience:** the first company Owner and separate Admin; they prepare and jointly
confirm initial company registration. This screen does not itself grant access to
the proposed team or make a store ready for business.

## Current flow

The page is one long registration form with six progress items:
**Company → Regional settings → First store → People → Review → Confirmation.**
The first four sections are on this page. Review and joint confirmation appear
when the user reaches those states. Company code, store code and staff codes are
previewed and allocated when saved by default; each can be overridden. Country,
time zone, currency, language and number format are shown as dropdowns with one
supported option. State choices are Jharkhand and Bihar, with city suggestions for
each and a custom city option. Store start requires choosing New or Existing.
Owner and Admin provide separate email and temporary password credentials.
Manager/team entries are optional and are not granted access by registration.

## What is working and should carry forward

- The page identifies the job simply and the six-item progress panel explains
  where the user is. The current section follows scroll position and the first
  four items offer direct navigation.
- The page separates the legal company, regional values, store and people details
  into named cards. Address inputs have room for a complete address.
- New and Existing store choices are distinct, explicit radio cards. The
  software-name field appears only for Existing.
- Generated codes avoid making every user invent a value; custom codes remain
  possible for an established company or store.
- The review step precedes submission. Owner and Admin confirmation remain
  separate, with named status shown after authentication.
- The UI distinguishes proposed people from people who have a login or store
  access. Registration does not signal that stock or selling is ready.
- The screen uses the existing warm surfaces and semantic theme tokens; it has
  been exercised in dark and light modes at 1440, 1366, 768 and 375 pixels.

## Candidate changes

| ID | Priority | Observation | Candidate change / acceptance note |
| --- | --- | --- | --- |
| SU-01 | High | The blank first visit presents PAN, GSTIN, address and two initial people in one lengthy page. The task and commitment are large for a first-time user. | Keep the single-page structure, but consider stronger section orientation and clear “what you need before you start” guidance. Test whether it reduces omissions without obscuring the complete form. |
| SU-02 | High | Company legal fields are visually peers, although company name and state guide later values. | Give Company name and State a clearer first reading path. Keep GSTIN and PAN together and explain their relationship near validation. |
| SU-03 | High | The single-option region dropdowns look like choices even though the user cannot choose another supported value. | Decide whether these should remain dropdowns to communicate configurable company defaults or be read-only selected settings with a clear “currently supported” label. Keep this consistent with later company setup. |
| SU-04 | Medium | Language and number format each display a dropdown but currently use the same `en-IN` setting. | Explain that they are separate display preferences, or combine them as “Language and format” if they always move together. Confirm product intent before changing the data model. |
| SU-05 | Medium | The State list is limited to Jharkhand and Bihar; the city control is unavailable until a state is selected. | Add a concise hint that this alpha supports two states. Make the dependency evident before the city control is opened, and retain the custom-city path. |
| SU-06 | Medium | Company code, store code and staff code previews sit among user-entered fields; “Use my own code” repeats. | Visually group each generated code with its entity. Give each override toggle a unique accessible name, and state that the preview becomes final on save. |
| SU-07 | Medium | Staff numbering is shared across Owner, Admin and team, but users see each person in a separate section. | Keep one sequence and show each preview beside the person. Verify that editing/removing a proposed person never silently changes another saved code. |
| SU-08 | Medium | The business consequences of New versus Existing determine later stock setup, but the consequence currently fits in one short sentence. | Keep the two-card choice. Make the different next step plain in the option text and subsequent setup checklist; avoid putting the SOH workflow inside signup. |
| SU-09 | Medium | Owner and Admin are two separate people, credentials and later confirmation steps. Their cards are visually similar. | Visually pair them under People while retaining distinct Owner/Admin headings and password controls. Tell each person where the second confirmation happens. |
| SU-10 | Medium | Temporary passwords are collected together. The screen gives a concise rule and show/hide controls. | Keep separate credentials and visibility controls. Clarify password rules next to the password fields and ensure errors identify which person’s password needs attention. |
| SU-11 | Medium | Team membership is optional and the note says access is configured later. | Preserve the optional grouping and explicit “login and store access are set up after registration” wording. Do not style proposed role as an active assignment. |
| SU-12 | Low | Progress tiles remain at the top while the user scrolls a long form. | Decide after screen comparison whether a compact sticky progress marker is useful. Avoid duplicating the full six-tile panel or consuming mobile viewport height without a demonstrated need. |
| SU-13 | Low | “Sign in” remains available while the registration form is blank or partially completed. | Preserve a clear sign-in route, and consider a small unsaved-changes warning only if browser navigation testing shows accidental loss is common. Do not interrupt ordinary navigation by default. |

The user's readability and concise-information feedback refers specifically to
the [Review setup screen](signup-review.md), recorded separately.

## Follow-up inspection — 1 October 2026

**Live state:** `/signup` in the browser, dark theme, about 725×994. The page is
the registration form in **Edit setup**, not a blank first visit and not Review
or Confirmation. No button was clicked, no field was edited, and nothing was
saved.

The form shows Company, Regional settings, First store and People, then
**Review setup**. Progress reads **Step 1 of 6 · Company**. Review and
Confirmation are disabled. An **Edit setup** card sits above Company, with
**Back to confirmation**.

Every entered field is blank: company, legal name, state, PAN, GSTIN, addresses,
store, city, New/Existing, and both people. Regional dropdowns still show the
single supported defaults. Code previews still read CMP-0001, STR-0001,
EMP-0001 and EMP-0002. The session draft in this tab matches that: only the
regional defaults are filled. Passwords are not in the draft.

The Edit setup card already says both people must confirm again and that new
temporary passwords are required, although no change is visible. The saved
setup is not on this page. Opening this form does not ask for a password. The
Owner password appears later, on Review, and only when a revision is saved.
See [Confirmation](signup-confirmation.md).

| ID | Priority | Observation | Candidate change / acceptance note |
| --- | --- | --- | --- |
| SU-14 | High | Edit setup opened with no previous company, store or people values. The session draft cannot refill them. | Back from confirmation should show the last non-secret details already entered. Password fields may stay empty. If those details are not available in this browser, say so on the form instead of presenting a blank setup. Do not invent or fetch the saved registration while navigating. |
| SU-15 | Medium | Progress says step 1 of 6 and disables Confirmation, while the banner already demands new passwords and a fresh confirmation. | Keep one visible way back to confirmation. Treat the reconfirmation warning and new passwords as part of saving a change, not as the cost of looking at the form. |

## Open product decisions

1. Should the one-value regional settings remain dropdowns, or become selected
   read-only defaults until more countries, currencies, zones or formats are
   supported?
2. Should Language and number format remain independently configurable even
   though this alpha stores them under the same locale value?

## Review and implementation boundary

These are candidate changes for the final consolidated UI pass. No Signup code
change is authorized by this note. Keep registration identity, code allocation,
independent confirmation, access grants, stock readiness and server validation
contracts intact during any later visual work. Once every screen has a note,
merge duplicate candidates across screens, resolve the open decisions, order
work by shared components and risk, and present the combined count before
implementation.

## Implementation pass — 1 October 2026

Owner decisions: regional settings are shown as read-only defaults ("Only option in
this release"); Language and number format are one line because both use `en-IN`.

Implemented in `frontend/src/pages/Signup.tsx` / `Signup.css`: SU-01 (before-you-start
checklist), SU-02 (name and state first, PAN/GSTIN hints), SU-03/SU-04 (read-only
regional values), SU-05 (supported-state hint, city disabled until state), SU-06
(code rows with unique toggle names), SU-08 (next step on New/Existing), SU-09/SU-10
(paired Owner/Admin cards, password rule beside each field, distinct show/hide
names), SU-11 (numbered proposed team rows), SU-14 (explanation when this browser
has no saved details), SU-15 (password and reconfirmation warnings moved to the
save). Progress marks filled sections with a check and becomes a compact marker row
at phone width. SR-01 and SC-01/SC-02 are covered in their own notes' screens.
Deferred: SU-07 behaviour unchanged (one staff sequence), SU-12 sticky progress,
SU-13 unsaved-changes warning.
