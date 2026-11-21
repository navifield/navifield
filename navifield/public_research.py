"""Dispatch outstanding catalog questions to a configured research agent."""

import hashlib
import http.client
import json
import os
from contextlib import closing
from functools import lru_cache
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import capability_kb


@lru_cache(maxsize=1)
def vocabulary():
    from . import physics_route, selection_vocabulary
    with closing(capability_kb.connect()) as db:
        words = set(selection_vocabulary.public_tokens(db))
        words.update(word for terms in physics_route.CATALOG.values() for word in terms)
        words.update(r[0] for r in db.execute("SELECT DISTINCT quantity FROM quantities"))
        versions = set(db.execute("SELECT DISTINCT solver, version FROM capabilities"))
    return frozenset(words), frozenset(versions)


def cache_directory():
    override = os.environ.get("NAVIFIELD_RESEARCH_CACHE")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "NaviField" / "public-research"


def public_gaps(analysis, answered):
    # Only catalog-owned questions qualify for unattended transfer. Raw customer
    # text, arbitrary structured identifiers, measurements and conditions stay local.
    if analysis.get("research_dispatch_disabled"):
        return []
    questions = {
        (plan["chain"], plan["solver"], plan["version"], quantity): question
        for plan in analysis["engineering_plans"]
        for quantity, question in plan["research_needs"].items()
    }
    result = []
    for gap in analysis["gaps"]:
        if gap["action"] != "research" or gap["id"] in answered:
            continue
        key = (gap.get("scope"), gap.get("solver"), gap.get("version"), gap["quantity"])
        question = questions.get(key)
        item = {k: gap[k] for k in ("id", "scope", "version", "quantity")}
        if question:
            item.update(solver=gap["solver"], question=question)
        else:
            # Generic interfaces and goals are caller-specified. Send only named
            # library quantities for a known product/version pair.
            words, versions = vocabulary()
            solver = gap["context"].get("solver")
            if (gap["kind"] not in {"unknown_quantity", "unknown_interface"} or
                    (solver, gap["version"]) not in versions or gap["quantity"] not in words):
                continue
            if gap["kind"] == "unknown_interface":
                if not all(gap.get(k) in words for k in ("source", "target")):
                    continue
                item.update(source=gap["source"], target=gap["target"])
            item.update(solver=solver, question=gap["question"])
        result.append(item)
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_findings(endpoint, packet):
    parts = urlsplit(endpoint)
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or not (parts.scheme == "https" or
                    (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("Expected an HTTPS agent endpoint or HTTP loopback")
    body = json.dumps(packet, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    request = urllib.request.Request(endpoint, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Idempotency-Key": hashlib.sha256(body).hexdigest(),
        "NaviField-Finding-Summary": f"single-line; max-chars={capability_kb.FINDING_SUMMARY_LIMIT}",
    })
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=8) as response:
        if response.status == 202:
            return None
        if response.status != 200:
            raise ValueError("Unexpected agent response")
        raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError("Oversized research response")
    return json.loads(raw)


def dispatch(endpoint, analysis, known, cache):
    answered = {f["gap_id"] for f in known if capability_kb.cache_eligible(f)}
    missing = public_gaps(analysis, answered)
    report = {"state": "local", "submitted": [], "received": [], "cached": []}
    if not missing:
        return [], report
    submitted = missing[:4]
    report["submitted"] = [g["id"] for g in submitted]
    if len(missing) > len(submitted):
        report["deferred"] = len(missing) - len(submitted)
    try:
        document = request_findings(endpoint, {"schema_version": 1, "gaps": submitted})
        if document is None:
            report["state"] = "pending"
            return [], report
        current = [g for g in analysis["gaps"] if g["id"] in report["submitted"]]
        capability_kb.bind_findings(current, document)
        findings = document["findings"]
        if len(findings) > len(current) or len({f["gap_id"] for f in findings}) != len(findings):
            raise ValueError("Duplicate or excessive findings")
        report["received"] = [f["gap_id"] for f in findings]
        report["cached"] = capability_kb.store_cache(cache, findings)
        if len(report["cached"]) == len(submitted):
            report["state"] = "answered"
        elif report["cached"]:
            report["state"] = "partial"
        else:
            report["state"] = "unresolved"
        return findings, report
    except capability_kb.InvalidFindingSummary:
        report.update(state="unavailable", reason="invalid_finding_summary",
                      summary_max_chars=capability_kb.FINDING_SUMMARY_LIMIT)
        return [], report
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        # Remote diagnostics can contain sensitive endpoint details. The local
        # query and pending engineering gaps are still useful if dispatch fails.
        report["state"] = "unavailable"
        return [], report
