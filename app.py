#!/usr/bin/env python3
"""
Grocery list web app (prototype, no price estimates).

    python app.py              # serves http://localhost:8000

Features:
  - Add a recipe from a TikTok URL (runs extract.py, needs an LLM key)
    or by pasting recipe JSON.
  - Removing a recipe automatically subtracts its ingredients.
  - Ingredient quantities are aggregated across recipes, with basic
    unit conversion (tsp/tbsp/cup/ml, g/kg/oz/lb).
  - Per-item quantity can be manually adjusted (override); clearing the
    override restores the computed total.

Storage: data/grocery.json (created on first run). Stdlib only.
"""

import json
import os
import re
import subprocess
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DATA_FILE = os.path.join(DATA_DIR, "grocery.json")
STATIC_DIR = os.path.join(BASE, "static")
EXTRACT_PY = os.path.join(BASE, "extract.py")

# ---------------------------------------------------------------- quantities

UNICODE_FRACTIONS = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3",
                     "⅔": "2/3", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8"}

UNIT_ALIASES = {
    # volume
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp", "t": "tsp",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp", "tblsp": "tbsp",
    "cup": "cup", "cups": "cup", "c": "cup",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "floz": "fl oz", "fl oz": "fl oz", "fluid ounce": "fl oz", "fluid ounces": "fl oz",
    "pint": "pint", "pints": "pint", "quart": "quart", "quarts": "quart",
    "gallon": "gallon", "gallons": "gallon",
    # weight
    "g": "g", "gram": "g", "grams": "g", "gramme": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    # count-ish (canonical singular; pluralized for display)
    "clove": "clove", "cloves": "clove",
    "can": "can", "cans": "can", "tin": "can",
    "jar": "jar", "jars": "jar",
    "slice": "slice", "slices": "slice",
    "piece": "piece", "pieces": "piece",
    "sprig": "sprig", "sprigs": "sprig",
    "stalk": "stalk", "stalks": "stalk",
    "head": "head", "heads": "head",
    "bunch": "bunch", "bunches": "bunch",
    "nest": "nest", "nests": "nest",
    "packet": "packet", "packets": "packet",
    "package": "package", "packages": "package",
    "scoop": "scoop", "scoops": "scoop",
    "dash": "dash", "dashes": "dash", "pinch": "pinch", "pinches": "pinch",
}

# singular -> plural for display ("1 jar" vs "2 jars")
PLURAL = {"cup": "cups", "pint": "pints", "quart": "quarts", "gallon": "gallons",
          "clove": "cloves", "can": "cans", "jar": "jars", "slice": "slices",
          "piece": "pieces", "sprig": "sprigs", "stalk": "stalks",
          "head": "heads", "bunch": "bunches", "nest": "nests",
          "packet": "packets", "package": "packages", "scoop": "scoops",
          "dash": "dashes", "pinch": "pinches"}

NULL_UNITS = {"", "null", "none", "n/a", "na", "-"}

VOLUME_TO_ML = {"tsp": 4.92892, "tbsp": 14.7868, "fl oz": 29.5735, "cup": 236.588,
                "pint": 473.176, "quart": 946.353, "gallon": 3785.41,
                "ml": 1.0, "l": 1000.0}
WEIGHT_TO_G = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}


def parse_quantity(q):
    """'1 1/2' -> 1.5, '½' -> 0.5, '2' -> 2.0, 'a handful' -> None."""
    if q is None:
        return None
    s = str(q).strip().lower()
    for ch, rep in UNICODE_FRACTIONS.items():
        s = s.replace(ch, f" {rep} ")
    s = re.sub(r"\s+", " ", s).strip()
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", s)          # 1 1/2
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", s)                  # 1/2
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    m = re.fullmatch(r"\d+(\.\d+)?", s)                        # 2, 2.5
    if m:
        return float(s)
    return None


def norm_unit(u):
    if not u:
        return None
    s = re.sub(r"\s+", " ", str(u).strip().lower().rstrip("."))
    if s in NULL_UNITS:
        return None
    return UNIT_ALIASES.get(s, s)


def norm_name(s):
    return re.sub(r"\s+", " ", str(s).strip().lower())


def dimension_of(unit):
    if unit in VOLUME_TO_ML:
        return "volume"
    if unit in WEIGHT_TO_G:
        return "weight"
    return "count"


def to_base(qty, unit):
    dim = dimension_of(unit)
    if dim == "volume":
        return qty * VOLUME_TO_ML[unit], "ml"
    if dim == "weight":
        return qty * WEIGHT_TO_G[unit], "g"
    return qty, unit  # count-ish: only merge identical units


