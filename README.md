# SQL Data Assistant

An Anna app: upload a CSV, ask a question in plain English, and get the answer, a result table and the SQL used.

## “Anna’s AI writes the SQL query, and the tool runs it safely (read-only) on your CSV.”
- Upload a CSV file
- Ask a question like "Who has the highest marks?"
- The backend loads the CSV into SQLite, turns the question into SQL, runs it and returns the result

## Structure
- `bundle/` - the web interface
- `tool/` - the Python tool (stdlib only), built into binaries for 4 platforms with GitHub Actions
- `manifest.json`, `app.json` - Anna app configuration

## Status
Submitted to the Anna App Store for review.
