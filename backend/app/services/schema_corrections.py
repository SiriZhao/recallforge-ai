"""Apply bounded leaf corrections without allowing a repair to drop knowledge."""
from copy import deepcopy


def apply_corrections(original, errors, patch):
    if not isinstance(original, dict) or not isinstance(patch, dict):
        raise ValueError("Schema correction requires objects")
    allowed={tuple(error["loc"]) for error in errors}
    corrections=patch.get("corrections")
    if not isinstance(corrections,list) or not corrections:
        raise ValueError("No leaf corrections supplied")
    result=deepcopy(original)
    for correction in corrections:
        path=tuple(correction.get("path",[]))
        if not path or path not in allowed or "value" not in correction:
            raise ValueError("Repair attempted to change an unreported field")
        target=result
        for component in path[:-1]:
            target=target[component]
        target[path[-1]]=correction["value"]
    return result
