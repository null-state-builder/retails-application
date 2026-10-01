# Signup — Review setup

**Route/state:** `/signup`, `#signup-review`, before saving the proposed setup.  
**Review date:** 1 October 2026  
**Evidence:** user-provided screenshot of the populated QA review summary.  
**Purpose:** let the user check entered information, correct a section and
understand the next confirmation step.

## User direction

Improve readability and UI/UX. Show precise information needed for review;
remove unnecessary explanations. Record notes now and apply changes during the
agreed combined screen pass.

## Candidate change

| ID | Priority | Observation | Proposed outcome |
| --- | --- | --- | --- |
| SR-01 | Medium | The summary combines several values in long lines, repeats the company name/code, mixes entered data with permission explanations and exposes expanded background text. | Make the summary easy to scan through concise labelled values, clear section grouping and a short explanation of the next action. |

## Details for the coordinated UI pass

- Separate Company, Regional settings, Store and People into readable groups;
  place each Edit action with its group rather than relying only on the top row.
- Avoid the repeated heading `KDPS (KDPS)`. Show the company name once and its
  code as a secondary labelled value. Label PAN and GSTIN individually rather
  than appending identifiers to other values.
- Present people's name, email, role and code as distinct values. Keep the
  proposed manager status explicit without dense dot-separated lines.
- Replace technical regional strings with readable labels; retain the stored
  values where needed for verification without letting them dominate the view.
- Keep one concise next-step message: Owner and Admin each confirm the saved
  setup. Preserve the essential warning that proposed team access and store
  readiness are completed afterward.
- Put detailed permission explanations and subsequent setup background behind
  optional, initially collapsed help. Avoid repeating them in the summary.
- Use consistent spacing, line wrapping and contrast. Keep the primary save
  action clear and distinguish reviewing from saving or completing registration.

## This pass

Review setup was not opened. The live form had no entered values, and
**Review setup** was not clicked, so no validation message and no save ran.
The earlier review note still stands.

When this form is a revision, Review setup adds **Saved Owner password** above
**Save and continue to confirmation**. That check belongs to the save, as
recorded on [Confirmation](signup-confirmation.md). It should not appear merely
because the person went back to the form.

## Acceptance and boundary

The user can identify and check every entered value, find its Edit action and
understand who confirms next without reading long explanatory paragraphs.
Check desktop and mobile wrapping and keyboard access in the final UI pass.
Preserve all entered data, credential privacy and independent confirmation of
the exact saved revision. This note authorizes no application change or save.
