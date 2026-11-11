/**
 * The Hindsight remainder: what is in Hindsight now and did NOT come from mnemopi.
 *
 * Input is a JSON export file (written by memory_ops/export_hindsight.sh from `/memories/list`): either
 * `{"banks": {"<bank>": [item, ...]}}`, `{"bank": "<bank>", "items": [...]}`, or a bare array of items that each carry
 * `bank`. An item is the shape of Hindsight's list rows: `id`, `text`, `fact_type`, `state`, `tags`, `context`,
 * `document_id`, `date` / `mentioned_at`, optional `metadata`.
 *
 * Only `fact_type` world or experience with state valid are imported (observations are derived, invalidated rows
 * are retired). Items that originated from mnemopi are excluded; see {@link mnemopiOrigin}.
 *
 * Folders: a `project:<name>` tag is mapped with an explicit, reviewable project map (`project-map` command writes
 * a draft). A name with no entry, an entry of null, two different project tags, or no project tag at all land in
 * the `unassigned` bank.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { normaliseFolder } from "../folders";
import { bump, type Collected, isRecord, normaliseText, sha256, type SourceItem } from "../model";
import { safeLabel } from "../outputs";
import { fail, indexedLabel, readJson } from "../sink";

export const IMPORTED_FACT_TYPES: Readonly<Record<string, true>> = { world: true, experience: true };
const PROJECT_TAG = "project:";

export interface HindsightItem {
	bank: string;
	id: string;
	text: string;
	factType: string;
	state: string | null;
	invalidatedAt: string | null;
	tags: string[];
	context: string | null;
	documentId: string | null;
	date: string | null;
	metadata: Record<string, unknown>;
}

export interface ImportLedgerFile {
	documents: Record<string, unknown>;
	contents: Record<string, unknown>;
	sources: Record<string, unknown>;
}

/** The Hindsight import's own ledger (`import-ledger.json`: which mnemopi rows it sent, as which document id). */
export function loadHindsightLedger(file: string): ImportLedgerFile {
	if (!existsSync(file)) return { documents: {}, contents: {}, sources: {} };
	const parsed = readJson(file, "the Hindsight import ledger");
	if (!isRecord(parsed)) fail("not a Hindsight import ledger", file);
	const documents = isRecord(parsed.documents) ? parsed.documents : {};
	return {
		documents,
		contents: isRecord(parsed.contents) ? parsed.contents : {},
		sources: isRecord(parsed.sources) ? parsed.sources : {},
	};
}

/**
 * Why an item is known to have come from mnemopi, or null. mnemopi rows were sent by hindsight_import with
 * `document_id = "mnemopi:<bank>:<label>:<w|e>:<id>"`, `context = "mnemopi import: <source>"` and a
 * `mnemopi_table` metadata key; Hindsight facts are rewritten text, so the content hash cannot be used. Each fact
 * extracted from such a document keeps the document id. Any one of the signals excludes the item.
 */
export function mnemopiOrigin(item: HindsightItem, ledger: ImportLedgerFile): string | null {
	const documentId = item.documentId ?? "";
	if (documentId !== "" && Object.hasOwn(ledger.documents, documentId)) return "document id in the Hindsight import ledger";
	if (documentId.startsWith("mnemopi:")) return 'document id starts with "mnemopi:"';
	if ((item.context ?? "").startsWith("mnemopi import:")) return 'context starts with "mnemopi import:"';
	if (Object.hasOwn(item.metadata, "mnemopi_table")) return "metadata has mnemopi_table";
	return null;
}

function text(value: unknown): string | null {
	return typeof value === "string" && value.trim() !== "" ? value : null;
}

function toItem(raw: unknown, bank: string | null): HindsightItem | string {
	if (!isRecord(raw)) return "item is not an object";
	const itemBank = text(raw.bank) ?? bank;
	const id = text(raw.id);
	if (itemBank === null) return "item has no bank";
	if (id === null) return "item has no id";
	const tags = Array.isArray(raw.tags) ? raw.tags.filter((tag): tag is string => typeof tag === "string") : [];
	return {
		bank: itemBank,
		id,
		text: typeof raw.text === "string" ? raw.text : "",
		factType: text(raw.fact_type) ?? text(raw.type) ?? "",
		state: text(raw.state),
		invalidatedAt: text(raw.invalidated_at),
		tags,
		context: text(raw.context),
		documentId: text(raw.document_id),
		date: text(raw.date) ?? text(raw.mentioned_at) ?? text(raw.occurred_start) ?? text(raw.created_at),
		metadata: isRecord(raw.metadata) ? raw.metadata : {},
	};
}

