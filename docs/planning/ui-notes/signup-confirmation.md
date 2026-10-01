# Signup — Confirmation

**Route/state:** `/signup`, after a setup has been saved and before both people
confirm. The live tab on 1 October 2026 was already on Edit setup, so this
confirmation view was not opened and no credential was entered.

**Purpose:** Owner and Admin each check the saved setup and confirm it with
their own email and temporary password.

## What the screen contains

From the current page contract, not from a fresh view of this state:

- Heading **Confirm company setup**, with **Back to setup** beside it.
- A short line that Owner and Admin each confirm with their own email and
  temporary password.
- Email, temporary password, and **Review my initial summary**.
- A summary, once available, plus each person’s confirmed or waiting status.
- After a successful credential check, a personal acknowledgement and
  **Confirm as Owner** or **Confirm as Admin**.

**Back to setup** only changes the page. It does not save. It refills the form
from the summary already held on the page, or leaves whatever is already in the
form. It does not load the saved registration. The public signup status does
not include that registration. In this browser the session draft is empty, and
the form **Back to setup** returns to is blank. See SU-14 in
[Signup](signup.md).

**Review my initial summary** is a different action. It sends the email and
temporary password and, only after they are accepted, shows the saved summary
and who has confirmed. A wrong password is refused. That check stays on this
action.

## Owner direction

On this screen, **Back to setup** should be a simple way back to the previous
populated form. No extra step, and no authentication just to go back. Password
fields may be empty on return, because temporary passwords are not kept in the
draft or the summary.

## Candidate changes

| ID | Priority | Observation | Candidate change / acceptance note |
| --- | --- | --- | --- |
| SC-01 | High | **Back to setup** exists, but the form it opened in this browser has none of the previous company, store or people details. | One visible **Back to setup** returns to those non-secret details. It does not ask for email or a password, and it does not save. |
| SC-02 | Medium | The same screen also asks for credentials before **Review my initial summary**. That is easy to read as the way back. | Keep credential review as the way to see the saved summary and confirmation status. Do not make it the way back to the form. |

## Security check when saving a revision

This is separate from navigation. Recorded here so a later UI pass does not
move it onto **Back to setup**.

- Going back, or returning with **Back to confirmation**, does not revise the
  saved setup.
- Saving a revision sends the current Owner temporary password with the new
  details. The proposed Owner is the only person who may revise. The Admin
  password is refused. A wrong password is refused.
- A saved revision clears both confirmations. Both people confirm the new
  summary again.
- Temporary passwords are not returned with the summary, so a revision needs
  new ones at save time. That request belongs next to the save, not on arrival
  at the form.
- Registration writes already require the existing CSRF and origin checks.

## Open question

If this browser has no draft of the previous setup, what should **Back to setup**
show? The saved registration is not part of the public signup status. I have
not chosen whether the screen should explain that, or whether you have another
copy of those details that should reappear.

No application change or save is authorized by this note.
