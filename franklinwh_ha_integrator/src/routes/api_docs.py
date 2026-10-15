"""Docs viewer — serves `docs/*.md` rendered to HTML locally.

v0.5.4 (2026-08-07). Removes the app's dependency on GitHub for
in-UI doc links (repo is private, working branches drift from
main, HA add-on installs may have no internet at all).

Route: `GET /docs/{name}` — where `{name}` is a bare markdown
filename like `sd_decision_chain.md`. Path traversal is not
possible: we resolve the filename against a fixed
`REPO_ROOT/docs/` directory, reject anything that would resolve
outside it, and only accept files ending in `.md`.

Rendered HTML is wrapped in a minimal dark-theme styled page
matching the app's overall look. No JS, no external assets — the
page loads even in air-gapped installs.

`ETag` cache header is set from the file mtime + size so the
browser skips re-downloads. Doc content changes rarely; the app
is often reloaded."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

logger = logging.getLogger(__name__)
router = APIRouter(tags=["docs"])

# Resolve the docs directory. Two candidate locations:
#   - Sibling-to-src ("REPO_ROOT/docs") — dev / bare-metal.
#   - "/app/docs" — container mount from docker-compose.yml.
# First existing wins. If neither exists, `_DOCS_DIR` is set to the
# expected path anyway; requests will 404 with a clear message.
def _resolve_docs_dir() -> Path:
    candidates = [
        (Path(__file__).parent.parent.parent / "docs").resolve(),  # sibling to src/
        Path("/app/docs"),                                          # container mount
    ]
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    # Fall back to the first candidate so error messages are stable.
    return candidates[0]

_DOCS_DIR: Path = _resolve_docs_dir()


def _list_docs() -> list[str]:
    """All markdown files in the docs directory, sorted."""
    if not _DOCS_DIR.exists():
        return []
    return sorted(p.name for p in _DOCS_DIR.glob("*.md"))


def _render_markdown(source: str) -> str:
    """Render markdown source to HTML. Uses python-markdown with
    fenced_code, tables, toc extensions for GitHub-flavored parity."""
    try:
        import markdown as _md
    except ImportError:
        # Fallback for a container missing the dep — wrap in <pre>
        # rather than 500 the whole route.
        import html as _h
        return f"<pre style='white-space:pre-wrap'>{_h.escape(source)}</pre>"

    return _md.markdown(
        source,
        extensions=[
            "fenced_code",   # ```lang code fences
            "tables",        # GFM tables
            "toc",           # heading anchors
            "sane_lists",    # match GFM list quirks
            "nl2br",         # newlines → <br> like GFM
        ],
    )


# Minimal dark-theme HTML template. Zero external assets (no CDN, no
# webfonts) so it works in air-gapped installs.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — FHAI Docs</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0;
    background: #0b1220; color: #e5e7eb;
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 32px 40px 96px; }}
  header {{
    display: flex; justify-content: space-between; align-items: center;
    padding-bottom: 16px; border-bottom: 1px solid #1f2937; margin-bottom: 24px;
  }}
  header .breadcrumb {{ font-size: 13px; color: #6b7280; font-family: 'JetBrains Mono', 'Fira Code', monospace; }}
  header .breadcrumb a {{ color: #10b981; text-decoration: none; }}
  header .breadcrumb a:hover {{ text-decoration: underline; }}
  h1, h2, h3, h4, h5 {{ color: #f3f4f6; margin-top: 1.6em; margin-bottom: 0.4em; line-height: 1.25; }}
  h1 {{ font-size: 28px; padding-bottom: 8px; border-bottom: 1px solid #1f2937; }}
  h2 {{ font-size: 22px; padding-bottom: 6px; border-bottom: 1px solid #1f2937; }}
  h3 {{ font-size: 18px; }}
  a {{ color: #34d399; text-decoration: underline; text-decoration-color: rgba(52, 211, 153, 0.3); }}
  a:hover {{ text-decoration-color: rgba(52, 211, 153, 1); }}
  code {{
    background: #1f2937; padding: 2px 6px; border-radius: 4px;
    font: 13px/1.4 'JetBrains Mono', 'Fira Code', ui-monospace, monospace;
    color: #fbbf24;
  }}
  pre {{
    background: #111827; border: 1px solid #1f2937; padding: 14px 16px;
    border-radius: 8px; overflow-x: auto; margin: 16px 0;
  }}
  pre code {{ background: transparent; padding: 0; color: #e5e7eb; font-size: 13px; }}
  blockquote {{
    border-left: 3px solid #10b981; margin: 16px 0; padding: 4px 16px;
    background: rgba(16, 185, 129, 0.05); color: #d1d5db;
  }}
  blockquote p {{ margin: 8px 0; }}
  table {{ border-collapse: collapse; margin: 16px 0; width: 100%; font-size: 14px; }}
  th, td {{ border: 1px solid #1f2937; padding: 8px 12px; text-align: left; }}
  th {{ background: #111827; color: #f3f4f6; font-weight: 600; }}
  tr:nth-child(even) td {{ background: rgba(255,255,255,0.02); }}
  hr {{ border: none; border-top: 1px solid #1f2937; margin: 32px 0; }}
  ul, ol {{ padding-left: 28px; }}
  li {{ margin: 6px 0; }}
  strong {{ color: #f9fafb; }}
  em {{ color: #d1d5db; }}
  .doc-list {{ list-style: none; padding: 0; }}
  .doc-list li {{ padding: 8px 0; border-bottom: 1px solid #1f2937; }}
  .doc-list li:last-child {{ border-bottom: none; }}
  .doc-list a {{ font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 14px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span class="breadcrumb"><a href="/admin">← FranklinWH HA Integrator</a> &nbsp;/&nbsp; <a href="/docs">docs</a> &nbsp;/&nbsp; {name}</span>
  </header>
  {body}
</div>
</body>
</html>
"""