def from_base(base_qty, unit):
    dim = dimension_of(unit)
    if dim == "volume":
        return base_qty / VOLUME_TO_ML[unit]
    if dim == "weight":
        return base_qty / WEIGHT_TO_G[unit]
    return base_qty


def fmt_qty(q):
    if q is None:
        return ""
    # pretty fractions for common values
    for frac, label in [(0.25, "¼"), (0.5, "½"), (0.75, "¾"),
                        (1/3, "⅓"), (2/3, "⅔")]:
        if abs(q - round(q) - frac) < 0.02 and q < 10:
            whole = int(round(q))
            return f"{whole}{label}" if whole else label
    if abs(q - round(q)) < 0.005:
        return str(int(round(q)))
    return f"{q:.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- state

def blank_state():
    return {"recipes": [], "overrides": {}, "checked": {},
            "pantry": [], "saved": []}


def load_state():
    st = blank_state()
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            disk = json.load(f)
        for k in st:  # tolerate state files from older versions
            if k in disk:
                st[k] = disk[k]
    return st


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def _new_bucket(merge_key, name, display_name, display_unit, dim):
    return {
        "key": merge_key,
        "name": name, "display_name": display_name,
        "display_unit": display_unit, "dim": dim,
        "total_base": 0.0, "has_qty": False,
        "unknown": 0, "sources": [],
    }


def aggregate(state):
    """Compute the grocery list from recipes + manual overrides."""
    buckets = {}  # key -> aggregated item

    def add_source(b, recipe, ing):
        qty_text = " ".join(p for p in
                            [str(ing.get("quantity") or "").strip(),
                             str(ing.get("unit") or "").strip()] if p)
        b["sources"].append({
            "recipe_id": recipe["id"],
            "recipe_name": recipe.get("name", "Recipe"),
            "qty_text": qty_text or "as needed",
            "notes": ing.get("notes"),
        })

    def find_bucket(name):
        """For a quantity-less ingredient: prefer a bucket with known amounts."""
        same = [b for b in buckets.values() if b["name"] == name]
        for b in same:
            if b["has_qty"]:
                return b
        return same[0] if same else None

    # pass 1+2: ingredients with a parseable quantity
    pending = []  # no-quantity ingredients, handled in pass 3
    for recipe in state["recipes"]:
        scale = float(recipe.get("scale", 1) or 1)
        for group in recipe.get("ingredient_groups", []):
            for ing in group.get("items", []):
                name = norm_name(ing.get("item", ""))
                if not name:
                    continue
                unit = norm_unit(ing.get("unit"))
                qty = parse_quantity(ing.get("quantity"))
                if qty is None:
                    pending.append((recipe, ing, name))
                    continue
                qty = qty * scale
                if unit:
                    dim = dimension_of(unit)
                    # count-ish units only merge when identical ("2 cloves" vs "1 head")
                    key = (name, dim, unit if dim == "count" else dim)
                    base, _ = to_base(qty, unit)
                else:
                    key = (name, "count", "")
                    base = qty
                    dim, unit = "count", ""
                b = buckets.get("|".join(key))
                if b is None:
                    b = _new_bucket("|".join(key), name,
                                    ing.get("item", "").strip(), unit, dim)
                    buckets[b["key"]] = b
                b["total_base"] += base
                b["has_qty"] = True
                add_source(b, recipe, ing)

    # pass 3: quantity-less ingredients attach to the same-name bucket if any
    for recipe, ing, name in pending:
        b = find_bucket(name)
        if b is None:
            key = "|".join([name, "count", ""])
            b = _new_bucket(key, name, ing.get("item", "").strip(), "", "count")
            buckets[b["key"]] = b
        b["unknown"] += 1
        add_source(b, recipe, ing)

    items = []
    pantry = {norm_name(p) for p in state.get("pantry", [])}
    for b in buckets.values():
        if b["name"] in pantry:
            continue  # staples you always have never hit the list
        if b["has_qty"]:
            qty_val = from_base(b["total_base"], b["display_unit"])
            unit = b["display_unit"]
            if not (0 < qty_val <= 1.0001):  # plural only above one ("2 cups", "½ cup")
                unit = PLURAL.get(unit, unit)
            qty_s, more = fmt_qty(qty_val), b["unknown"] > 0
        else:
            qty_s, unit, more = "as needed", "", False
        item = {
            "key": b["key"], "name": b["display_name"] or b["name"],
            "quantity": qty_s, "unit": unit, "more": more,
            "checked": bool(state["checked"].get(b["key"])),
            "sources": b["sources"], "overridden": False,
        }
        ov = state["overrides"].get(b["key"])
        if ov is not None:
            item["quantity"] = str(ov.get("quantity", ""))
            item["unit"] = ov.get("unit", "")
            item["more"] = False
            item["overridden"] = True
        items.append(item)
    items.sort(key=lambda i: (i["checked"], i["name"].lower()))
    return {
        "recipes": [recipe_summary(r) for r in state["recipes"]],
        "saved": [recipe_summary(r) for r in state.get("saved", [])],
        "pantry": sorted(state.get("pantry", [])),
        "items": items,
    }


