"""Temporal workflows: the incident lifecycle, durable.

IncidentWorkflow: pipeline -> durable wait for dispatcher approve/override -> outcome -> review agent.
NightlyReviewWorkflow: the feedback agent loop (timer survives restarts; no continue-as-new at 1 timer/night)."""
from dataclasses import dataclass
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .temporal_activities import approve, outcome, override, review, review_all, run_pipeline

RETRY = RetryPolicy(maximum_attempts=3)
ACT = dict(start_to_close_timeout=timedelta(seconds=60), retry_policy=RETRY)
LONG = dict(start_to_close_timeout=timedelta(minutes=6), retry_policy=RETRY)


@dataclass
class IncidentInput:
    inc_id: int


@workflow.defn
class IncidentWorkflow:
    def __init__(self):
        self._inbox: list[dict] = []
        self._status = "starting"

    @workflow.run
    async def run(self, inp: IncidentInput) -> dict:
        await workflow.execute_activity(run_pipeline, inp, **LONG)
        self._status = "awaiting_dispatcher"  # durable wait: survives worker crashes, can hold for hours
        review_res = None
        while True:
            await workflow.wait_condition(lambda: bool(self._inbox))
            ev = self._inbox.pop(0)
            if ev["kind"] == "approve":
                res = await workflow.execute_activity(approve, args=[inp.inc_id, ev["did"]], **ACT)
                if res.get("ok"):
                    self._status = "awaiting_outcome"
            elif ev["kind"] == "override":
                await workflow.execute_activity(override, args=[inp.inc_id, ev["did"], ev["vehicle_id"], ev["reason"]], **ACT)
                self._status = "awaiting_outcome"
            elif ev["kind"] == "outcome":
                res = await workflow.execute_activity(outcome, args=[inp.inc_id, ev["did"]], **ACT)
                if res.get("ok"):
                    self._status = "reviewing"
                    review_res = await workflow.execute_activity(review, inp.inc_id, start_to_close_timeout=timedelta(minutes=3), retry_policy=RETRY)
                    self._status = "done"
                    break
        return {"incident_id": inp.inc_id, "status": self._status, "review": review_res}

    @workflow.signal
    def approve(self, did: int) -> None:
        self._inbox.append({"kind": "approve", "did": did})

    @workflow.signal
    def override(self, did: int, vehicle_id: str, reason: str = "") -> None:
        self._inbox.append({"kind": "override", "did": did, "vehicle_id": vehicle_id, "reason": reason})

    @workflow.signal
    def outcome(self, did: int) -> None:
        self._inbox.append({"kind": "outcome", "did": did})

    @workflow.query
    def status(self) -> str:
        return self._status


@workflow.defn
class NightlyReviewWorkflow:
    @workflow.run
    async def run(self, interval_min: int) -> None:
        while True:
            await workflow.sleep(timedelta(minutes=max(1, interval_min)))
            await workflow.execute_activity(review_all, start_to_close_timeout=timedelta(minutes=5), retry_policy=RETRY)
