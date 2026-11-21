"""CLI integration for versioned model/chain selection and bound research evidence."""

import json
from pathlib import Path

from . import (
    device_chains,
    model_selection,
    selection_research,
    target_selection,
    selection_vocabulary,
)


def run(db, args):
    request = json.loads(Path(args.requirement).read_text(encoding="utf-8-sig"))
    is_chain = args.capability_action == "select-chains"
    target_mode = request.get("domain") == "engineering_targets"
    selector = (
        target_selection
        if target_mode
        else (device_chains if is_chain else model_selection)
    )
    options = (args.solver, args.version)

    def query(brief):
        selected = selector.select(db, request, *options, brief=brief)
        if target_mode:
            selected["selection_level"] = "chain" if is_chain else "model"
            if is_chain:
                selected["solve_order"] = target_selection.solve_order(
                    selected["stages"], selected["transfers"]
                )
        elif not is_chain and request["domain"] == "phase_change":
            chain = device_chains.select(db, request, *options, brief=True)
            if chain["status"] != "no_match":
                selected["equipment_chain"] = chain
        return selected

    # The gap set is independent of display verbosity, including when equally ranked
    # implementations have different requirements.
    if args.emit_gaps or args.with_evidence or args.research_cache:
        full = query(False)
        enriched = selection_research.attach(
            full,
            request,
            args.capability_action,
            *options,
            emit_gaps=args.emit_gaps,
            with_evidence=args.with_evidence,
            research_cache=args.research_cache,
            public_tokens=selection_vocabulary.public_tokens(db),
        )
        if args.brief:
            result = query(True)
            result.update(
                {
                    k: v
                    for k, v in enriched.items()
                    if k
                    not in {
                        "candidates",
                        "other_roles",
                        "sources",
                        "candidate_count",
                        "equipment_chain",
                        "engineering_plans",
                        "support_review",
                    }
                }
            )
            return result
        return enriched
    return query(args.brief)
