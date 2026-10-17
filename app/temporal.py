"""Temporal wiring: REST helpers (start/signal, fall back to direct calls when Temporal is off)
plus the in-process worker, so one container runs API + worker with one command."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from temporalio.client import Client
from temporalio.worker import Worker
from . import temporal_activities as acts
from .config import NIGHTLY_REVIEW_MIN, TASK_QUEUE, TEMPORAL_URL
from .temporal_workflows import IncidentInput, IncidentWorkflow, NightlyReviewWorkflow

_c = None


def enabled():
    return bool(TEMPORAL_URL)


def wfid(inc_id):
    return f"incident-{inc_id}"


async def client():
    global _c
    if _c is None:
        _c = await Client.connect(TEMPORAL_URL)
    return _c


async def start_incident(inc_id):
    await (await client()).start_workflow(IncidentWorkflow.run, IncidentInput(inc_id), id=wfid(inc_id), task_queue=TASK_QUEUE)


async def signal_decision(inc_id, name, args):
    """True if the signal reached a live workflow; False => the caller runs the tool directly."""
    if not enabled():
        return False
    try:
        await (await client()).get_workflow_handle(wfid(inc_id)).signal(name, args=args)
        return True
    except Exception:
        return False


async def _run_worker():
    c = await client()
    try:  # one durable nightly-review loop per deployment; already-running is fine
        await c.start_workflow(NightlyReviewWorkflow.run, NIGHTLY_REVIEW_MIN, id="dock-to-door-nightly-review", task_queue=TASK_QUEUE)
    except Exception:
        pass
    with ThreadPoolExecutor(max_workers=16) as ex:
        worker = Worker(c, task_queue=TASK_QUEUE, workflows=[IncidentWorkflow, NightlyReviewWorkflow],
                        activities=[acts.run_pipeline, acts.approve, acts.override, acts.outcome, acts.review, acts.review_all],
                        activity_executor=ex)
        await worker.run()


def start_worker_thread():
    def go():
        try:
            asyncio.run(_run_worker())
        except Exception as e:
            print(f"temporal worker stopped: {e}")
    threading.Thread(target=go, name="temporal-worker", daemon=True).start()