def item_line(i):
    if i["quantity"] == "as needed":
        return f"{i['name']} (as needed)"
    q = " ".join(p for p in [i["quantity"], i["unit"]] if p)
    return f"{q} {i['name']}" + ("+" if i.get("more") else "")


def share_text(state):
    agg = aggregate(state)
    buy = [i for i in agg["items"] if not i["checked"]]
    got = [i for i in agg["items"] if i["checked"]]
    lines = [f"Grocery list — {len(buy)} to buy" +
             (f", {len(got)} already bought" if got else "")]
    if agg["recipes"]:
        lines.append("")
        for r in agg["recipes"]:
            sc = r.get("scale", 1) or 1
            lines.append(f"· {r['name']}" + (f" (×{fmt_qty(sc)})" if sc != 1 else ""))
    if buy:
        lines += ["", "TO BUY"] + [f"• {item_line(i)}" for i in buy]
    if got:
        lines += ["", "BOUGHT"] + [f"• {item_line(i)}" for i in got]
    return "\n".join(lines)


def recipe_summary(r):
    scale = float(r.get("scale", 1) or 1)
    servings = r.get("servings")
    sq = parse_quantity(servings)
    return {
        "id": r["id"], "name": r.get("name"),
        "servings": fmt_qty(sq * scale) if sq else servings,
        "scale": scale,
        "ingredient_count": sum(len(g.get("items", []))
                                for g in r.get("ingredient_groups", [])),
        "source_url": (r.get("source") or {}).get("url"),
    }


# ---------------------------------------------------------------- extract via URL

