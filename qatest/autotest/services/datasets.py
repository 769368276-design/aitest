from itertools import product
import re


def parse_scalar(s):
    try:
        t = str(s).strip()
    except Exception:
        return s
    if t == "":
        return ""
    if t.lower() == "true":
        return True
    if t.lower() == "false":
        return False
    try:
        if re.fullmatch(r"-?\d+", t):
            return int(t)
        if re.fullmatch(r"-?\d+\.\d+", t):
            return float(t)
    except Exception:
        return t
    return t


def as_list_values(v):
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        raw = v.strip()
        if not raw:
            return [""]
        parts = [x.strip() for x in re.split(r"[\n,，;；、|]+", raw) if x and x.strip()]
        if len(parts) >= 2:
            return [parse_scalar(x) for x in parts]
        return [parse_scalar(raw)]
    return [v]


def expand_dataset(ds: dict, max_runs: int) -> list[dict]:
    if not isinstance(ds, dict):
        return []
    name = str(ds.get("name") or "数据集")[:120]
    vars_obj = ds.get("vars") or {}
    if not isinstance(vars_obj, dict):
        vars_obj = {}

    keys = []
    vals = []
    for k, v in vars_obj.items():
        kk = str(k or "").strip()
        if not kk:
            continue
        vv = as_list_values(v)
        if not vv:
            vv = [""]
        keys.append(kk)
        vals.append(vv)

    if not keys:
        return [{"name": name, "vars": {}}]

    out = []
    limit = int(max_runs or 0)
    if limit <= 0:
        limit = 10
    for combo in product(*vals):
        obj = {}
        for i, kk in enumerate(keys):
            obj[kk] = combo[i]
        out.append({"name": name, "vars": obj})
        if len(out) >= limit:
            break
    if len(out) > 1:
        for i, it in enumerate(out):
            it["name"] = f"{name} #{i+1}"
    return out

