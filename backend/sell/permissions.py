"""Who may work the counter (the `sell` section of the ratified access matrix).

| Role | sell | What it means here |
|---|---|---|
| Store manager / store staff | operate | bills, and reads back what they billed |
| Owner / brand manager / accounts | view | reads bills, never writes one |
| Admin (IT) | manage | everything |
| Warehouse / HO ops / data steward | none | no access at all |

One gate here is not `sell` at all - see `CanWorkIrnQueue`.

The rungs come from `Role.section_access` - the stored matrix, which an
administrator edits at runtime (#173) - so nothing here names a role. What is
*not* expressible on that ladder stays out of it: the manager's OK on an over-cap
discount is checked person by person inside the accept pipeline, because it asks
"could this named person have been standing at this counter", which is a question
about one human and one store rather than about a rung.
"""

from __future__ import annotations

from accounts.permissions import require_section
from accounts.sections import CAP_APPROVE, CAP_MANAGE, CAP_OPERATE, CAP_VIEW

#: Reading a bill back is `view`; billing is `operate`. One view carries both
#: because `GET`/`POST` on `/api/sell/sales` are the search and the till.
CanReadOrBill = require_section("sell", CAP_VIEW, write_minimum=CAP_OPERATE)

#: Reading one bill for reprint.
CanReadSales = require_section("sell", CAP_VIEW)

CanReadOrManagePolicy = require_section("sell", CAP_VIEW, write_minimum=CAP_MANAGE)

#: Pulling the till's dataset down (#179). `operate`, not `view`: it is not a
#: report about selling but the working copy a counter bills from, and it carries
#: this store's manager override PIN hashes. Somebody who may only read back
#: yesterday's bills has no use for it and should not hold it.
CanRunTill = require_section("sell", CAP_OPERATE)

#: The IRN queue (#187), and the one gate in this module that is not `sell`.
#:
#: Raising an IRN against a B2B bill is head office's statutory duty, not the
#: shop's - grill Q8 says so, and db-design says the queue "surfaces in an HO work
#: queue, not at the store". The rung has to match that audience, and `sell`
#: cannot express it: `sell: operate` is the *store*, which would put a GST filing
#: duty on a cashier's sidebar, and `sell: manage` is the IT administrator alone,
#: who holds no money at all on the ratified sheet.
#:
#: `money: manage` is exactly the two people who file the returns - Owner and
#: Accounts - so the gate follows the duty rather than the app the model happens
#: to live in. Nothing on the ratified sheet moves to make this true.
CanWorkIrnQueue = require_section("money", CAP_MANAGE)

#: The store's exception list (#188), and the second gate here that is not `sell`.
#:
#: The flags are what the Money section's day summary shows underneath the day by
#: tender - "here is what the day took, and here is what it left open" - so they
#: answer to the same section that screen does. `money: view` reads and
#: `money: operate` clears, which on the ratified sheet is the store itself
#: ("Expenses only (create)" is `operate`), the warehouse, Owner and Accounts.
#:
#: Not `sell`, for the reason the IRN queue is not: `sell: view` is held by the
#: brand manager, whose business a counter's exceptions are not, and no seeded
#: role reaches `sell: approve` at all - a rung nobody holds is a list nobody
#: could ever clear.
CanWorkStoreFlags = require_section("money", CAP_VIEW, write_minimum=CAP_OPERATE)

#: Moving a store's bill series onto a different machine (#189). `approve`, a rung
#: above the till itself: a handover leaves holes in a numbered money series that
#: somebody then has to fill from paper, and the cashier whose machine just died
#: is exactly the person who should be fetching a manager rather than deciding.
#: It is the same rung the counter's own manager-override PIN is built on, so the
#: person a cashier calls over for a discount is the person who can do this too.
CanHandOverTill = require_section("sell", CAP_APPROVE)
