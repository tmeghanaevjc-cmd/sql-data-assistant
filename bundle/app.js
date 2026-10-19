// SQL Data Assistant - UI. AI writes the SQL; the Python tool runs it safely.
import { AnnaAppRuntime } from "/static/anna-apps/_sdk/latest/index.js";

const PUBLISHED_TOOL_ID = "tool-meghana-ai-sql-data-assistant-r446nerg";
const DEV_TOOL_ID = "tool-dev-sql-data-assistant";
const TABLE = "data";
const $ = (id) => document.getElementById(id);

const els = {
  drop: $("drop"), file: $("file"), info: $("file-info"), cols: $("cols"),
  question: $("question"), askBtn: $("ask-btn"), chips: $("chips"),
  status: $("status"), result: $("result"), answer: $("answer"),
  tableWrap: $("table-wrap"), sql: $("sql"),
};

let anna = null;
let csvText = "";
let columnsInfo = [];
let workingToolId = null;

const SQL_PROMPT =
  "You are an expert SQLite analyst. Write exactly ONE read-only SQLite SELECT query that answers the user's question. " +
  "Use only the table and columns provided, and quote column names with double quotes. " +
  "Return only the SQL, with no explanation and no markdown.";

const ANSWER_PROMPT =
  "You answer questions about a table. Using only the result rows provided, answer the question in one or two plain-English sentences. " +
  "If there are no rows, say no matching data was found.";

function setStatus(msg, isError = false) {
  els.status.textContent = msg;
  els.status.className = isError ? "error" : "";
}

function candidateToolIds() {
  const injected = Object.values(window.__ANNA_TOOL_IDS__ || {});
  return [...new Set([workingToolId, ...injected, PUBLISHED_TOOL_ID, DEV_TOOL_ID].filter(Boolean))];
}

async function callTool(method, args) {
  let lastError = null;
  for (const tool_id of candidateToolIds()) {
    try {
      const out = await anna.tools.invoke({ tool_id, method, args });
      workingToolId = tool_id;
      if (out && out.error) throw Object.assign(new Error(out.error), { fromTool: true });
      return out;
    } catch (e) {
      if (e.fromTool) throw e;
      lastError = e;
    }
  }
  throw lastError || new Error("Could not reach the tool.");
}

async function aiComplete(systemPrompt, userText, maxTokens = 400) {
  const res = await anna.llm.complete({
    systemPrompt,
    messages: [{ role: "user", content: { type: "text", text: userText } }],
    maxTokens,
    temperature: 0,
  });
  return ((res && res.content && res.content.text) || "").trim();
}

function cleanSql(text) {
  return text.replace(/```sql|```/gi, "").trim().replace(/;+\s*$/, "");
}

function buildSqlRequest(question, badSql, errorMsg) {
  const schema = columnsInfo.map((c) => `"${c.sql_name || c.name}" (${c.type})`).join(", ");
  let text = `Table: ${TABLE}\nColumns: ${schema}\nQuestion: ${question}`;
  if (badSql) {
    text += `\n\nThe previous query failed.\nQuery: ${badSql}\nError: ${errorMsg}\nWrite a corrected query.`;
  }
  return text;
}

async function explain(question, columns, rows) {
  try {
    const sample = rows.slice(0, 20);
    const text = `Question: ${question}\nColumns: ${columns.join(", ")}\nRows (first ${sample.length} of ${rows.length}): ${JSON.stringify(sample)}`;
    const reply = await aiComplete(ANSWER_PROMPT, text, 250);
    return reply || `${rows.length} row(s) returned.`;
  } catch (e) {
    return `${rows.length} row(s) returned.`;
  }
}

