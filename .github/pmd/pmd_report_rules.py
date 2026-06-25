"""Shared PMD report rule metadata."""
from __future__ import annotations

RULE_META: dict[str, dict[str, str]] = {
    "CyclomaticComplexity": {
        "label": "branching complexity",
        "why": "Many conditional branches make all paths harder to reason about and test.",
        "fix": "Split the method or move branching behind smaller collaborators.",
        "severity": "high",
    },
    "CognitiveComplexity": {
        "label": "reader complexity",
        "why": "Nested control flow makes the code harder to scan and safely change.",
        "fix": "Flatten with guard clauses and extract focused methods.",
        "severity": "high",
    },
    "NPathComplexity": {
        "label": "execution path explosion",
        "why": "The method has too many possible execution paths.",
        "fix": "Extract independent decision steps and reduce nested combinations.",
        "severity": "critical",
    },
    "CouplingBetweenObjects": {
        "label": "class coupling",
        "why": "The class knows about too many other classes.",
        "fix": "Introduce a boundary, facade, or smaller collaborator.",
        "severity": "medium",
    },
    "ExcessiveParameterList": {
        "label": "too many parameters",
        "why": "Wide method signatures are easy to call incorrectly.",
        "fix": "Group related values into a request or value object.",
        "severity": "low",
    },
    "GodClass": {
        "label": "responsibility overload",
        "why": "One class is carrying too many responsibilities.",
        "fix": "Split cohesive behavior into separate classes.",
        "severity": "critical",
    },
    "NcssCount": {
        "label": "oversized method or class",
        "why": "The implementation size is above the configured threshold.",
        "fix": "Extract smaller methods or classes.",
        "severity": "medium",
    },
    "ExcessivePublicCount": {
        "label": "too many public members",
        "why": "Large public surfaces weaken encapsulation.",
        "fix": "Hide implementation details and narrow the public API.",
        "severity": "low",
    },
}

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