export interface ParsedExport {
	items: HindsightItem[];
	problems: { where: string; error: string }[];
	exportedAt: string | null;
}

export function parseHindsightExport(file: string): ParsedExport {
	const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
	const out: ParsedExport = { items: [], problems: [], exportedAt: null };
	const add = (list: unknown, bank: string | null, where: string): void => {
		if (!Array.isArray(list)) {
			out.problems.push({ where, error: "not a list of items" });
			return;
		}
		for (const [index, raw] of list.entries()) {
			const item = toItem(raw, bank);
			if (typeof item === "string") out.problems.push({ where: `${where}[${index}]`, error: item });
			else out.items.push(item);
		}
	};
	if (Array.isArray(parsed)) add(parsed, null, "export");
	else if (isRecord(parsed)) {
		out.exportedAt = text(parsed.exportedAt) ?? text(parsed.exported_at);
		if (isRecord(parsed.banks)) {
			for (const [bank, list] of Object.entries(parsed.banks)) add(list, bank, `banks.${bank}`);
		} else if (typeof parsed.bank === "string") add(parsed.items, parsed.bank, `bank ${parsed.bank}`);
		else out.problems.push({ where: "export", error: 'expected "banks" or "bank"+"items"' });
	} else out.problems.push({ where: "export", error: "unexpected top-level value" });
	return out;
}

export interface ProjectMap {
	projects: Record<string, string | null>;
}

export function loadProjectMap(file: string | undefined): ProjectMap {
	if (file === undefined) return { projects: {} };
	const parsed = readJson(file, "the project map");
	if (!isRecord(parsed) || !isRecord(parsed.projects)) fail('the project map must be {"projects": {...}}', file);
	const projects: Record<string, string | null> = {};
	for (const [name, folder] of Object.entries(parsed.projects)) {
		if (folder !== null && typeof folder !== "string") fail("a project map entry must be a path or null", file, indexedLabel("project", name));
		projects[name.toLowerCase()] = folder;
	}
	return { projects };
}

export interface HindsightOptions {
	exportFile: string;
	ledgerFile: string;
	projectMap: ProjectMap;
}

export function projectNames(item: HindsightItem): string[] {
	return [...new Set(item.tags.filter(tag => tag.startsWith(PROJECT_TAG)).map(tag => tag.slice(PROJECT_TAG.length).toLowerCase()))];
}

export function collectHindsight(options: HindsightOptions): Collected {
	const collected: Collected = { items: [], errors: [], warnings: [], unresolved: [], excluded: {} };
	let parsed: ParsedExport;
	try {
		parsed = parseHindsightExport(options.exportFile);
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		collected.errors.push({ where: options.exportFile, error: code === undefined ? "unreadable export: not valid JSON" : `unreadable export: ${code}` });
		return collected;
	}
	collected.errors.push(...parsed.problems);
	const ledger = loadHindsightLedger(options.ledgerFile);
	const unresolved = new Map<string, { reason: string; candidates: string[]; items: number }>();
	for (const item of parsed.items) {
		if (!Object.hasOwn(IMPORTED_FACT_TYPES, item.factType)) {
			bump(collected.excluded, `fact_type ${item.factType || "(none)"}`);
			continue;
		}
		if (item.invalidatedAt !== null || (item.state !== null && item.state !== "valid")) {
			bump(collected.excluded, `state ${item.state ?? "invalidated"}`);
			continue;
		}
		if (item.text.trim() === "") {
			bump(collected.excluded, "empty text");
			continue;
		}
		const origin = mnemopiOrigin(item, ledger);
		if (origin !== null) {
			bump(collected.excluded, `from mnemopi: ${origin}`);
			continue;
		}
		const names = projectNames(item);
		let cwd: string | null = null;
		let exists = false;
		let raw: string | null = null;
		let how = "unresolved";
		let reason: string | null = null;
		if (names.length === 0) reason = "no project tag (global in Hindsight)";
		else if (names.length > 1) reason = `several project tags: ${names.join(", ")}`;
		else {
			const mapped = options.projectMap.projects[names[0] ?? ""];
			if (typeof mapped === "string") {
				const folder = normaliseFolder(mapped);
				cwd = folder.path;
				exists = folder.exists;
				raw = folder.raw;
				how = "project-map";
			} else {
				reason = mapped === null ? `project "${names[0]}" is mapped to null` : `project "${names[0]}" is not in the project map`;
			}
		}
		if (reason !== null) {
			const entry = unresolved.get(reason) ?? { reason, candidates: [], items: 0 };
			entry.items += 1;
			unresolved.set(reason, entry);
		}
		const parsedDate = Date.parse(item.date ?? parsed.exportedAt ?? "");
		const source: SourceItem = {
			source: "hindsight",
			sourceKind: "memory",
			originKey: `${item.bank}#${item.id}`,
			origin: `hindsight:${item.bank}/${item.id}`,
			originHash: sha256(normaliseText(item.text)),
			name: null,
			description: null,
			claudeType: null,
			content: item.text.trim(),
			timestamp: new Date(Number.isNaN(parsedDate) ? 0 : parsedDate).toISOString(),
			cwd,
			cwdExists: exists,
			cwdRaw: raw,
			folderHow: how,
			extra: {
				hindsightBank: item.bank,
				factType: item.factType,
				documentId: item.documentId,
				tags: item.tags.join(","),
			},
			importance: 0.7,
			memoryType: "fact",
		};
		collected.items.push(source);
	}
	for (const entry of unresolved.values()) collected.unresolved.push({ where: `hindsight ${entry.reason}`, ...entry });
	return collected;
}

