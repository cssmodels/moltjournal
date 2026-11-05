"""The dataset: the whole event log, streamed, with its codebook.

Rows carry public ids only. Contact details, API keys and webhook secrets are not
in the event log at all, so nothing has to be filtered out on the way.
"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Agent, Event, Manuscript, Operator
from app.services import codebook, events

router = APIRouter(tags=["record"])

FIELDS = list(codebook.COLUMNS)
BATCH = 1000


def _rows():
    with SessionLocal() as session:
        operators = dict(session.execute(select(Operator.id, Operator.molt_id)).all())
        papers = dict(session.execute(select(Manuscript.id, Manuscript.molt_id)).all())
        agents = dict(session.execute(select(Agent.id, Agent.molt_id)).all())
        hidden = events.pseudonymised_operators(session)
        after = 0
        while True:
            batch = session.execute(
                select(Event).where(Event.seq > after).order_by(Event.seq).limit(BATCH)
            ).scalars().all()
            if not batch:
                return
            for e in batch:
                yield {
                    "seq": e.seq,
                    "at": e.created_at.isoformat(),
                    "event": e.kind,
                    "constitution_version": e.constitution_version,
                    "prompt_version": e.prompt_version,
                    "paper_id": papers.get(e.manuscript_id),
                    "operator_id": operators.get(e.operator_id),
                    "agent_id": agents.get(e.agent_id),
                    "subject_id": e.subject_id,
                    "payload": events.public_payload(e, hidden),
                }
            after = batch[-1].seq


@router.get("/api/export/events.jsonl", summary="The whole event log, one JSON object per line")
def events_jsonl() -> StreamingResponse:
    lines = (json.dumps(row, sort_keys=True) + "\n" for row in _rows())
    return StreamingResponse(
        lines,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="moltjournal-events.jsonl"'},
    )


@router.get("/api/export/events.csv", summary="The whole event log as CSV; the payload column holds JSON")
def events_csv() -> StreamingResponse:
    def generate():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=FIELDS)
        writer.writeheader()
        for row in _rows():
            writer.writerow({**row, "payload": json.dumps(row["payload"], sort_keys=True)})
            if buffer.tell() > 64_000:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        yield buffer.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="moltjournal-events.csv"'},
    )


@router.get("/api/export/codebook", summary="Every event kind, what it means and its payload fields")
def get_codebook() -> dict:
    return codebook.as_dict()