@router.get("/docs", response_class=HTMLResponse)
async def docs_index():
    """List all markdown docs available under the local docs viewer."""
    items = _list_docs()
    if not items:
        body = "<h1>Docs</h1><p>No documentation files found under <code>docs/</code>.</p>"
    else:
        rows = "\n".join(
            f'<li><a href="/docs/{name}">{name}</a></li>'
            for name in items
        )
        body = (
            "<h1>Documentation</h1>"
            f"<p>{len(items)} markdown files. Served locally by FHAI — no GitHub round-trip.</p>"
            f'<ul class="doc-list">{rows}</ul>'
        )
    html = _TEMPLATE.format(title="Docs", name="index", body=body)
    return HTMLResponse(content=html)


@router.get("/docs/{name}", response_class=HTMLResponse)
async def render_doc(name: str, request: Request):
    """Render a single markdown file to styled HTML.

    Security: only accept bare filenames ending in `.md` inside
    `DOCS_DIR`. Anything with a path separator or extension mismatch
    is rejected. `Path.resolve()` follow-up check ensures the
    resolved path is still inside `DOCS_DIR` (belt-and-braces against
    Unicode / URL-encoding tricks)."""
    # Basic guards — reject obvious traversal / bad extensions early.
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="invalid doc name")
    if not name.endswith(".md"):
        raise HTTPException(status_code=400, detail="only .md files supported")

    target = (_DOCS_DIR / name).resolve()
    # After resolution, must still be inside DOCS_DIR (blocks symlink / encoding tricks).
    try:
        target.relative_to(_DOCS_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes docs directory")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"doc not found: {name}")

    try:
        stat = target.stat()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"stat failed: {exc!r}")

    # ETag from mtime + size — cheap and stable. Browser sends
    # If-None-Match on subsequent loads; we can 304 without re-reading
    # + re-rendering.
    etag = f'W/"{hashlib.sha1(f"{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304)

    try:
        source = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"read failed: {exc!r}")

    body_html = _render_markdown(source)
    title = name.replace(".md", "").replace("_", " ").title()
    html = _TEMPLATE.format(title=title, name=name, body=body_html)
    resp = HTMLResponse(content=html)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp
