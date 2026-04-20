"""Report generator — spec-vs-code divergence table."""

from datetime import datetime, timezone
from collections import defaultdict
import html as _html


def _sev_color(s):
    return {"high": "#ef4444", "medium": "#f59e0b", "low": "#71717a"}.get(s or "low", "#71717a")

def _sev_border(s):
    return {"high": "#7f1d1d", "medium": "#78350f", "low": "#27272a"}.get(s or "low", "#27272a")

def _score_label(score):
    if score < 25: return "Low drift", "#22c55e"
    if score < 50: return "Moderate drift", "#f59e0b"
    if score < 75: return "Significant drift", "#f97316"
    return "Severe drift", "#ef4444"


def _fallback_summary(pairs_analyzed, issues_closed, unlinked_count, period_days,
                      high_count, medium_count, drift_count, gap_count, rework_days, findings,
                      pr_desc_mode=False):
    b = lambda t: f"<span style='color:#fff;font-weight:700'>{t}</span>"
    lines = []
    thin_skipped = issues_closed - pairs_analyzed - unlinked_count
    pair_label = "PR" if pr_desc_mode else "issue-PR"
    parts = [f"Analysed {b(pairs_analyzed)} {pair_label} {'pair' if pairs_analyzed == 1 else 'pairs'} from the last {period_days} days"]
    if pr_desc_mode and pairs_analyzed > 0:
        parts.append("using PR descriptions as specifications")
    if thin_skipped > 0: parts.append(f"{thin_skipped} skipped (thin spec)")
    if unlinked_count > 0: parts.append(f"{unlinked_count} closed without a linked PR")
    lines.append(". ".join(parts) + ".")
    if not findings:
        lines.append("No specification drift or gaps detected.")
    else:
        fp = []
        if drift_count: fp.append(f"{b(drift_count)} spec {'divergence' if drift_count == 1 else 'divergences'}")
        if gap_count: fp.append(f"{b(gap_count)} spec {'gap' if gap_count == 1 else 'gaps'}")
        sp = []
        if high_count: sp.append(f"{b(high_count)} high")
        if medium_count: sp.append(f"{b(medium_count)} medium")
        lines.append(f"Found {' and '.join(fp)}" + (f" — {', '.join(sp)} severity" if sp else "") + ".")
    if rework_days > 0:
        lines.append(f"Estimated {b(str(rework_days) + ' engineer-hours')} lost to rework.")
    return "<br><br>".join(lines)


def _linkify(text: str) -> str:
    """Linkify @usernames to GitHub profiles."""
    import re
    return re.sub(
        r'@([A-Za-z0-9][A-Za-z0-9-]{0,38})',
        r'<a href="https://github.com/\1" target="_blank" style="color:#6366f1;text-decoration:none;">@\1</a>',
        text
    )


def _format_exec_summary(text: str) -> str:
    """Format the LLM exec summary: sentence-per-line, highlight issue/PR refs."""
    import re
    text = text.strip().replace("**", "").replace("\n", " ")
    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+', text)
    formatted = []
    for s in sentences:
        if not s.strip():
            continue
        # Highlight #NNN issue/PR references
        s = re.sub(r'(#\d+)', r'<span style="color:#e4e4e7;font-weight:600">\1</span>', s)
        # Highlight setting names (ALL_CAPS_WITH_UNDERSCORES)
        s = re.sub(r'\b([A-Z][A-Z_]{3,})\b', r'<code style="font-size:12px;background:#1f1f23;padding:1px 5px;border-radius:3px;color:#a5b4fc">\1</code>', s)
        formatted.append(f'<span style="display:block;margin-bottom:10px;">{s}</span>')
    return "".join(formatted)