def extract_from_url(url):
    backend = os.environ.get("LLM_BACKEND", "gemini")
    tmp = os.path.join(DATA_DIR, f".extract-{uuid.uuid4().hex}.json")
    os.makedirs(DATA_DIR, exist_ok=True)
    cmd = [sys.executable, EXTRACT_PY, url, "--llm", backend, "-o", tmp]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    try:
        if p.returncode != 0:
            err = (p.stderr or p.stdout).strip()
            if "GEMINI_API_KEY" in err or "OPENAI_API_KEY" in err:
                raise RuntimeError("missing-llm-key",
                                   "Set GEMINI_API_KEY (free at "
                                   "https://aistudio.google.com/apikey) and restart.")
            raise RuntimeError("extract-failed", err[-800:] or "yt-dlp failed")
        with open(tmp, encoding="utf-8") as f:
            recipe = json.load(f)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if recipe.get("error"):
        raise RuntimeError("no-recipe",
                           "; ".join(recipe.get("warnings", ["no recipe found"])))
    return recipe


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "GroceryList/0.1"

    def _send(self, code, obj=None, ctype="application/json"):
        body = json.dumps(obj, ensure_ascii=False).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def log_message(self, *a):
        pass

    # -- routes --
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html")
        if path == "/api/state":
            return self._send(200, aggregate(load_state()))
        if path == "/api/share":
            return self._send(200, {"text": share_text(load_state())})
        if path.startswith("/static/"):
            return self._serve_file(unquote(path[len("/static/"):]))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/recipes":
            body = self._read_json()
            try:
                if body.get("url"):
                    recipe = extract_from_url(body["url"].strip())
                elif body.get("recipe"):
                    recipe = body["recipe"]
                else:
                    return self._send(400, {"error": "provide 'url' or 'recipe'"})
            except RuntimeError as e:
                code, msg = e.args
                return self._send(502 if code != "no-recipe" else 422,
                                  {"error": code, "message": msg})
            recipe["id"] = uuid.uuid4().hex[:8]
            recipe.setdefault("scale", 1)
            state = load_state()
            state["recipes"].append(recipe)
            save_state(state)
            return self._send(200, aggregate(state))
        m = re.fullmatch(r"/api/recipes/([0-9a-f]+)/scale", path)
        if m:
            body = self._read_json()
            try:
                s = float(body.get("scale", 1))
            except (TypeError, ValueError):
                return self._send(400, {"error": "scale must be a number"})
            if not 0.25 <= s <= 10:
                return self._send(400, {"error": "scale must be between 0.25 and 10"})
            state = load_state()
            for r in state["recipes"]:
                if r["id"] == m.group(1):
                    r["scale"] = s
                    break
            else:
                return self._send(404, {"error": "recipe not found"})
            save_state(state)
            return self._send(200, aggregate(state))
        if path == "/api/pantry":
            body = self._read_json()
            name = norm_name(body.get("name", ""))
            if not name:
                return self._send(400, {"error": "provide 'name'"})
            state = load_state()
            if name not in state["pantry"]:
                state["pantry"].append(name)
            save_state(state)
            return self._send(200, aggregate(state))
        if path == "/api/saved":
            body = self._read_json()
            rid = body.get("recipe_id")
            state = load_state()
            src = next((r for r in state["recipes"] if r["id"] == rid), None)
            if src is None:
                return self._send(404, {"error": "recipe not found"})
            copy = json.loads(json.dumps(src))
            copy["id"] = uuid.uuid4().hex[:8]
            state["saved"].append(copy)
            save_state(state)
            return self._send(200, aggregate(state))
        m = re.fullmatch(r"/api/saved/([0-9a-f]+)/readd", path)
        if m:
            state = load_state()
            src = next((r for r in state.get("saved", []) if r["id"] == m.group(1)), None)
            if src is None:
                return self._send(404, {"error": "saved recipe not found"})
            copy = json.loads(json.dumps(src))
            copy["id"] = uuid.uuid4().hex[:8]
            copy["scale"] = 1
            state["recipes"].append(copy)
            save_state(state)
            return self._send(200, aggregate(state))
        if path == "/api/items/check":
            body = self._read_json()
            key = body.get("key")
            if not key:
                return self._send(400, {"error": "provide 'key'"})
            state = load_state()
            if body.get("checked"):
                state["checked"][key] = True
            else:
                state["checked"].pop(key, None)
            save_state(state)
            return self._send(200, aggregate(state))
        if path == "/api/items/adjust":
            body = self._read_json()
            key = body.get("key")
            if not key:
                return self._send(400, {"error": "provide 'key'"})
            state = load_state()
            agg = aggregate(state)
            current = next((i for i in agg["items"] if i["key"] == key), None)
            if current is None:
                return self._send(404, {"error": "item not found"})
            ov = state["overrides"].get(key, {})
            if "quantity" in body:  # absolute set
                ov = {"quantity": body["quantity"], "unit": body.get("unit", "")}
            elif "delta" in body:   # relative adjust
                base_q = parse_quantity(ov.get("quantity") if ov else current["quantity"])
                base_q = 0.0 if base_q is None else base_q
                new_q = max(0.0, base_q + float(body["delta"]))
                ov = {"quantity": fmt_qty(new_q),
                      "unit": ov.get("unit", current["unit"])}
            else:
                return self._send(400, {"error": "provide 'quantity' or 'delta'"})
            state["overrides"][key] = ov
            save_state(state)
            return self._send(200, aggregate(state))
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        state = load_state()
        if path == "/api/checked":
            state["checked"] = {}
            save_state(state)
            return self._send(200, aggregate(state))
        m = re.fullmatch(r"/api/pantry/(.+)", path)
        if m:
            name = norm_name(unquote(m.group(1)))
            state["pantry"] = [p for p in state["pantry"] if norm_name(p) != name]
            save_state(state)
            return self._send(200, aggregate(state))
        m = re.fullmatch(r"/api/saved/([0-9a-f]+)", path)
        if m:
            state["saved"] = [r for r in state.get("saved", []) if r["id"] != m.group(1)]
            save_state(state)
            return self._send(200, aggregate(state))
        m = re.fullmatch(r"/api/recipes/([0-9a-f]+)", path)
        if m:
            rid = m.group(1)
            state["recipes"] = [r for r in state["recipes"] if r["id"] != rid]
            save_state(state)
            return self._send(200, aggregate(state))
        m = re.fullmatch(r"/api/overrides/(.+)", path)
        if m:
            state["overrides"].pop(unquote(m.group(1)), None)
            save_state(state)
            return self._send(200, aggregate(state))
        return self._send(404, {"error": "not found"})

    def _serve_file(self, name, ctype=None):
        safe = os.path.normpath(name).lstrip("/")
        fpath = os.path.join(STATIC_DIR, safe)
        if not fpath.startswith(STATIC_DIR) or not os.path.isfile(fpath):
            return self._send(404, {"error": "not found"})
        if ctype is None:
            ctype = {"html": "text/html", "js": "text/javascript",
                     "css": "text/css"}.get(safe.rsplit(".", 1)[-1], "text/plain")
        with open(fpath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Grocery list running at http://localhost:{port}")
    print("Paste a TikTok URL in the page, or POST recipe JSON to /api/recipes.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
