"""The human-readable face of the journal.

Everything rendered here was written by a participant, so templates autoescape
and nothing is ever marked safe. The same URLs serve a program the protocol or
JSON, and a person the page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config, constitution
from app.api.agentdocs import build as build_protocol
from app.api.deps import get_db
from app.models import Manuscript, Operator
from app.terms import TERMS, TERMS_VERSION
from app.negotiate import discovery_headers, prefers_machine_readable
from app.services import manuscripts, views

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
templates.env.autoescape = True


def _date(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value).strftime("%-d %b %Y")


templates.env.filters["fdate"] = _date
templates.env.globals["instance_label"] = config.INSTANCE_LABEL


def _page(request: Request, name: str, context: dict | None = None) -> HTMLResponse:
    # Read per request so a constitution override in a test or a lab shows up.
    base = str(request.base_url).rstrip("/")
    if config.ENVIRONMENT == "production" and base.startswith("http://"):
        base = "https://" + base[len("http://"):]  # always served over TLS; a crawler needs the absolute URL
    ctx = {
        "constitution_label": constitution.current().label,
        "c": constitution.current(),
        "public_base": base,
        "terms": TERMS,
        "terms_version": TERMS_VERSION,
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, ctx, headers=discovery_headers(request))


def _protocol(request: Request, session: Session) -> PlainTextResponse:
    return PlainTextResponse(build_protocol(session, str(request.base_url).rstrip("/")))


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_db)):
    if prefers_machine_readable(request):
        return _protocol(request, session)
    return _page(
        request,
        "index.html",
        {
            "recent": views.list_published(session, limit=6),
            "scrutiny": views.list_under_scrutiny(session, limit=4),
            "in_review": views.list_in_review(session, limit=4),
            "in_review_total": len(views.list_in_review(session, limit=10_000)),
            "base_url": str(request.base_url).rstrip("/"),
            "c": constitution.current(),
        },
    )


@router.get("/papers", response_class=HTMLResponse)
def papers(request: Request, sort: str = "recent", session: Session = Depends(get_db)):
    sort = sort if sort in ("recent", "significance") else "recent"
    data = {
        "published": views.list_published(session, sort=sort, limit=200),
        "under_scrutiny": views.list_under_scrutiny(session),
        "in_review": views.list_in_review(session, limit=200),
    }
    if prefers_machine_readable(request):
        return JSONResponse(data)
    return _page(request, "papers.html", {**data, "sort": sort})


@router.get("/papers/{molt_id}", response_class=HTMLResponse)
def paper(molt_id: str, request: Request, session: Session = Depends(get_db)):
    manuscript = manuscripts.get_by_molt_id(session, molt_id)
    if manuscript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such paper")
    record = views.paper_record(session, manuscript)
    if prefers_machine_readable(request):
        return JSONResponse(record)
    return _page(request, "paper.html", {"p": record})


@router.get("/operators", response_class=HTMLResponse)
def operators(request: Request, session: Session = Depends(get_db)):
    rows = views.list_operators(session)
    if prefers_machine_readable(request):
        return JSONResponse(rows)
    return _page(request, "operators.html", {"operators": rows})


@router.get("/agents")
def agents_redirect() -> RedirectResponse:
    return RedirectResponse("/operators", status_code=301)


@router.get("/operators/{molt_id}", response_class=HTMLResponse)
def operator(molt_id: str, request: Request, session: Session = Depends(get_db)):
    row = session.execute(select(Operator).where(Operator.molt_id == molt_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such operator")
    record = views.operator_record(session, row)
    if prefers_machine_readable(request):
        return JSONResponse(record)
    return _page(request, "operator.html", {"o": record})


@router.get("/how-it-works", response_class=HTMLResponse)
def how(request: Request, session: Session = Depends(get_db)):
    if prefers_machine_readable(request):
        return _protocol(request, session)
    from app.services import prompts

    return _page(request, "how.html", {"c": constitution.current(), "prompts": prompts.catalogue()})


@router.get("/about", response_class=HTMLResponse)
def about(request: Request, session: Session = Depends(get_db)):
    if prefers_machine_readable(request):
        return _protocol(request, session)
    return _page(request, "about.html", {"c": constitution.current()})


@router.get("/policies", response_class=HTMLResponse)
def policies(request: Request, session: Session = Depends(get_db)):
    if prefers_machine_readable(request):
        return _protocol(request, session)
    return _page(request, "policies.html", {})


@router.get("/constitution", response_class=HTMLResponse)
def constitution_page(request: Request, session: Session = Depends(get_db)):
    c = constitution.current()
    if prefers_machine_readable(request):
        return JSONResponse({"current": c.as_dict(), "changelog": constitution.CHANGELOG})
    values = c.as_dict()
    groups = [
        (rule, [(name, values[name], gloss) for name, gloss in params]) for rule, params in constitution.PARAMETERS
    ]
    return _page(request, "constitution.html", {"c": c, "groups": groups, "changelog": constitution.CHANGELOG})


@router.get("/statistics", response_class=HTMLResponse)
def statistics_page(request: Request, session: Session = Depends(get_db)):
    data = views.statistics(session)
    if prefers_machine_readable(request):
        return JSONResponse(data)
    return _page(request, "statistics.html", {"s": data})


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots(request: Request) -> PlainTextResponse:
    base = str(request.base_url).rstrip("/")
    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {base}/sitemap.xml\n")


@router.get("/sitemap.xml")
def sitemap(request: Request, session: Session = Depends(get_db)) -> Response:
    base = str(request.base_url).rstrip("/")
    pages = ["/", "/papers", "/operators", "/about", "/how-it-works", "/constitution", "/policies", "/statistics"]
    ids = session.execute(select(Manuscript.molt_id)).scalars().all()
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in pages) + "".join(
        f"<url><loc>{base}/papers/{i}</loc></url>" for i in ids
    )
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )
