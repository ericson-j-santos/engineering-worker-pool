"""Trusted acceptance cases for the real portfolio_bridge._property defect.

No candidate implementation is provided to the Ollama E2E.
"""
from __future__ import annotations

INSTRUCTION = (
    "Repair _property(properties, name) used by the TODO Global admission bridge. "
    "Return a string or empty string for invalid/missing data, never coerce numbers/containers. "
    "If properties or the selected property is not a dict, return empty. "
    "For type select, select must be a dict and name must be str; strip that string. "
    "For type url, url must be str; strip it. "
    "For type rich_text, rich_text must be a list of dict fragments. "
    "Use plain_text when it is a string, including empty string; when it is missing or None, "
    "require a text dict with string content. Any invalid fragment invalidates the whole field. "
    "Concatenate the strings in order and strip the concatenation. Unknown type returns empty. "
    "Do not raise exceptions on any JSON input. Do not change unrelated functions, imports or tests."
)


def cases():
    def case(data, expected):
        return {"args": [{"F": data}, "F"], "expected": expected}
    output = [
        case({"type": "select", "select": {"name": " PENDENTE "}}, "PENDENTE"),
        case({"type": "select", "select": ["invalid"]}, ""),
        case({"type": "url", "url": " https://github.com/a/b "}, "https://github.com/a/b"),
        case({"type": "url", "url": 123}, ""),
        case({"type": "rich_text", "rich_text": None}, ""),
        case({"type": "rich_text", "rich_text": [{"plain_text": " foo"}, {"text": {"content": "bar "}}]}, "foobar"),
        case({"type": "rich_text", "rich_text": [{"plain_text": 5}]}, ""),
        case({"type": "rich_text", "rich_text": [{"text": ["invalid"]}]}, ""),
    ]
    for data in [None, [], "invalid", 1, True, {}, {"type": "unknown"}]:
        output.append(case(data, ""))
    for selected in [None, "x", 1, True, {}, {"name": 1}, {"name": []}, {"name": ""}]:
        output.append(case({"type": "select", "select": selected}, ""))
    for value in [None, [], {}, True, "", "  "]:
        output.append(case({"type": "url", "url": value}, ""))
    for items in [[], "x", {}, 1, [1], [{"plain_text": None}], [{"text": {"content": 1}}],
                  [{"plain_text": "safe"}, None], [{"plain_text": "", "text": {"content": "not-used"}}]]:
        output.append(case({"type": "rich_text", "rich_text": items}, ""))
    output += [
        {"args": [None, "F"], "expected": ""},
        {"args": [[], "F"], "expected": ""},
        {"args": [{}, "F"], "expected": ""},
        case({"type": "rich_text", "rich_text": [{"plain_text": " ação"}, {"plain_text": " válida "}]}, "ação válida"),
    ]
    return output
