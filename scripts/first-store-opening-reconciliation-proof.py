"""Read the canonical accepted opening before commercial browser proof proceeds."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    spec = importlib.util.spec_from_file_location("opening_read_proof", ROOT / "scripts/first-store-onboarding-proof.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The positively verified rehearsal helper is unavailable.")
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    claim, record = proof.initialise(), proof.load()
    for who in ("owner", "admin"):
        if claim.summary[who]["email"] != record[who]["email"] or not record[who]["email"].endswith(".example.test"):
            raise RuntimeError("The current fictional proof registration differs; preserved.")
    from core.kernel_models import DocumentHead
    from core.tenancy import tenant_context
    from masters.models import Store
    from ptmapper.goods_models import GoodsPt
    from ptmapper.soh_models import SohImport
    from ptmapper.soh_services import opening_reconciliation
    from stockledger.goods_acceptance import line_progress
    from stockledger.goods_models import AcceptanceSession

    with tenant_context(claim.tenant_id):
        site = Store.objects.get(pk=claim.first_store_id, code="FIRST")
        source = SohImport.objects.get(pk=record["browser_bootstrap"]["source_id"], site=site,
            source_name="fictional-opening-soh.xlsx")
        opening = opening_reconciliation(site)
        if not opening["passed"] or opening["quantity"] != 3 or opening["source_count"] != 1:
            raise RuntimeError("Fictional opening is incomplete: " + "; ".join(opening["reasons"]))
        progress, version_ids = [], []
        for batch in source.batches.select_related("manifest").all():
            pt = GoodsPt.objects.get(manifest_version_id=batch.manifest.approved_version_id)
            head = DocumentHead.objects.get(document_id=pt.document_id, state="official")
            if head.live_version is None:
                raise RuntimeError("The linked fictional PT has no official version.")
            progress.extend(line_progress(head.live_version))
            version_ids.append(head.live_version.pk)
        accepted = sum(row["accepted_qty"] for row in progress)
        remaining = sum(row["remaining_qty"] for row in progress)
        if accepted != 3 or remaining != 0:
            raise RuntimeError("The canonical acceptance does not account for the three fictional units.")
        sessions = list(AcceptanceSession.objects.filter(source_version_id__in=version_ids).values("id", "state"))
        if len(sessions) != 1 or sessions[0]["state"] != "completed":
            raise RuntimeError("The existing fictional acceptance session must be completed through the ordinary UI.")
        public = {**opening, "synthetic_only": True, "source_id": str(source.pk), "accepted_qty": accepted,
            "remaining_qty": remaining, "source_hash": source.source_hash,
            "acceptance_session_id": str(sessions[0]["id"]), "acceptance_session_state": sessions[0]["state"]}
        evidence = ROOT / ".local/first-store-opening-reconciliation.json"
        descriptor, temporary = tempfile.mkstemp(prefix="opening-reconciliation-", dir=evidence.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(public, stream, indent=2, sort_keys=True)
            os.replace(temporary, evidence)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print("Canonical fictional opening is official and all 3 units have independent physical acceptance; no business state was written.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 -- generated proof credentials never enter tracebacks.
        print(str(error) if isinstance(error, RuntimeError) else f"Opening read proof failed ({type(error).__name__}); preserved for inspection.")
        raise SystemExit(1) from None