function renderTable(columns, rows) {
  els.tableWrap.replaceChildren();
  if (!rows.length || (rows.length === 1 && columns.length === 1)) {
    els.tableWrap.hidden = true;
    return;
  }
  const table = document.createElement("table");
  const head = table.createTHead().insertRow();
  columns.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    head.appendChild(th);
  });
  const body = table.createTBody();
  rows.forEach((r) => {
    const tr = body.insertRow();
    r.forEach((v) => {
      tr.insertCell().textContent = v === null ? "" : String(v);
    });
  });
  els.tableWrap.appendChild(table);
  els.tableWrap.hidden = false;
}

function showChips(columns) {
  const numeric = columns.filter((c) => c.type !== "TEXT").map((c) => c.name);
  const text = columns.filter((c) => c.type === "TEXT").map((c) => c.name);
  const ideas = ["How many rows are there?", "Show the first 5 rows"];
  if (numeric[0]) {
    ideas.push(`What is the average ${numeric[0]}?`, `Who has the highest ${numeric[0]}?`);
    if (text[1] || text[0]) ideas.push(`Total ${numeric[0]} by ${text[text.length > 1 ? 1 : 0]}`);
  }
  els.chips.replaceChildren();
  ideas.slice(0, 5).forEach((q) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = q;
    b.addEventListener("click", () => { els.question.value = q; ask(); });
    els.chips.appendChild(b);
  });
}

async function loadFile(file) {
  if (!file) return;
  if (file.size > 2_000_000) { setStatus("This file is over 2 MB. Choose a smaller CSV.", true); return; }
  els.result.hidden = true;
  setStatus("Reading file…");
  try {
    csvText = await file.text();
    const p = await callTool("profile", { csv_text: csvText });
    columnsInfo = p.columns;
    els.info.textContent = `${file.name} — ${p.rows} rows, ${p.columns.length} columns`;
    els.cols.textContent = "Columns: " + p.columns.map((c) => c.name).join(", ");
    els.question.disabled = false;
    els.askBtn.disabled = false;
    showChips(p.columns);
    setStatus("File loaded. Ask a question.");
    els.question.focus();
  } catch (e) {
    csvText = "";
    columnsInfo = [];
    els.question.disabled = true;
    els.askBtn.disabled = true;
    els.chips.replaceChildren();
    setStatus(e.message || "Could not read this file.", true);
  }
}

async function ask() {
  const question = els.question.value.trim();
  if (!csvText) { setStatus("Choose a CSV file first.", true); return; }
  if (!question) { setStatus("Type a question first.", true); return; }
  els.askBtn.disabled = true;
  els.result.hidden = true;
  try {
    setStatus("AI is writing the SQL…");
    let sql = cleanSql(await aiComplete(SQL_PROMPT, buildSqlRequest(question)));
    let out;
    try {
      out = await callTool("run_sql", { csv_text: csvText, sql });
    } catch (firstError) {
      setStatus("Fixing the query…");
      sql = cleanSql(await aiComplete(SQL_PROMPT, buildSqlRequest(question, sql, firstError.message)));
      out = await callTool("run_sql", { csv_text: csvText, sql });
    }
    setStatus("Writing the answer…");
    els.answer.textContent = await explain(question, out.columns, out.rows);
    els.sql.textContent = sql;
    renderTable(out.columns, out.rows);
    els.result.hidden = false;
    setStatus("");
  } catch (e) {
    setStatus(e.message || "Something went wrong. Try rephrasing your question.", true);
  } finally {
    els.askBtn.disabled = false;
  }
}

async function main() {
  try {
    anna = await AnnaAppRuntime.connect();
  } catch (e) {
    setStatus("Standalone preview: open this app inside Anna to use it.", true);
    return;
  }
  await anna.window.set_title({ title: "SQL Data Assistant" });

  els.file.addEventListener("change", () => loadFile(els.file.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    els.drop.addEventListener(ev, (e) => { e.preventDefault(); els.drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    els.drop.addEventListener(ev, (e) => { e.preventDefault(); els.drop.classList.remove("over"); }));
  els.drop.addEventListener("drop", (e) => loadFile(e.dataTransfer.files[0]));
  els.askBtn.addEventListener("click", ask);
  els.question.addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
}

main();