/**
 * Draft project map: every `project:<name>` tag in the export, with the one existing folder whose name matches
 * (null when none or several match, with the candidates listed). The result is meant to be reviewed and edited. A tag name or
 * folder that carries a secret or a control character is named by an opaque label (`project:<hex>`, `folder:<hex>`) in the
 * draft, which is printed and written to a file.
 */
export function draftProjectMap(
	exportFile: string,
	folders: string[],
): { projects: Record<string, string | null>; candidates: Record<string, string[]>; counts: Record<string, number> } {
	const names: Record<string, number> = {};
	let items: HindsightItem[];
	try {
		items = parseHindsightExport(exportFile).items;
	} catch {
		return fail("the Hindsight export cannot be read or is not valid JSON", exportFile); // the parser's own message quotes what it saw
	}
	for (const item of items) for (const name of projectNames(item)) bump(names, name);
	const byName: Record<string, string[]> = {};
	for (const folder of [...new Set(folders)].sort()) (byName[path.basename(folder).toLowerCase()] ??= []).push(folder);
	const projects: Record<string, string | null> = {};
	const candidates: Record<string, string[]> = {};
	const counts: Record<string, number> = {};
	for (const name of Object.keys(names).sort()) {
		const found = byName[name] ?? [];
		const label = safeLabel("project", name);
		const only = found.length === 1 ? (found[0] ?? null) : null;
		projects[label] = only === null ? null : safeLabel("folder", only);
		candidates[label] = found.map(folder => safeLabel("folder", folder));
		counts[label] = names[name] ?? 0;
	}
	return { projects, candidates, counts };
}

/** Existing folders one level below each workspace root of a conf file (`<group> <root relative to home>` per line). */
export function workspaceFolders(confFile: string, home: string): string[] {
	const out: string[] = [];
	let lines: string[];
	try {
		lines = readFileSync(confFile, "utf8").split("\n");
	} catch {
		return out;
	}
	for (const line of lines) {
		const fields = line.trim().split(/\s+/);
		if (fields.length < 2 || fields[0]?.startsWith("#")) continue;
		const root = path.join(home, fields[1] ?? "");
		if (!existsSync(root)) continue;
		out.push(root);
		for (const name of readdirSync(root)) {
			const child = path.join(root, name);
			try {
				if (!name.startsWith(".") && statSync(child).isDirectory()) out.push(child);
			} catch {
				// unreadable entry
			}
		}
	}
	return out;
}

/** Existing folders directly below `root` (project folders of a workspace directory the user named). */
export function childFolders(root: string): string[] {
	try {
		return readdirSync(root)
			.filter(name => !name.startsWith("."))
			.map(name => path.join(root, name))
			.filter(child => statSync(child).isDirectory());
	} catch {
		return [];
	}
}
