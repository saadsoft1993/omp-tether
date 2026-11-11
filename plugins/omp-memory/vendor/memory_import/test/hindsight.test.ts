/** The Hindsight remainder: what is excluded (mnemopi origin, observations, invalid), and where the rest goes. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { writeFileSync } from "node:fs";
import path from "node:path";
import { runImport } from "../engine";
import { draftProjectMap, loadHindsightLedger, mnemopiOrigin, parseHindsightExport } from "../sources/hindsight";
import { type Env, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let env: Env;
let exportFile: string;
let mapFile: string;
let sms: string;

const fact = (id: string, text: string, extra: Record<string, unknown> = {}) => ({
	id,
	text,
	fact_type: "world",
	state: "valid",
	tags: ["project:sms"],
	context: "coding-agent-retain",
	document_id: `session-${id}`,
	date: "2026-10-01T10:00:00Z",
	...extra,
});

beforeAll(async () => {
	env = await makeEnv();
	sms = env.folder("sms");
	exportFile = path.join(env.root, "export.json");
	mapFile = path.join(env.root, "project-map.json");
	writeFileSync(
		path.join(env.root, "hs-ledger.json"),
		JSON.stringify({ documents: { "mnemopi:alpha:sms-abc:w:123": { bank: "alpha", sha: "x" } }, contents: {}, sources: {} }),
	);
	writeFileSync(
		exportFile,
		JSON.stringify({
			exportedAt: "2026-11-11T00:00:00Z",
			banks: {
				alpha: [
					fact("h1", "Hindsight native fact about sms invoices."),
					fact("h2", "Experience: sms deploy broke on Friday.", { fact_type: "experience" }),
					fact("m1", "Rewritten text of a mnemopi row (ledger id).", { document_id: "mnemopi:alpha:sms-abc:w:123" }),
					fact("m2", "Rewritten text of a mnemopi row (prefix only).", { document_id: "mnemopi:alpha:other:e:9" }),
					fact("m3", "Rewritten text, context marks the origin.", { context: "mnemopi import: coding-agent-learn" }),
					fact("m4", "Rewritten text, metadata marks the origin.", { metadata: { mnemopi_table: "w" } }),
					fact("o1", "Observation derived by Hindsight.", { fact_type: "observation" }),
					fact("i1", "An invalidated fact.", { state: "invalidated" }),
					fact("g1", "A global Hindsight fact with no project tag.", { tags: [] }),
					fact("u1", "A fact for a project nobody mapped.", { tags: ["project:mystery"] }),
					fact("t2", "A fact tagged for two projects.", { tags: ["project:sms", "project:other"] }),
				],
			},
		}),
	);
	writeFileSync(mapFile, JSON.stringify({ projects: { sms, other: null } }));
	env.emptyBank(env.bankOf(sms));
});
afterAll(() => env.cleanup());

const options = (over = {}) => env.options({ mode: "hindsight", hindsightExport: exportFile, projectMap: mapFile, ...over });

describe("exclusion of items that came from mnemopi", () => {
	test("each signal excludes on its own", () => {
		const ledger = loadHindsightLedger(path.join(env.root, "hs-ledger.json"));
		const base = { bank: "alpha", id: "x", text: "t", factType: "world", state: "valid", invalidatedAt: null, tags: [], context: null, documentId: null, date: null, metadata: {} };
		expect(mnemopiOrigin({ ...base, documentId: "mnemopi:alpha:sms-abc:w:123" }, ledger)).toContain("ledger");
		expect(mnemopiOrigin({ ...base, documentId: "mnemopi:whatever" }, ledger)).toContain("mnemopi:");
		expect(mnemopiOrigin({ ...base, context: "mnemopi import: x" }, ledger)).toContain("context");
		expect(mnemopiOrigin({ ...base, metadata: { mnemopi_table: "e" } }, ledger)).toContain("mnemopi_table");
		expect(mnemopiOrigin({ ...base, documentId: "session-1", context: "coding-agent-retain" }, ledger)).toBeNull();
	});

	test("only world/experience facts in state valid are candidates; mnemopi-origin ones are dropped", async () => {
		const report = await runImport(options({ command: "dry-run" }));
		expect(report.counts.found).toBe(5); // h1, h2, g1, u1, t2
		expect(report.counts.errors).toBe(0);
		expect(report.excluded).toMatchObject({
			"fact_type observation": 1,
			"state invalidated": 1,
		});
		const fromMnemopi = Object.entries(report.excluded).filter(([key]) => key.startsWith("from mnemopi")).reduce((sum, [, n]) => sum + n, 0);
		expect(fromMnemopi).toBe(4);
		expect(report.counts.excludedBySource).toBe(6);
	});

	test("project tags map through the explicit map; everything else is unassigned", async () => {
		const report = await runImport(options({ command: "dry-run" }));
		const smsBank = env.bankOf(sms);
		expect(report.perBank.find(item => item.bank === smsBank)?.wouldImport).toBe(2);
		expect(report.perBank.find(item => item.bank === "unassigned")?.wouldImport).toBe(3);
		expect(report.unresolved.map(item => item.where).sort()).toEqual([
			'hindsight project "mystery" is not in the project map',
			"hindsight no project tag (global in Hindsight)",
			"hindsight several project tags: sms, other",
		].sort());
	});

	test("apply writes the remainder with hindsight provenance, then re-running adds nothing", async () => {
		const first = await runImport(options({ command: "import", apply: true }));
		expect(first.counts.imported).toBe(5);
		const db = env.db(env.bankOf(sms));
		try {
			const rows = db.query("SELECT content, metadata_json, timestamp FROM working_memory WHERE source = 'import-hindsight'").all() as { content: string; metadata_json: string; timestamp: string }[];
			expect(rows).toHaveLength(2);
			const meta = JSON.parse(rows[0]?.metadata_json ?? "{}");
			expect(meta).toMatchObject({ source: "hindsight", sourceKind: "memory", hindsightBank: "alpha", cwd: sms });
			expect(meta.originKey).toMatch(/^alpha#h[12]$/);
			expect(rows.map(row => row.timestamp)).toEqual(["2026-10-01T10:00:00.000Z", "2026-10-01T10:00:00.000Z"]);
		} finally {
			db.close();
		}
		const second = await runImport(options({ command: "import", apply: true }));
		expect(second.counts.imported).toBe(0);
		expect(second.counts.skippedUnchanged).toBe(5);
	});
});

describe("project map draft", () => {
	test("names with exactly one matching folder are filled; the rest are null with candidates", () => {
		const draft = draftProjectMap(exportFile, [sms, "/x/a/other", "/x/b/other"]);
		expect(draft.projects).toEqual({ mystery: null, other: null, sms });
		expect(draft.candidates.other).toEqual(["/x/a/other", "/x/b/other"]);
		expect(draft.counts.sms).toBe(9);
	});

	test("export shapes: banks map, bank+items, bare list", () => {
		const bare = path.join(env.root, "bare.json");
		writeFileSync(bare, JSON.stringify([{ ...fact("b1", "bare"), bank: "beta" }]));
		expect(parseHindsightExport(bare).items[0]?.bank).toBe("beta");
		const single = path.join(env.root, "single.json");
		writeFileSync(single, JSON.stringify({ bank: "alpha", items: [fact("s1", "single")] }));
		expect(parseHindsightExport(single).items[0]?.bank).toBe("alpha");
	});
});
