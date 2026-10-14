"""SQL Data Assistant - Anna Executa (stdio JSON-RPC plugin).

Upload a CSV, ask a question in plain English, get the answer, the SQL used,
and a result table. Uses only the Python standard library.
"""

import csv
import io
import json
import re
import sqlite3
import sys

MAX_CSV_CHARS = 2_000_000
MAX_ROWS_RETURNED = 200

MANIFEST = {
    "name": "sql-data-assistant-tool",
    "display_name": "SQL Data Assistant Tool",
    "version": "0.1.2",
    "description": (
        "Loads a CSV, turns a plain-English question into SQL, runs it, and "
        "returns the answer, the SQL and a result table."
    ),
    "author": "meghana-ai",
    "license": "MIT",
    "category": "data",
    "runtime": {"type": "uv", "min_version": "0.1.2"},
    "tools": [
        {
            "name": "ping",
            "description": "Smoke-test method that returns pong.",
            "timeout": 10,
            "parameters": [],
        },
        {
            "name": "profile",
            "description": "Describe a CSV file: row count, column names and types, and a few sample rows.",
            "timeout": 30,
            "parameters": [
                {
                    "name": "csv_text",
                    "type": "string",
                    "description": "The full text of the CSV file.",
                    "required": True,
                }
            ],
        },
        {
            "name": "ask",
            "description": (
                "Answer a plain-English question about a CSV file by running SQL on it. "
                "Returns the answer, the SQL used, and a result table."
            ),
            "timeout": 60,
            "parameters": [
                {
                    "name": "csv_text",
                    "type": "string",
                    "description": "The full text of the CSV file.",
                    "required": True,
                },
                {
                    "name": "question",
                    "type": "string",
                    "description": "The question in plain English.",
                    "required": True,
                },
            ],
        },
    ],
}


class UserError(Exception):
    """An error with a message that is safe and useful to show the user."""


# ---------------------------------------------------------------- CSV loading
def _clean_name(name, index, used):
    base = re.sub(r"\W+", "_", (name or "").strip()).strip("_").lower() or f"col{index + 1}"
    if base[0].isdigit():
        base = "c_" + base
    candidate, n = base, 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate


def _to_number(value):
    value = value.strip().replace(",", "")
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return float(value)  # raises ValueError if not numeric


