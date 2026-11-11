/**
 * Claude Code memory files: `<claude home>/<.claude root>/projects/<project dir>/memory/<name>.md` (MEMORY.md is an index, not a
 * memory). Each file has optional front matter (`name`, `description`, `type`, plus volatile `originSessionId` /
 * `modified`) and a body. The folder comes from the project directory (see folders.ts).
 */
import { closeSync, constants, existsSync, fstatSync, openSync, readdirSync, readSync, realpathSync, statSync } from "node:fs";
import path from "node:path";
import { MAX_TEXT_CHARS } from "../content_guard";
import { type FolderResolution, resolveProjectFolder } from "../folders";
import { type Collected, normaliseText, type ParseError, sha256, type SourceItem } from "../model";
import { isInside } from "../outputs";

const TYPES = new Set(["user", "feedback", "project", "reference"]);
const VOLATILE = new Set(["originsessionid", "modified"]);

export interface ClaudeMemoryOptions {
	/** Directory that holds the `.claude*` roots (default: the home directory). */
	claudeHome: string;
}

export interface ParsedMemoryFile {
	front: Record<string, string>;
	body: string;
	problem: string | null;
}

/** Minimal front matter reader: `---` fences around `key: value` lines; one nested level is kept as `parent.key`. */
export function parseMemoryFile(text: string): ParsedMemoryFile {
	const lines = text.replace(/\r\n?/g, "\n").split("\n");
	if (lines[0]?.trim() !== "---") return { front: {}, body: text.trim(), problem: null };
	const close = lines.findIndex((line, index) => index > 0 && line.trim() === "---");
	if (close < 0) return { front: {}, body: text.trim(), problem: "front matter is never closed; whole file kept as text" };
	const front: Record<string, string> = {};
	let parent = "";
	for (const line of lines.slice(1, close)) {
		const match = /^(\s*)([A-Za-z0-9_-]+)\s*:\s*(.*)$/.exec(line);
		if (!match) continue;
		const key = (match[2] ?? "").toLowerCase();
		const value = (match[3] ?? "").trim().replace(/^(['"])(.*)\1$/, "$2");
		if ((match[1] ?? "") === "") {
			parent = value === "" ? key : "";
			front[key] = value;
		} else if (parent !== "") front[`${parent}.${key}`] = value;
	}
	return { front, body: lines.slice(close + 1).join("\n").trim(), problem: null };
}

/** The hash input: front matter without the volatile fields, then the body, normalised. */
export function memoryHashInput(parsed: ParsedMemoryFile): string {
	const stable = Object.entries(parsed.front)
		.filter(([key]) => !VOLATILE.has(key))
		.sort(([a], [b]) => a.localeCompare(b))
		.map(([key, value]) => `${key}: ${value}`)
		.join("\n");
	return normaliseText(`${stable}\n---\n${parsed.body}`);
}

function claudeRoots(claudeHome: string): string[] {
	let names: string[];
	try {
		names = readdirSync(claudeHome);
	} catch {
		return [];
	}
	return names
		.filter(name => name === ".claude" || name.startsWith(".claude-"))
		.sort()
		.map(name => path.join(claudeHome, name, "projects"))
		.filter(dir => existsSync(dir));
}

export function projectDirsByName(claudeHome: string): Map<string, string[]> {
	const byName = new Map<string, string[]>();
	for (const root of claudeRoots(claudeHome)) {
		let names: string[];
		try {
			names = readdirSync(root);
		} catch {
			continue;
		}
		for (const name of names) {
			const dir = path.join(root, name);
			try {
				if (!statSync(dir).isDirectory()) continue;
			} catch {
				continue;
			}
			byName.set(name, [...(byName.get(name) ?? []), dir]);
		}
	}
	return byName;
}

/** The longest memory file that is read: UTF-8 takes at most 4 bytes per character, so a larger file is over the character limit whatever it holds. */
export const MAX_FILE_BYTES = MAX_TEXT_CHARS * 4;

function skipFile(collected: Pick<Collected, "excluded" | "warnings">, where: string, reason: string): void {
	collected.excluded[reason] = (collected.excluded[reason] ?? 0) + 1;
	collected.warnings.push({ where, error: `skipped: ${reason}` });
}

/** The real path of a project's memory directory; null when there is none, or when it is a link that leads out of its project. */
function memoryDirectoryOf(projectDir: string, memoryDir: string, collected: Collected): string | null {
	let real: string;
	let project: string;
	try {
		real = realpathSync(memoryDir);
		project = realpathSync(projectDir);
	} catch {
		return null; // no memory directory
	}
	if (!isInside(project, real)) {
		skipFile(collected, memoryDir, "memory directory leading outside its project");
		return null;
	}
	return real;
}

type FileRead = { text: string; mtimeMs: number } | { skipped: string };

/**
 * Reads a memory file through one descriptor: it must be a regular file (a FIFO or a device is never read, and opening a FIFO
 * does not wait for a writer), and at most MAX_FILE_BYTES are read whatever size it claims.
 */
function readBounded(real: string): FileRead {
	const fd = openSync(real, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
	try {
		const stat = fstatSync(fd);
		if (!stat.isFile()) return { skipped: "not a regular file" };
		const chunks: Buffer[] = [];
		let total = 0;
		for (;;) {
			const chunk = Buffer.allocUnsafe(64 * 1024);
			const n = readSync(fd, chunk, 0, chunk.length, null);
			if (n === 0) break;
			total += n;
			if (total > MAX_FILE_BYTES) return { skipped: `larger than ${MAX_FILE_BYTES} bytes (not read)` };
			chunks.push(chunk.subarray(0, n));
		}
		return { text: Buffer.concat(chunks).toString("utf8"), mtimeMs: stat.mtimeMs };
	} finally {
		closeSync(fd);
	}
}

export async function collectClaudeMemory(options: ClaudeMemoryOptions): Promise<Collected> {
	const collected: Collected = { items: [], errors: [], warnings: [], unresolved: [], excluded: {} };
	const seenReal = new Set<string>();
	for (const [dirName, dirs] of [...projectDirsByName(options.claudeHome)].sort(([a], [b]) => a.localeCompare(b))) {
		const files: { file: string; real: string; root: string }[] = [];
		for (const dir of dirs) {
			const memoryDir = path.join(dir, "memory");
			const memoryReal = memoryDirectoryOf(dir, memoryDir, collected);
			if (memoryReal === null) continue;
			let names: string[];
			try {
				names = readdirSync(memoryReal).filter(name => name.endsWith(".md"));
			} catch {
				continue;
			}
			for (const name of names.sort()) {
				if (name === "MEMORY.md") {
					collected.excluded["MEMORY.md index"] = (collected.excluded["MEMORY.md index"] ?? 0) + 1;
					continue;
				}
				const file = path.join(memoryDir, name);
				let real: string;
				try {
					real = realpathSync(file);
				} catch (error) {
					collected.errors.push({ where: file, error: `unreadable: ${String(error)}` });
					continue;
				}
				if (!isInside(memoryReal, real)) {
					skipFile(collected, file, "symlink leading outside the memory directory");
					continue;
				}
				if (seenReal.has(real)) {
					collected.excluded["symlink seen twice"] = (collected.excluded["symlink seen twice"] ?? 0) + 1;
					continue;
				}
				seenReal.add(real);
				files.push({ file, real, root: path.basename(path.dirname(path.dirname(dir))) });
			}
		}
		if (files.length === 0) continue;
		const resolution = await resolveProjectFolder(dirName, dirs);
		if (resolution.folder === null) {
			collected.unresolved.push({
				where: dirName,
				reason: resolution.reason ?? "unresolved",
				candidates: resolution.candidates,
				items: files.length,
			});
		}
		if (resolution.folder !== null && !resolution.folder.exists) {
			collected.warnings.push({
				where: dirName,
				error: `folder ${resolution.folder.path} no longer exists (${files.length} items go to a bank omp opens only if the folder is recreated)`,
			});
		}
		const firstOfStem = new Map<string, string>();
		for (const { file, real, root } of files) {
			const item = readMemoryFile(file, real, root, dirName, resolution, firstOfStem, collected);
			if (item) collected.items.push(item);
		}
	}
	return collected;
}

function readMemoryFile(
	file: string,
	real: string,
	root: string,
	dirName: string,
	resolution: FolderResolution,
	firstOfStem: Map<string, string>,
	collected: Collected,
): SourceItem | null {
	const { errors, warnings } = collected;
	let text: string;
	let mtime: number;
	try {
		const read = readBounded(real);
		if ("skipped" in read) {
			skipFile(collected, file, read.skipped);
			return null;
		}
		text = read.text;
		mtime = read.mtimeMs;
	} catch (error) {
		errors.push({ where: file, error: `unreadable: ${String(error)}` });
		return null;
	}
	const parsed = parseMemoryFile(text);
	if (parsed.problem) warnings.push({ where: file, error: parsed.problem });
	if (parsed.body === "" && Object.keys(parsed.front).length === 0) {
		errors.push({ where: file, error: "empty file" });
		return null;
	}
	const stem = path.basename(file, ".md");
	const name = parsed.front.name?.trim() || stem;
	const description = parsed.front.description?.trim() || null;
	const rawType = (parsed.front.type ?? parsed.front["metadata.type"] ?? "").trim().toLowerCase();
	const claudeType = TYPES.has(rawType) ? rawType : null;
	if (rawType !== "" && claudeType === null) warnings.push({ where: file, error: "unknown type (value not shown)" });
	const modified = Date.parse(parsed.front.modified ?? "");
	const timestamp = new Date(Number.isNaN(modified) ? mtime : modified).toISOString();
	const folderKey = resolution.folder?.path ?? `unassigned:${dirName}`;
	// Two roots can hold a same-named file for the same folder (the files differ): the later root gets its own key.
	const baseKey = `${folderKey}#${stem}`;
	const firstRoot = firstOfStem.get(baseKey);
	if (firstRoot === undefined) firstOfStem.set(baseKey, root);
	const originKey = firstRoot === undefined || firstRoot === root ? baseKey : `${baseKey}@${root}`;
	const content = [name, description, "", parsed.body].filter(part => part !== null).join("\n").replace(/\n{3,}/g, "\n\n").trim();
	return {
		source: "claude-code",
		sourceKind: "memory",
		originKey,
		origin: file,
		originHash: sha256(memoryHashInput(parsed)),
		name,
		description,
		claudeType,
		content,
		timestamp,
		cwd: resolution.folder?.path ?? null,
		cwdExists: resolution.folder?.exists ?? false,
		cwdRaw: resolution.folder?.raw ?? null,
		folderHow: resolution.how,
		extra: { root, projectDir: dirName },
		importance: 0.75,
		memoryType: "fact",
	};
}
