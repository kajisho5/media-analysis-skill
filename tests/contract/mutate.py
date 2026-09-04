"""Apply the mutation vocabulary of tests/contract/cases/*.json to a live contract document. The fixtures never
carry a full contract (that would be a second, hand-maintained copy); they describe a change to the real one."""
from __future__ import annotations

import copy
from typing import Any, Dict, List


def apply(contract: Dict[str, Any], mutations: List[Dict[str, Any]]) -> Dict[str, Any]:
    doc = copy.deepcopy(contract)
    for m in mutations:
        op = m["op"]
        if op == "set":
            if isinstance(m["value"], str) and m["value"].startswith("APPEND:"):
                doc[m["path"]] = list(doc[m["path"]]) + [m["value"][len("APPEND:"):]]
            else:
                doc[m["path"]] = m["value"]
        elif op == "set_map":
            doc[m["path"]][m["key"]] = m["value"]
        elif op == "delete_tool":
            doc["tools"] = [t for t in doc["tools"] if t["tool_id"] != m["tool_id"]]
        elif op == "add_tool":
            base = copy.deepcopy(doc["tools"][0])
            base.update({"tool_id": m["tool_id"], "kinds": m["kinds"], "output_observation_kinds": m["kinds"], "parameters": {k: {} for k in m["kinds"]}})
            doc["tools"].append(base)
        elif op == "set_tool_field":
            for t in doc["tools"]:
                if t["tool_id"] == m["tool_id"]:
                    t[m["field"]] = m["value"]
        elif op == "delete_schema_property":
            doc["schemas"][m["schema"]]["properties"].pop(m["property"])
        else:
            raise ValueError(f"unknown mutation op {op}")
    return doc