def load_csv(csv_text):
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise UserError("The CSV file is empty. Upload a file that has a header row and data rows.")
    if len(csv_text) > MAX_CSV_CHARS:
        raise UserError("This CSV is too large. Please upload a file under 2 MB.")
    csv_text = csv_text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(csv_text[:5000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(csv_text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise UserError("The CSV needs a header row and at least one data row.")
    header, body = rows[0], rows[1:]
    used = set()
    cols = [_clean_name(h, i, used) for i, h in enumerate(header)]
    display = {c: (h.strip() or c) for c, h in zip(cols, header)}
    body = [(r + [""] * len(cols))[: len(cols)] for r in body]

    types = {}
    for i, c in enumerate(cols):
        try:
            nums = [_to_number(r[i]) for r in body]
            non_null = [n for n in nums if n is not None]
            if not non_null:
                raise ValueError
            types[c] = "REAL" if any(isinstance(n, float) for n in non_null) else "INTEGER"
        except ValueError:
            types[c] = "TEXT"

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE data (%s)" % ", ".join(f'"{c}" {types[c]}' for c in cols))
    prepared = []
    for r in body:
        row = []
        for i, c in enumerate(cols):
            v = r[i].strip()
            if types[c] == "TEXT":
                row.append(v if v != "" else None)
            else:
                row.append(_to_number(v) if v != "" else None)
        prepared.append(row)
    conn.executemany("INSERT INTO data VALUES (%s)" % ",".join("?" * len(cols)), prepared)
    conn.commit()
    return conn, cols, display, types, len(body)


# ----------------------------------------------------- question understanding
def _words(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def _singular(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _find_columns(question, cols, display):
    """Return columns mentioned in the question, in order of appearance."""
    q_words = [_singular(w) for w in _words(question)]
    found = []
    for c in cols:
        name_words = [_singular(w) for w in _words(display[c])] or [c]
        n = len(name_words)
        for i in range(len(q_words) - n + 1):
            if q_words[i : i + n] == name_words:
                found.append((i, n, c))
                break
    found.sort(key=lambda t: (t[0], -t[1]))
    result = []
    for _, _, c in found:
        if c not in result:
            result.append(c)
    return result


def _q(col):
    return f'"{col}"'


def build_sql(question, conn, cols, display, types):
    ql = " " + question.lower().strip().rstrip("?.! ") + " "
    mentioned = _find_columns(question, cols, display)
    numeric = [c for c in cols if types[c] != "TEXT"]
    text_cols = [c for c in cols if types[c] == "TEXT"]
    num_mentioned = [c for c in mentioned if c in numeric]
    txt_mentioned = [c for c in mentioned if c in text_cols]

    # ---- filters
    where, params = [], []
    for c in text_cols:
        for (v,) in conn.execute(f'SELECT DISTINCT {_q(c)} FROM data WHERE {_q(c)} IS NOT NULL LIMIT 500'):
            if len(v) >= 2 and re.search(r"(?<![a-z0-9])" + re.escape(v.lower()) + r"(?![a-z0-9])", ql):
                where.append(f"{_q(c)} = ?")
                params.append(v)
                break
    filter_col = num_mentioned[0] if num_mentioned else (numeric[0] if len(numeric) == 1 else None)
    m = re.search(r"(?:more than|greater than|above|over|bigger than|higher than|>)\s*(-?\d+(?:\.\d+)?)", ql)
    if m and filter_col:
        where.append(f"{_q(filter_col)} > ?")
        params.append(float(m.group(1)))
    m = re.search(r"(?:less than|lower than|below|under|smaller than|<)\s*(-?\d+(?:\.\d+)?)", ql)
    if m and filter_col:
        where.append(f"{_q(filter_col)} < ?")
        params.append(float(m.group(1)))
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    def metric():
        if num_mentioned:
            return num_mentioned[0]
        if len(numeric) == 1:
            return numeric[0]
        raise UserError(
            "Which column should I use? Mention one of these numeric columns in your question: "
            + ", ".join(display[c] for c in numeric)
            + "."
        )

    # ---- grouping: "by X", "per X", "for each X"
    group_col = None
    gm = re.search(r"\b(?:by|per|for each|in each|of each|each)\s+([a-z0-9_ ]+)", ql)
    if gm:
        tail = [_singular(w) for w in _words(gm.group(1))]
        for c in cols:
            nw = [_singular(w) for w in _words(display[c])] or [c]
            if tail[: len(nw)] == nw:
                group_col = c
                break

    has = lambda *ws: any(re.search(r"\b" + w + r"\b", ql) for w in ws)

    top_n = re.search(r"\b(?:top|highest|best|largest|biggest)\s+(\d+)\b", ql)
    bottom_n = re.search(r"\b(?:bottom|lowest|worst|smallest)\s+(\d+)\b", ql)
    first_n = re.search(r"\b(?:first|show|list|display)\s+(\d+)\b", ql)

    # ---- count
    if has("how many", "count", "number of"):
        if group_col:
            return (f"SELECT {_q(group_col)}, COUNT(*) AS count FROM data{where_sql} "
                    f"GROUP BY {_q(group_col)} ORDER BY count DESC", params)
        return f"SELECT COUNT(*) AS count FROM data{where_sql}", params

    # ---- aggregates (average / total / max / min), optionally grouped
    agg = None
    if has("average", "mean", "avg"):
        agg = ("AVG", "average")
    elif has("total", "sum"):
        agg = ("SUM", "total")
    if agg:
        mc = metric()
        fn, label = agg
        if group_col:
            return (f"SELECT {_q(group_col)}, ROUND({fn}({_q(mc)}), 2) AS {label}_{mc} FROM data{where_sql} "
                    f"GROUP BY {_q(group_col)} ORDER BY {label}_{mc} DESC", params)
        return f"SELECT ROUND({fn}({_q(mc)}), 2) AS {label}_{mc} FROM data{where_sql}", params

    is_max = has("highest", "maximum", "max", "largest", "biggest", "most", "best", "top", "greatest")
    is_min = has("lowest", "minimum", "min", "smallest", "least", "worst", "bottom", "cheapest")
    if (is_max or is_min) and not (top_n or bottom_n):
        mc = metric()
        if group_col:
            fn = "MAX" if is_max else "MIN"
            return (f"SELECT {_q(group_col)}, {fn}({_q(mc)}) AS {fn.lower()}_{mc} FROM data{where_sql} "
                    f"GROUP BY {_q(group_col)} ORDER BY {fn.lower()}_{mc} {'DESC' if is_max else 'ASC'}", params)
        fn = "MAX" if is_max else "MIN"
        and_or_where = " AND " if where else " WHERE "
        return (f"SELECT * FROM data{where_sql}{and_or_where}{_q(mc)} = "
                f"(SELECT {fn}({_q(mc)}) FROM data{where_sql})", params + params)
    if top_n or bottom_n:
        mc = metric()
        n = int((top_n or bottom_n).group(1))
        order = "DESC" if top_n else "ASC"
        return (f"SELECT * FROM data{where_sql} ORDER BY {_q(mc)} {order} LIMIT {min(n, MAX_ROWS_RETURNED)}", params)

    # ---- distinct values
    if has("unique", "distinct") and mentioned:
        return f"SELECT DISTINCT {_q(mentioned[0])} FROM data{where_sql} LIMIT {MAX_ROWS_RETURNED}", params

    # ---- plain listing / filtering
    if where or has("show", "list", "display", "find"):
        limit = min(int(first_n.group(1)), MAX_ROWS_RETURNED) if first_n else MAX_ROWS_RETURNED
        select = ", ".join(_q(c) for c in mentioned) if mentioned and not where else "*"
        return f"SELECT {select} FROM data{where_sql} LIMIT {limit}", params

    raise UserError(
        "I couldn't understand that question. Try something like: "
        "\"How many rows are there?\", \"What is the average <column>?\", "
        "\"Who has the highest <column>?\", \"Total <column> by <column>\", or \"Show the first 5 rows\"."
    )


# ------------------------------------------------------------------- methods
def _fmt_sql(sql, params):
    out = sql
    for p in params:
        out = out.replace("?", repr(p) if isinstance(p, str) else str(p), 1)
    return out


def profile(csv_text):
    conn, cols, display, types, n = load_csv(csv_text)
    sample = conn.execute("SELECT * FROM data LIMIT 5").fetchall()
    return {
        "rows": n,
        "columns": [{"name": display[c], "type": types[c]} for c in cols],
        "sample": {"columns": [display[c] for c in cols], "rows": [list(r) for r in sample]},
    }


def ask(csv_text, question):
    if not isinstance(question, str) or not question.strip():
        raise UserError("Type a question about your data first.")
    conn, cols, display, types, n = load_csv(csv_text)
    sql, params = build_sql(question, conn, cols, display, types)
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    for c in cols:
        names = [display[c] if x == c else x for x in names]
    rows = [list(r) for r in cur.fetchmany(MAX_ROWS_RETURNED)]
    if len(rows) == 1 and len(rows[0]) == 1:
        answer = f"{names[0]}: {rows[0][0]}"
    elif not rows:
        answer = "No rows matched your question."
    else:
        answer = f"Found {len(rows)} row(s)."
    return {
        "answer": answer,
        "sql": _fmt_sql(sql, params),
        "columns": names,
        "rows": rows,
        "total_rows_in_file": n,
    }


def invoke(method, args):
    # Tool methods MUST return {"success": True, "data": ...} or {"success": False, "error": "..."}
    try:
        if method == "ping":
            return {"success": True, "data": {"pong": True}}
        if method == "profile":
            return {"success": True, "data": profile(args.get("csv_text", ""))}
        if method == "ask":
            return {"success": True, "data": ask(args.get("csv_text", ""), args.get("question", ""))}
        return {"success": False, "error": f"unknown method: {method}"}
    except UserError as e:
        return {"success": True, "data": {"error": str(e)}}
    except sqlite3.Error as e:
        return {"success": True, "data": {"error": f"I couldn't run that question on this file ({e}). Try rephrasing it."}}


def _send(obj):
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(line.encode("utf-8"))
        buf.flush()
    else:
        sys.stdout.write(line)
        sys.stdout.flush()


def handle_rpc(req):
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    is_notification = "id" not in req

    if method == "describe":
        _send({"jsonrpc": "2.0", "id": req_id, "result": MANIFEST})
    elif method == "initialize":
        _send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2.0",
                "serverInfo": {"name": "sql-data-assistant-tool", "version": MANIFEST["version"]},
                "capabilities": {},
            },
        })
    elif method == "health":
        _send({"jsonrpc": "2.0", "id": req_id, "result": {"status": "ready", "message": "", "details": {}}})
    elif method == "invoke":
        tool = params.get("tool")
        if not tool:
            _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Missing 'tool' in params"}})
        else:
            _send({"jsonrpc": "2.0", "id": req_id, "result": invoke(tool, params.get("arguments") or {})})
    elif method == "shutdown":
        if not is_notification:
            _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return False
    elif not is_notification:
        _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
    return True


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print("[plugin] SQL Data Assistant Executa running", file=sys.stderr)
    raw_in = getattr(sys.stdin, "buffer", None)
    lines = (raw.decode("utf-8", errors="replace") for raw in raw_in) if raw_in is not None else sys.stdin
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(f"[warn] non-JSON on stdin: {line[:200]}", file=sys.stderr)
            continue
        if not isinstance(req, dict):
            continue
        try:
            if handle_rpc(req) is False:
                break
        except Exception as e:  # noqa: BLE001
            print(f"[plugin] internal error: {e}", file=sys.stderr)
            if "id" in req:
                _send({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32603, "message": f"Internal error: {e}"}})
    sys.stdout.flush()


if __name__ == "__main__":
    main()
