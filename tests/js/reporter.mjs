// node --test reporter for tests/test_frontend.py: one JSON line per finished test
// {file, name, status: "pass" | "fail" | "skip", error?}, so pytest can report each
// test file on its own (node's TAP output doesn't say which file a test came from).
export default async function* reporter(source) {
  for await (const ev of source) {
    if (ev.type !== "test:pass" && ev.type !== "test:fail") continue;
    const d = ev.data || {};
    if (d.details && d.details.type === "suite") continue;
    const out = { file: d.file || "", name: d.name || "", nesting: d.nesting || 0,
                  status: ev.type === "test:fail" ? "fail" : (d.skip !== undefined ? "skip" : "pass") };
    if (out.status === "fail") {
      const err = d.details && d.details.error;
      const cause = err && (err.cause || err);
      out.error = String((cause && (cause.stack || cause.message)) || err || "failed").slice(0, 4000);
    }
    yield JSON.stringify(out) + "\n";
  }
}
