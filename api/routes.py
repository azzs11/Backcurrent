"""
All API endpoints. Three main ones once fully built out:
  POST /batch/run          — kick off a recovery run on the current payment batch
  GET  /audit/{payment_id} — full decision trail for a single payment
  GET  /report             — end-of-batch summary with ₹ recovered by decline reason

/health is here too — useful for deployment health checks and a quick sanity
test that the server started correctly.
"""

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/health")
def health_check():
    """Quick check that the server is up and the db initialised without errors."""
    return {"status": "ok"}


@router.post("/batch/run")
def run_batch():
    """
    Triggers a full recovery run on the current batch of failed payments.
    Runs detect → diagnose → decide → execute for each payment in sequence,
    with stopping rules applied before every execution step.

    Returns a summary of what was attempted and the immediate outcomes.
    Full details are in /report after the run completes.
    """
    # coming in Phase 4 once the agent layers are built
    raise HTTPException(status_code=501, detail="Not implemented yet — coming in Phase 4")


@router.get("/audit/{payment_id}")
def get_audit_trail(payment_id: str):
    """
    Returns the full decision chain for a single payment — every step the agent
    took, what it decided, why, and which layer (deterministic vs llm) made the call.
    """
    # coming in Phase 6
    raise HTTPException(status_code=501, detail="Not implemented yet — coming in Phase 6")


@router.get("/report")
def get_report():
    """
    End-of-batch summary: ₹ recovered, recovery rate, breakdown by decline reason,
    and the unresolved cases with plain-English explanations of why they couldn't be fixed.
    """
    # coming in Phase 7
    raise HTTPException(status_code=501, detail="Not implemented yet — coming in Phase 7")