def generate_report(repo_data: dict, analysis: dict) -> str:
    owner       = repo_data["owner"]
    repo        = repo_data["repo"]
    repo_url    = repo_data["repo_url"]
    period_days = repo_data["period_days"]
    description = repo_data.get("description", "")
    unlinked    = repo_data.get("unlinked_issues", [])
    oversized   = repo_data.get("oversized_prs", [])

    # Detect PR-description-first mode (no linked GitHub Issues)
    pairs = repo_data.get("pairs") or []
    _pr_desc_mode = any(p.get("issue", {}).get("_pr_description_mode") for p in pairs)

    findings      = analysis.get("findings", [])
    summary       = analysis.get("summary", {})
    exec_summary  = analysis.get("exec_summary", "")
    rework_days   = analysis.get("total_rework_hours", 0.0)
    _tok          = analysis.get("tokens", {})
    _tok_input    = _tok.get("input", 0) if _tok else 0
    _tok_output   = _tok.get("output", 0) if _tok else 0
    _tok_total    = (_tok_input + _tok_output) if _tok else 0
    _tok_est      = _tok.get("estimated", False) if _tok else False

    total_findings  = summary.get("total_findings", 0)
    high_count      = summary.get("high", 0)
    medium_count    = summary.get("medium", 0)
    low_count       = summary.get("low", 0)
    drift_count     = summary.get("drift", 0)
    gap_count       = summary.get("gap", 0)
    pairs_analyzed  = summary.get("pairs_analyzed", 0)
    issues_closed   = summary.get("issues_closed", 0)
    unlinked_count  = summary.get("issues_unlinked", 0)

    scan_date = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    score = 0 if pairs_analyzed == 0 else min(100, round(
        (high_count * 3 + medium_count * 1.5 + low_count * 0.5) / max(pairs_analyzed, 1) * 20
        + (unlinked_count / max(issues_closed, 1)) * 30
    ))
    score_label, score_color = _score_label(score)

    headline_html = (
        _format_exec_summary(exec_summary)
        if exec_summary else
        _fallback_summary(pairs_analyzed, issues_closed, unlinked_count, period_days,
                          high_count, medium_count, drift_count, gap_count, rework_days, findings,
                          pr_desc_mode=_pr_desc_mode)
    )

    # ── Build Slack permalink lookup: issue_number -> list[str] ───────────────
    slack_permalinks_by_issue: dict[int, list[str]] = {}
    for pair in repo_data.get("pairs", []):
        links = pair.get("slack_permalinks", [])
        if links:
            inum = (pair.get("issue") or {}).get("number")
            if inum:
                slack_permalinks_by_issue[inum] = links

    # ── Build the findings table ───────────────────────────────────────────────
    # Group by issue, one row per finding
    grouped: dict = defaultdict(list)
    issue_meta: dict = {}
    for f in findings:
        inum = f["issue_number"]
        grouped[inum].append(f)
        if inum not in issue_meta:
            issue_meta[inum] = f

    all_cards = []
    for issue_num, group in grouped.items():
        meta = issue_meta[issue_num]
        rw   = meta.get("rework_hours", 0)

        sev_rank = {"high": 0, "medium": 1, "low": 2, None: 3}
        top_sev  = min((f.get("severity") for f in group), key=lambda s: sev_rank.get(s, 3))

        # Card header
        pr_author_html = (
            f'<span style="color:#27272a">&middot;</span>'
            f'<a href="https://github.com/{meta["pr_author"]}" target="_blank" style="font-size:11px;color:#3f3f46;text-decoration:none;">@{meta["pr_author"]}</a>'
            if meta.get("pr_author") else ""
        )
        rework_html = (
            f'<span style="color:#27272a">&middot;</span>'
            f'<span style="font-size:11px;color:#a16207;font-weight:600">{rw}h rework</span>'
            if rw > 0 else ""
        )
        caught = meta.get("caught_in_review", False)
        caught_html = (
            '<span style="color:#22c55e;font-size:11px;">✓ caught in review</span>'
            if caught else ""
        )

        _is_spec_scan = (issue_num == 0 and meta.get("source_platform") == "doc")
        _issue_label  = meta.get('issue_title', '')[:70]
        _issue_href   = meta.get('issue_url', '')
        _pr_num       = meta.get('pr_number')
        _pr_href      = meta.get('pr_url', '')
        _header_issue_html = (
            f'<span style="font-size:13px;font-weight:600;color:#e4e4e7;">{_issue_label}</span>'
            if _is_spec_scan else
            f'<a href="{_issue_href}" target="_blank" style="font-size:13px;font-weight:600;color:#e4e4e7;text-decoration:underline;text-underline-offset:3px;text-decoration-color:#3f3f46;">#{issue_num} {_issue_label}</a>'
        )
        _header_pr_html = (
            "" if _is_spec_scan or not _pr_href else
            f'<a href="{_pr_href}" target="_blank" style="font-size:11px;color:#6366f1;text-decoration:underline;text-underline-offset:2px;">PR #{_pr_num}</a>'
        )
        card_header = f"""
<div style="padding:10px 16px;background:#111113;border-bottom:1px solid #1f1f23;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
  {_header_issue_html}
  <span style="margin-left:auto;display:flex;align-items:center;gap:8px;">
    {caught_html}
    {rework_html}
    {_header_pr_html}
    {pr_author_html}
  </span>
</div>"""

        finding_rows = ""

        for f in group[:5]:  # max 5 findings per issue
            spec_said  = _linkify(_html.escape(f.get("spec_said") or f.get("requirement", "")))
            code_does  = _linkify(_html.escape(f.get("code_does") or f.get("note", "")))
            req_source     = f.get("requirement_source", "")
            context_source = f.get("context_source", "")
            code_evid  = f.get("code_evidence", "")
            evid_url   = f.get("code_evidence_url", "")
            code_snippet = (f.get("code_snippet") or "").strip()
            # Strip git diff +/- prefixes if the snippet is in diff format
            if code_snippet and all(l == "" or l[0] in ("+", "-", " ", "@") for l in code_snippet.splitlines()):
                code_snippet = "\n".join(
                    l[1:] if l and l[0] in ("+", "-", " ") else l
                    for l in code_snippet.splitlines()
                ).strip()
            fsev       = f.get("severity") or "low"
            fstatus    = f.get("status", "gap")
            frework    = f.get("rework_hours", 0)
            source_platform = f.get("source_platform", "")
            source_author   = f.get("source_author", "")
            source_author_url = f.get("source_author_url", "")
            code_fix = (f.get("code_fix") or "").strip()
            doc_fix  = (f.get("doc_fix") or "").strip()

            # Source quote under spec — suppress if it duplicates spec_said text
            spec_said_raw = (f.get("spec_said") or f.get("requirement", "")).strip().lower()
            req_source_cmp = req_source.strip().lower()
            if req_source_cmp and (req_source_cmp in spec_said_raw or spec_said_raw in req_source_cmp):
                req_source = ""

            safe_req_source = _html.escape(req_source[:160]) if req_source else ""
            source_html = (
                f'<div style="margin-top:7px;font-size:11px;color:#3f3f46;font-style:italic;'
                f'border-left:2px solid #27272a;padding-left:8px;line-height:1.5;">'
                f'&ldquo;{safe_req_source}&rdquo;</div>'
                if safe_req_source else ""
            )

            # Source pill link — standalone clickable link to the origin ticket / issue
            # For github_issue, source_author_url is the author profile — use issue_url instead
            _src_link_url = (
                f.get("issue_url", "") if source_platform == "github_issue"
                else source_author_url or f.get("issue_url", "")
            )
            _src_issue_num = f.get("issue_number", "")
            _src_pr_num    = f.get("pr_number", "")
            _src_pr_url    = f.get("pr_url", "")
            _safe_author = _html.escape(source_author) if source_author else ""
            _src_label = {
                "jira":         f"{_safe_author} · Jira" if _safe_author else "View Jira ticket",
                "linear":       f"{_safe_author} · Linear" if _safe_author else "View Linear issue",
                "slack":        "",  # Slack attribution chip already shows name + link
                "github_issue": f"Issue #{_src_issue_num}" if _src_issue_num else "View issue",
                "doc":          "View document",
            }.get(source_platform, f"Issue #{_src_issue_num}" if _src_issue_num else "")
            # Issue pill — Expectation cell (skip for Slack — covered by attribution chip)
            if _src_link_url and _src_label:
                source_pill_html = (
                    f'<div style="margin-top:8px;">'
                    f'<a href="{_html.escape(_src_link_url)}" target="_blank" '
                    f'style="display:inline-flex;align-items:center;gap:5px;font-size:11px;'
                    f'color:#6366f1;font-family:monospace;background:#09090b;'
                    f'padding:4px 9px;border-radius:5px;text-decoration:none;">'
                    f'&#128279; {_src_label}</a>'
                    f'</div>'
                )
            else:
                source_pill_html = ""

            # PR pill — Finding cell
            pr_pill_html = (
                f'<div style="margin-top:8px;">'
                f'<a href="{_html.escape(_src_pr_url)}" target="_blank" '
                f'style="display:inline-flex;align-items:center;gap:5px;font-size:11px;'
                f'color:#6366f1;font-family:monospace;background:#09090b;'
                f'padding:4px 9px;border-radius:5px;text-decoration:none;">'
                f'&#128279; PR #{_src_pr_num}</a>'
                f'</div>'
                if _src_pr_url and _src_pr_num else ""
            )

            # Attribution chip — Slack, Jira, or Linear sourced findings
            if source_platform == "slack" and source_author:
                safe_author = _html.escape(source_author)
                safe_url    = _html.escape(source_author_url) if source_author_url else ""
                _slack_icon = (
                    '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0">'
                    '<path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165'
                    'a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52'
                    ' 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1'
                    '-2.521-2.522v-6.313zm2.521-10.123a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1'
                    ' 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1'
                    ' 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834'
                    'a2.528 2.528 0 0 1 2.522-2.521h6.312zm10.122 2.521a2.528 2.528 0 0 1 2.522-2.521'
                    'A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.268 0'
                    'a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1'
                    ' 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zm-2.523 10.122a2.528 2.528 0 0 1'
                    ' 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52z'
                    'm0-1.268a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313'
                    'A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>'
                )
                _inner = f'{_slack_icon}{safe_author} via Slack'
                source_author_html = (
                    f'<div style="margin-top:6px;">'
                    + (
                        f'<a href="{safe_url}" target="_blank" style="display:inline-flex;align-items:center;'
                        f'gap:5px;font-size:10px;font-weight:500;color:#7c3aed;background:#1e1030;'
                        f'border:1px solid #4c1d95;border-radius:4px;padding:3px 8px;text-decoration:none;">'
                        f'{_inner}</a>'
                        if safe_url else
                        f'<span style="display:inline-flex;align-items:center;gap:5px;font-size:10px;'
                        f'font-weight:500;color:#7c3aed;background:#1e1030;border:1px solid #4c1d95;'
                        f'border-radius:4px;padding:3px 8px;">{_inner}</span>'
                    )
                    + '</div>'
                )
            elif source_platform == "jira" and source_author_url:
                safe_author = _html.escape(source_author) if source_author else "Jira"
                safe_url    = _html.escape(source_author_url)
                _jira_icon = (
                    '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0">'
                    '<path d="M11.571 11.429 6.429 6.286A.857.857 0 0 0 5.143 7.571l4.536 4.536-4.536 4.536'
                    'a.857.857 0 0 0 1.286 1.286l5.142-5.143a.857.857 0 0 0 0-1.357zm5.143 0L11.571 6.286'
                    'a.857.857 0 0 0-1.285 1.285l4.535 4.536-4.535 4.536a.857.857 0 0 0 1.285 1.286'
                    'l5.143-5.143a.857.857 0 0 0 0-1.357z"/></svg>'
                )
                _inner = f'{_jira_icon}{safe_author} via Jira'
                source_author_html = (
                    f'<div style="margin-top:6px;">'
                    f'<a href="{safe_url}" target="_blank" style="display:inline-flex;align-items:center;'
                    f'gap:5px;font-size:10px;font-weight:500;color:#60a5fa;background:#0c1a2e;'
                    f'border:1px solid #1e40af;border-radius:4px;padding:3px 8px;text-decoration:none;">'
                    f'{_inner}</a>'
                    f'</div>'
                )
            elif source_platform == "linear" and source_author_url:
                safe_author = _html.escape(source_author) if source_author else "Linear"
                safe_url    = _html.escape(source_author_url)
                _linear_icon = (
                    '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0">'
                    '<path d="M3.14 14.218 9.782 20.86a10.232 10.232 0 0 1-6.642-6.642zm-.852-2.342'
                    ' 9.836 9.836a10.252 10.252 0 0 1-1.438.206L2.896 13.143a10.25 10.25 0 0 1 .392-1.267z'
                    'M3.67 9.044l11.286 11.286c-.36.174-.73.33-1.108.462L3.208 10.152a10.16 10.16 0 0 1'
                    ' .462-1.108zm1.512-2.341 12.115 12.115a10.267 10.267 0 0 1-.836.698L4.346 7.403'
                    'c.224-.24.46-.471.708-.69l.128-.01zm2.2-1.699 11.614 11.614A10.22 10.22 0 0 1'
                    ' 13.85 21.13L2.87 10.15A10.22 10.22 0 0 1 7.382 5.004zm4.424-1.133'
                    'c3.892.358 7.375 3.84 7.733 7.733L11.806 3.87z"/></svg>'
                )
                _inner = f'{_linear_icon}{safe_author} via Linear'
                source_author_html = (
                    f'<div style="margin-top:6px;">'
                    f'<a href="{safe_url}" target="_blank" style="display:inline-flex;align-items:center;'
                    f'gap:5px;font-size:10px;font-weight:500;color:#a78bfa;background:#120d1e;'
                    f'border:1px solid #5b21b6;border-radius:4px;padding:3px 8px;text-decoration:none;">'
                    f'{_inner}</a>'
                    f'</div>'
                )
            else:
                source_author_html = ""

            source_html += source_author_html

            # Uploaded file attribution — show filename when requirement came from an extra doc
            safe_ctx_src = _html.escape((context_source or "").strip())
            context_source_html = (
                f'<div style="margin-top:6px;display:inline-flex;align-items:center;gap:4px;'
                f'font-size:10px;color:#52525b;background:#18181b;border:1px solid #27272a;'
                f'border-radius:4px;padding:2px 6px;font-family:monospace;">'
                f'<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
                f'style="flex-shrink:0;opacity:0.7">'
                f'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
                f'<polyline points="14 2 14 8 20 8"/></svg>'
                f'{safe_ctx_src}</div>'
                if safe_ctx_src else ""
            )

            # Slack link — only show when this specific finding came from Slack
            slack_links = slack_permalinks_by_issue.get(issue_num, []) if source_platform == "slack" else []
            if slack_links:
                slack_link_items = "".join(
                    f'<a href="{_html.escape(url)}" target="_blank" '
                    f'style="display:inline-flex;align-items:center;gap:4px;font-size:11px;'
                    f'color:#4a154b;background:#f4ede4;border-radius:4px;padding:2px 7px;'
                    f'text-decoration:none;font-weight:500;">'
                    f'<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">'
                    f'<path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zM6.313 15.165a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zM8.834 6.313a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zM17.688 8.834a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.165 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zM15.165 17.688a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/>'
                    f'</svg>View in Slack</a>'
                    for url in slack_links[:1]
                )
                slack_link_html = f'<div style="margin-top:8px;">{slack_link_items}</div>'
            else:
                slack_link_html = ""

            # Code snippet block — skip if too short to be meaningful (e.g. "} />")
            if code_snippet and len(code_snippet.strip()) >= 15:
                safe_snippet = _html.escape(code_snippet[:400])
                snippet_html = (
                    f'<pre style="margin-top:10px;font-size:11px;font-family:monospace;'
                    f'background:#0a0a0c;border:1px solid #1f1f23;border-radius:6px;'
                    f'padding:10px 12px;color:#71717a;overflow-x:auto;white-space:pre-wrap;'
                    f'word-break:break-all;line-height:1.6;">{safe_snippet}</pre>'
                )
            else:
                snippet_html = ""

            if evid_url and code_evid:
                evid_html = (
                    f'<a href="{evid_url}" target="_blank" style="margin-top:8px;display:inline-flex;'
                    f'align-items:center;gap:5px;font-size:11px;color:#6366f1;font-family:monospace;'
                    f'background:#09090b;padding:4px 9px;border-radius:5px;text-decoration:none;'
                    f'word-break:break-all;">&#128279; {code_evid[:80]}</a>'
                )
            elif code_evid:
                evid_html = (
                    f'<div style="margin-top:8px;font-size:11px;color:#52525b;font-family:monospace;'
                    f'background:#09090b;padding:4px 9px;border-radius:5px;word-break:break-all;">'
                    f'{code_evid[:80]}</div>'
                )
            else:
                # No evidence — show a muted placeholder so the column isn't empty
                evid_html = (
                    f'<div style="margin-top:8px;font-size:11px;color:#3f3f46;font-style:italic;">'
                    f'no line-level evidence</div>'
                    if fstatus == "drift" else ""
                )

            # Fix suggestion block — distinct card style, separate from git-diff blocks above
            if code_fix:
                _fix_code_block = (
                    f'<div style="display:flex;gap:8px;align-items:flex-start;">'
                    f'<span style="font-size:10px;margin-top:1px;flex-shrink:0;">&#128295;</span>'
                    f'<div>'
                    f'<div style="font-size:9px;font-weight:700;letter-spacing:0.6px;'
                    f'color:#f59e0b;text-transform:uppercase;margin-bottom:3px;">Fix the code</div>'
                    f'<div style="font-size:12px;color:#fef3c7;line-height:1.6;">'
                    f'{_html.escape(code_fix)}</div>'
                    f'</div>'
                    f'</div>'
                )
                _fix_doc_block = (
                    f'<div style="display:flex;gap:8px;align-items:flex-start;'
                    f'margin-top:10px;padding-top:10px;border-top:1px dashed #1f2a3a;">'
                    f'<span style="font-size:10px;margin-top:1px;flex-shrink:0;">&#128196;</span>'
                    f'<div>'
                    f'<div style="font-size:9px;font-weight:700;letter-spacing:0.6px;'
                    f'color:#818cf8;text-transform:uppercase;margin-bottom:3px;">Or update the spec</div>'
                    f'<div style="font-size:12px;color:#e0e7ff;line-height:1.6;">'
                    f'{_html.escape(doc_fix)}</div>'
                    f'</div>'
                    f'</div>'
                ) if doc_fix and fstatus == "drift" else ""
                fix_html = (
                    f'<div style="margin-top:12px;background:#0d1117;border:1px solid #2a2a3a;'
                    f'border-radius:6px;padding:10px 12px;">'
                    f'<div style="font-size:9px;font-weight:700;letter-spacing:0.6px;color:#52525b;'
                    f'text-transform:uppercase;margin-bottom:8px;">How to resolve</div>'
                    f'{_fix_code_block}'
                    f'{_fix_doc_block}'
                    f'</div>'
                )
            else:
                fix_html = ""

            # Rework days inside findings column
            rework_finding_html = (
                f'<div style="margin-top:10px;display:inline-flex;align-items:center;gap:6px;'
                f'background:#1a1200;border:1px solid #3d2e00;border-radius:6px;padding:4px 10px;">'
                f'<span style="font-size:11px;color:#a16207;font-weight:600;">rewrite took {frework}h</span>'
                f'</div>'
                if frework > 0 else ""
            )

            # When found cell
            wf = f.get("when_found") or {}
            wf_status = wf.get("status", "unknown")
            if wf_status == "found":
                h        = wf.get("hours_later", 0)
                fu_url   = wf.get("follow_up_url", "")
                fu_num   = wf.get("follow_up_number", "")
                fu_title = (wf.get("follow_up_title") or "")[:45]
                when_html = (
                    f'<div style="font-size:13px;color:#f59e0b;font-weight:700;">+{h}h later</div>'
                    f'<a href="{fu_url}" target="_blank" style="font-size:11px;color:#52525b;'
                    f'text-decoration:none;display:block;margin-top:5px;line-height:1.4;">'
                    f'#{fu_num} {fu_title}</a>'
                )
            elif wf_status == "not_found":
                hours_ago = wf.get("hours_since_merge", 0)
                when_html = (
                    f'<div style="font-size:13px;color:#ef4444;font-weight:700;">Not yet fixed</div>'
                    f'<div style="font-size:11px;color:#52525b;margin-top:5px;">Unresolved since {hours_ago}h ago</div>'
                )
            else:
                # unknown = no merged_at date available
                when_html = '<div style="font-size:11px;color:#3f3f46;">Not tracked</div>'

            # For gaps with no code evidence, link to the issue as the reference
            issue_link_html = ""
            if fstatus == "gap" and not code_evid:
                issue_url = f.get("issue_url", "")
                issue_num_f = f.get("issue_number", "")
                pr_url_f = f.get("pr_url", "")
                pr_num_f = f.get("pr_number", "")
                _links = []
                if issue_url and issue_num_f:
                    _links.append(
                        f'<a href="{issue_url}" target="_blank" style="font-size:11px;color:#6366f1;'
                        f'background:#09090b;padding:4px 9px;border-radius:5px;text-decoration:underline;text-underline-offset:2px;">'
                        f'&#128279; issue #{issue_num_f}</a>'
                    )
                if pr_url_f and pr_num_f:
                    _links.append(
                        f'<a href="{pr_url_f}" target="_blank" style="font-size:11px;color:#6366f1;'
                        f'background:#09090b;padding:4px 9px;border-radius:5px;text-decoration:underline;text-underline-offset:2px;">'
                        f'&#128279; PR #{pr_num_f}</a>'
                    )
                if _links:
                    issue_link_html = f'<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">{"".join(_links)}</div>'

            is_drift = fstatus == "drift"
            row_border_color = "#ef4444" if is_drift else "#3b82f6"
            badge_bg         = "#2d0a0a" if is_drift else "#0a1628"
            badge_color      = "#ef4444" if is_drift else "#60a5fa"
            badge_label      = "DRIFT" if is_drift else "GAP"
            badge_desc       = "contradicts spec" if is_drift else "missing from code"
            exp_cell_bg      = "#120a0a" if is_drift else "#0a0e18"

            fstatus_badge = (
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'background:{badge_bg};border:1px solid {badge_color}44;'
                f'border-radius:4px;padding:2px 8px;margin-right:6px;">'
                f'<span style="font-size:9px;font-weight:800;letter-spacing:0.8px;color:{badge_color};">{badge_label}</span>'
                f'<span style="font-size:9px;color:{badge_color}99;">{badge_desc}</span>'
                f'</span>'
            )
            finding_text_bg = "#1c0a0a" if is_drift else "#0a0e1c"
            fix_row = (
                f'<tr><td colspan="3" style="padding:0 16px 14px;border-top:none;">{fix_html}</td></tr>'
                if fix_html else ""
            )
            finding_rows += f"""
<tr style="border-top:1px solid #1a1a1e;">
  <td colspan="3" style="padding:8px 16px 4px;">
    {fstatus_badge}
  </td>
</tr>
<tr>
  <td style="padding:4px 16px 14px;vertical-align:top;border-right:1px solid #1a1a1e;width:36%;">
    <div style="font-size:13px;color:#e4e4e7;line-height:1.65;background:#0d1f0e;
                border-left:3px solid #22c55e;padding:8px 10px;border-radius:0 4px 4px 0;">{spec_said}</div>
    {source_html}
    {source_pill_html}
    {context_source_html}
    {slack_link_html}
  </td>
  <td style="padding:4px 16px 14px;vertical-align:top;border-right:1px solid #1a1a1e;width:40%;">
    <div style="font-size:13px;color:#a1a1aa;line-height:1.65;background:{finding_text_bg};
                border-left:3px solid {row_border_color};padding:8px 10px;border-radius:0 4px 4px 0;">{code_does}</div>
    {snippet_html}
    {evid_html}
    {pr_pill_html}
    {rework_finding_html}
  </td>
  <td style="padding:4px 14px 14px;vertical-align:top;width:24%;">
    {when_html}
  </td>
</tr>
{fix_row}"""

        all_cards.append(f"""
<div class="card" style="padding-top:20px;padding-bottom:4px;background:#09090b;break-inside:avoid;page-break-inside:avoid;">
<div style="border:1px solid #1f1f23;border-top:2px solid {_sev_border(top_sev)};border-radius:12px;overflow:hidden;background:#0d0d0f;">
  {card_header}
  <table style="width:100%;border-collapse:collapse;">
    <thead>
      <tr style="background:#111113;border-bottom:1px solid #1f1f23;">
        <th style="padding:8px 16px;text-align:left;font-size:10px;font-weight:700;color:#3f3f46;letter-spacing:0.7px;text-transform:uppercase;width:36%;">Expectation</th>
        <th style="padding:8px 16px;text-align:left;font-size:10px;font-weight:700;color:#3f3f46;letter-spacing:0.7px;text-transform:uppercase;width:40%;border-left:1px solid #1a1a1e;">Finding</th>
        <th style="padding:8px 14px;text-align:left;font-size:10px;font-weight:700;color:#3f3f46;letter-spacing:0.7px;text-transform:uppercase;width:24%;border-left:1px solid #1a1a1e;">When Found</th>
      </tr>
    </thead>
    <tbody>{finding_rows}</tbody>
  </table>
</div>
</div>""")

    # ── Assemble main content ──────────────────────────────────────────────────
    if pairs_analyzed == 0:
        main_content = """
<div style="text-align:center;padding:48px 24px;background:#111113;border:1px solid #1f1f23;border-radius:12px;">
  <div style="font-size:15px;font-weight:600;color:#a1a1aa;margin-bottom:8px;">No analysable pairs found</div>
  <div style="font-size:13px;color:#52525b;line-height:1.7;">
    No merged pull requests with descriptions were found in this window.<br>
    Try a longer analysis window, or add descriptions to your PRs explaining what they are intended to do.
  </div>
</div>"""
    elif total_findings == 0:
        main_content = """
<div style="text-align:center;padding:48px 24px;background:#111113;border:1px solid #1f1f23;border-radius:12px;">
  <div style="font-size:28px;margin-bottom:12px;">&#10003;</div>
  <div style="font-size:16px;font-weight:600;color:#e4e4e7;margin-bottom:8px;">No drift detected</div>
  <div style="font-size:14px;color:#52525b;line-height:1.6;">All analysed issues had implementations that matched their stated requirements.</div>
</div>"""
    else:
        main_content = "".join(all_cards)

    # ── Unlinked issues ────────────────────────────────────────────────────────
    unlinked_html = ""
    if unlinked:
        _ul = unlinked[:10]
        items = "".join(
            f'<a href="{u["url"]}" target="_blank" style="font-size:13px;color:#a1a1aa;text-decoration:none;display:flex;align-items:center;gap:8px;'
            f'padding:8px 0;{"border-bottom:1px solid #18181b;" if i < len(_ul)-1 else ""}">'
            f'<span style="color:#6366f1;font-size:11px;font-family:monospace;flex-shrink:0;">#{u["number"]}</span>'
            f'<span>{u["title"][:70]}</span>'
            f'<span style="margin-left:auto;font-size:10px;color:#3f3f46;">↗</span>'
            f'</a>'
            for i, u in enumerate(_ul)
        )
        unlinked_html = f"""
<div class="no-break" style="padding-top:20px;background:#09090b;break-before:page;page-break-before:always;">
  <div style="font-size:11px;font-weight:700;letter-spacing:0.8px;color:#52525b;text-transform:uppercase;margin-bottom:14px;">{len(unlinked)} ISSUE{"S" if len(unlinked) != 1 else ""} WITH NO LINKED PR</div>
  <div style="background:#111113;border:1px solid #1f1f23;border-radius:10px;padding:4px 16px;">{items}</div>
  <div style="font-size:12px;color:#3f3f46;margin-top:8px;">Closed without a traceable pull request — unclear what code change addressed them.</div>
</div>"""

    # ── Oversized PRs ──────────────────────────────────────────────────────────
    oversized_html = ""
    if oversized:
        _op = oversized[:5]
        items = "".join(
            f'<a href="{p["url"]}" target="_blank" style="font-size:13px;color:#52525b;text-decoration:none;'
            f'padding:8px 0;{"border-bottom:1px solid #18181b;" if i < len(_op)-1 else ""}display:block;">'
            f'PR #{p["number"]} {p["title"][:55]} <span style="color:#3f3f46">({p["total_changes"]:,} lines)</span></a>'
            for i, p in enumerate(_op)
        )
        oversized_html = f"""
<div class="no-break" style="margin-top:24px;">
  <div style="font-size:11px;font-weight:700;letter-spacing:0.8px;color:#52525b;text-transform:uppercase;margin-bottom:14px;">{len(oversized)} OVERSIZED PR{"S" if len(oversized) != 1 else ""} SKIPPED</div>
  <div style="background:#111113;border:1px solid #1f1f23;border-radius:10px;padding:4px 16px;">{items}</div>
  <div style="font-size:12px;color:#3f3f46;margin-top:8px;">PRs over 2,000 changed lines were excluded from AI analysis.</div>
</div>"""

    stat_tiles = "".join(
        f'<div style="background:#111113;border:1px solid #1f1f23;border-radius:10px;padding:14px 16px;">'
        f'<div style="font-size:20px;font-weight:700;color:{c}">{v}</div>'
        f'<div style="font-size:11px;color:#52525b;margin-top:3px;">{l}</div></div>'
        for v, l, c in [
            (high_count,   "high severity",  "#ef4444"),
            (medium_count, "medium severity","#f59e0b"),
            (drift_count,  "spec divergences","#818cf8"),
            (gap_count,    "spec gaps",      "#60a5fa"),
        ]
    )

    rework_block = ""
    if rework_days > 0:
        rework_block = f"""
<div style="background:#111113;border:1px solid #27272a;border-left:3px solid #f59e0b;border-radius:10px;padding:16px 20px;margin-bottom:40px;display:flex;align-items:center;gap:24px;">
  <div>
    <div style="font-size:28px;font-weight:800;color:#f59e0b;">{rework_days}h</div>
    <div style="font-size:11px;color:#52525b;margin-top:2px;letter-spacing:0.3px;">engineer-hours lost to rework</div>
  </div>
  <div style="width:1px;height:36px;background:#27272a;"></div>
  <div>
    <div style="font-size:24px;font-weight:700;color:#a1a1aa;">~${round(rework_days * 100):,}</div>
    <div style="font-size:11px;color:#52525b;margin-top:2px;letter-spacing:0.3px;">estimated cost ($100/hr blended rate)</div>
  </div>
</div>"""
    else:
        rework_block = '<div style="margin-bottom:40px;"></div>'

    desc_html = (
        f'<div style="font-size:14px;color:#52525b;margin-bottom:28px;">{description}</div>'
        if description else '<div style="margin-bottom:28px;"></div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>1Spur Scanner &mdash; {owner}/{repo}</title>
<link rel="icon" type="image/svg+xml" href="/static/icon.svg"/>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:#09090b;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:0 0 80px;}}
  table{{border-radius:12px;overflow:hidden;}}
  @page{{margin:0;}}
  @media print{{
    .no-print{{display:none!important;}}
    body{{padding:0!important;}}
    .card{{page-break-inside:avoid;break-inside:avoid;}}
    .no-break{{page-break-inside:avoid;break-inside:avoid;}}
    tr{{page-break-inside:avoid;break-inside:avoid;}}
    thead{{display:table-header-group;}}
  }}
  *{{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important;}}
</style>
</head>
<body>

<div class="no-print" style="border-bottom:1px solid #18181b;padding:14px 32px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:#09090b;z-index:10;">
  <div style="font-size:16px;font-weight:800;letter-spacing:-0.3px;">Spec<span style="color:#6366f1">Drift</span> <a href="https://1spur.com" target="_blank" style="font-weight:300;color:#3f3f46;font-size:9px;letter-spacing:0.3px;text-decoration:none;">by 1spur AI</a></div>
  <div style="display:flex;gap:10px;align-items:center;">
    <span style="font-size:12px;color:#3f3f46">{scan_date}</span>
    <button onclick="window.print()" style="background:#18181b;border:1px solid #27272a;color:#a1a1aa;font-size:12px;padding:6px 14px;border-radius:8px;cursor:pointer;">Save PDF</button>
    <a href="/" style="background:#18181b;border:1px solid #27272a;color:#a1a1aa;font-size:12px;padding:6px 14px;border-radius:8px;text-decoration:none;">New scan</a>
  </div>
</div>

<div style="max-width:860px;margin:0 auto;padding:48px 24px 0;">

  <div style="margin-bottom:6px;">
    <a href="{repo_url}" target="_blank" style="font-size:22px;font-weight:800;color:#fff;text-decoration:none;letter-spacing:-0.5px;">{owner}/{repo}</a>
  </div>
  {desc_html}

  <div style="background:#111113;border:1px solid #1f1f23;border-radius:14px;padding:16px 24px;margin-bottom:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
    <div style="font-size:14px;font-weight:600;color:{score_color};">{score_label}</div>
    <span style="color:#27272a">&middot;</span>
    <div style="font-size:12px;color:#52525b;">Last {period_days} days &middot; {pairs_analyzed} {"PR" if _pr_desc_mode else "issue-PR"} pairs analysed{"" if not _tok_total else f" &middot; <span style='color:#3f3f46'>{'~' if _tok_est else ''}{_tok_total:,} tokens used ({'~' if _tok_est else ''}{_tok_input:,} in · {'~' if _tok_est else ''}{_tok_output:,} out)</span>"}</div>
  </div>

  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px;">{stat_tiles}</div>

  {rework_block}

  {main_content}
  {unlinked_html}
  {oversized_html}

  <div style="margin-top:56px;padding-top:24px;border-top:1px solid #18181b;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
    <div style="font-size:12px;color:#3f3f46;">Generated by <a href="https://1spur.com" target="_blank" style="color:#52525b;text-decoration:none;">1spur AI</a> &middot; <a href="/" style="color:#52525b;text-decoration:none;">Run another scan</a></div>
    <div style="font-size:11px;color:#27272a;">Scan: {scan_date} &middot; Window: {period_days}d &middot; PRs: {pairs_analyzed}{"" if _pr_desc_mode else f"/{issues_closed}"}</div>
  </div>

</div>
</body>
</html>"""
