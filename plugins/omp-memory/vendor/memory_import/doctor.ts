/**
 * Checks that run before anyone trusts the importer with a banks directory (read-only on the banks; the library self-test
 * works in a scratch bank under the temp directory and caches its result in omp-memory's cache directory):
 *  - which Bun, which omp and which library package (and where it was found) the importer uses, and that the library
 *    is exactly omp's version, with the SQL the importer relies on present in the package and in the omp binary, and
 *    the behavioural self-test: the importer's write sequence run through the library in a scratch bank (selftest.ts),
 *  - every bank's core schema versus a fresh library database (skipped, and said so, when no library is installed),
 *  - every bank's directory name versus `projectBankSegment(cwd)` for the cwd its own rows record, computed both
 *    from the recorded path and from its realpath (omp hashes the path it was started in, so a symlinked path such
 *    as /tmp/x differs from /private/tmp/x). The importer's own `projectBankSegment` is a local copy, so this check
 *    is what proves it against the banks that already exist; it needs no library.
 */
import { existsSync } from "node:fs";
import { projectBankSegment } from "./bank_id";
import { listBanks, openReadonly } from "./banks";
import { normaliseFolder } from "./folders";
import { bankDbPath, freshCoreSchema, inspectLibrary, type LibraryCheck, readCoreSchema, schemaDiff } from "./lib";
import { bump, isRecord } from "./model";
import { ensureSelfTest, type SelfTestResult } from "./selftest";

export interface BankCheck {
	bank: string;
	/** "ok", the differences from a fresh library database, or "not checked" when no library is installed. */
	schema: "ok" | "not checked" | string[];
	rows: number;
	embeddingModels: string[];
	/** Most common cwd recorded by omp's own rows (retain/learn/transcript metadata). */
	cwd: string | null;
	cwdRealpath: string | null;
	cwdVotes: number;
	matchedBy: "recorded-path" | "realpath" | "none" | null;
	bankIdMatches: boolean | null;
}

export interface DoctorReport {
	/** The Bun running the importer (the embedding worker runs on the same one). */
	bun: { path: string; version: string };
	library: LibraryCheck;
	/** The behavioural self-test, run now (null when no library is installed); see selftest.ts. */
	selfTest: SelfTestResult | null;
	banks: BankCheck[];
	schemaMismatches: number;
	bankIdMatches: number;
	bankIdMatchedOnlyByRecordedPath: number;
	bankIdMismatches: number;
	bankIdUnknown: number;
}

export async function runDoctor(options: { libDir?: string; ompBin: string; banksDir: string }): Promise<DoctorReport> {
	const { check: library, lib } = await inspectLibrary({ libDir: options.libDir, ompBin: options.ompBin });
	const selfTest = lib ? (await ensureSelfTest(lib, { reuse: false })).result : null;
	const fresh = lib ? await freshCoreSchema(lib) : null;
	const banks: BankCheck[] = [];
	for (const bank of listBanks(options.banksDir)) {
		const file = bankDbPath(options.banksDir, bank);
		if (!existsSync(file)) continue;
		const db = openReadonly(file);
		try {
			const diffs = fresh ? schemaDiff(fresh, readCoreSchema(db)) : null;
			const votes: Record<string, number> = {};
			const rows = db.query("SELECT metadata_json FROM working_memory WHERE json_valid(metadata_json)").all() as {
				metadata_json: string;
			}[];
			for (const row of rows) {
				const meta: unknown = JSON.parse(row.metadata_json);
				if (isRecord(meta) && typeof meta.cwd === "string" && meta.cwd.startsWith("/") && meta.source === undefined) {
					bump(votes, meta.cwd);
				}
			}
			const top = Object.entries(votes).sort((a, b) => b[1] - a[1])[0];
			const recorded = top ? top[0] : null;
			const real = recorded === null ? null : normaliseFolder(recorded).path;
			let matchedBy: BankCheck["matchedBy"] = null;
			if (recorded !== null && real !== null) {
				if (projectBankSegment(real) === bank) matchedBy = "realpath";
				else if (projectBankSegment(recorded) === bank) matchedBy = "recorded-path";
				else matchedBy = "none";
			}
			const models = (db.query("SELECT DISTINCT model FROM memory_embeddings").all() as { model: string | null }[]).map(
				row => row.model ?? "(none)",
			);
			const total = db.query("SELECT COUNT(*) AS n FROM working_memory").get() as { n: number };
			banks.push({
				bank,
				schema: diffs === null ? "not checked" : diffs.length === 0 ? "ok" : diffs,
				rows: total.n,
				embeddingModels: models,
				cwd: recorded,
				cwdRealpath: real,
				cwdVotes: top?.[1] ?? 0,
				matchedBy,
				bankIdMatches: matchedBy === null ? null : matchedBy !== "none",
			});
		} finally {
			db.close();
		}
	}
	return {
		bun: { path: process.execPath, version: Bun.version },
		library,
		selfTest,
		banks,
		schemaMismatches: banks.filter(bank => Array.isArray(bank.schema)).length,
		bankIdMatches: banks.filter(bank => bank.matchedBy === "realpath").length,
		bankIdMatchedOnlyByRecordedPath: banks.filter(bank => bank.matchedBy === "recorded-path").length,
		bankIdMismatches: banks.filter(bank => bank.matchedBy === "none").length,
		bankIdUnknown: banks.filter(bank => bank.matchedBy === null).length,
	};
}
