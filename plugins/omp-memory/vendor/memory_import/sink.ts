/**
 * The importer's one output sink. Everything the importer prints (stdout, stderr, the message of a failure as the command line
 * shows it, progress lines) or writes as a report goes through here, and the label policy is applied at this one place: a string
 * that carries a recognised secret (see `carriesSecret`: the raw string, its percent-decoded and lower-case forms, each path
 * segment and each word of it) is replaced by an index label such as `secret#2`, control characters (terminal escape
 * sequences) are removed, and a failure is shown as its message only, never as a stack.
 *
 * test/output-sink.test.ts reads every other source file and fails on a direct `console.*`, `process.stdout/stderr`, file write
 * or interpolated `throw new Error(...)`: use `emit.*`, `writeOutput` (outputs.ts) and `fail(...)` instead.
 */
import { readFileSync } from "node:fs";
import { carriesSecret } from "./hold";
import { Refusal, UsageError } from "./model";

const labelIndexes = new Map<string, Map<string, number>>();

/** Starts a command: the numbering of withheld labels begins again. */
export function resetLabelIndexes(): void {
	labelIndexes.clear();
}

/**
 * `kind#N`: what the importer says instead of something it must not show. N counts the distinct values of that kind in the order
 * a command first meets them, so a label carries nothing of the value (no hash, no length, no characters): a short secret cannot
 * be recovered from it, and the same label for the same value is only meaningful inside one run.
 */
export function indexedLabel(kind: string, value: string): string {
	let index = labelIndexes.get(kind);
	if (index === undefined) {
		index = new Map();
		labelIndexes.set(kind, index);
	}
	let n = index.get(value);
	if (n === undefined) {
		n = index.size + 1;
		index.set(value, n);
	}
	return `${kind}#${n}`;
}

const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;
const CONTROLS = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]+/g;

/** Whether `text` holds a control character (a terminal escape sequence, a newline, a NUL). */
export const hasControlCharacters = (text: string): boolean => CONTROL.test(text);

/** A string from a source, safe to print on one line: control characters replaced by one space and the length limited. */
export function printable(text: string, max = 300): string {
	const flat = text.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim();
	return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** One line of text, with every word that carries a secret replaced; a line that still carries one is withheld whole. */
function scrubLine(line: string): string {
	if (!carriesSecret(line)) return line;
	const replaced = line
		.split(/(\s+)/)
		.map(word => (/^\s*$/.test(word) || !carriesSecret(word) ? word : indexedLabel("secret", word)))
		.join("");
	return carriesSecret(replaced) ? `(withheld: ${indexedLabel("line", line)})` : replaced;
}

/** `text` as the importer may show it: no control characters (a newline and a tab stay), no recognised secret. */
export function scrubText(text: string): string {
	return text.replace(CONTROLS, " ").split("\n").map(scrubLine).join("\n");
}

/** `value` with the label policy applied to every string in it (and every key): the shape and the numbers stay. */
export function scrubValue<T>(value: T, map: (text: string) => string = scrubText): T {
	if (typeof value === "string") return map(value) as T;
	if (Array.isArray(value)) return value.map(item => scrubValue(item, map)) as T;
	if (value !== null && typeof value === "object") {
		const out: Record<string, unknown> = {};
		for (const [key, item] of Object.entries(value)) out[map(key)] = scrubValue(item, map);
		return out as T;
	}
	return value;
}

export interface EmitOptions {
	/** Show paths below the home directory as `~/...` (the form every command printed before). */
	tilde?: (text: string) => string;
}

/** Everything the command line prints. One line per call; stdout carries the result, stderr the progress and the failure. */
export const emit = {
	out(text: string): void {
		console.log(scrubText(text));
	},
	err(text: string): void {
		console.error(scrubText(text));
	},
	/** A JSON result on stdout. */
	json(value: unknown, options: EmitOptions = {}): void {
		const text = options.tilde;
		console.log(JSON.stringify(scrubValue(value, text === undefined ? scrubText : raw => scrubText(text(raw))), null, 2));
	},
};

/**
 * A failure stated as a category and the labels of what it is about, never as raw source-controlled text: each label is
 * scrubbed, so a path, a project key or a bank name that carries a secret is shown as an index label. Exit status 1.
 */
export function fail(category: string, ...labels: (string | number)[]): never {
	throw new Refusal(failureText(category, labels));
}

/** The same for a wrong flag or argument (exit status 2). */
export function failUsage(category: string, ...labels: (string | number)[]): never {
	throw new UsageError(failureText(category, labels));
}

function failureText(category: string, labels: (string | number)[]): string {
	return labels.length === 0 ? category : `${category}: ${labels.map(label => scrubText(String(label))).join(", ")}`;
}

/** Reads a JSON file; a file that cannot be read or parsed fails with a category and the file's label, never with what the parser saw. */
export function readJson(file: string, what: string): unknown {
	let text: string;
	try {
		text = readFileSync(file, "utf8");
	} catch (error) {
		return fail(`${what} cannot be read (${(error as NodeJS.ErrnoException).code ?? "error"})`, file);
	}
	try {
		return JSON.parse(text);
	} catch {
		return fail(`${what} is not valid JSON`, file);
	}
}

/** The message of any failure as the command line shows it: the label policy applied, one message, no stack. */
export function describeFailure(error: unknown): string {
	const message = error instanceof Error ? error.message : String(error);
	return scrubText(message.length > 2000 ? `${message.slice(0, 2000)}…` : message);
}

/** Says that something that ran on its own (a cleanup, a handler) failed: one line, the message under the label policy. Never throws. */
export function reportCleanupFailure(what: string, error: unknown): void {
	try {
		emit.err(`unexpected failure (${what}): ${describeFailure(error)}`);
	} catch {
		// stderr is closed: nothing left to say it to
	}
}

/**
 * The body of a process-level callback (an exit hook, a signal handler) with its failure caught and reported through the sink, so
 * that it never escapes as a second, unfiltered failure. With `exitCode` the process exits afterwards: with that status when the
 * body succeeded, with status 3 when it failed.
 */
export function guarded(what: string, body: () => void, exitCode?: number): () => void {
	return () => {
		let failed = false;
		try {
			body();
		} catch (error) {
			failed = true;
			reportCleanupFailure(what, error);
		}
		if (exitCode !== undefined) process.exit(failed ? 3 : exitCode);
	};
}

export type ProcessEvent = "exit" | "SIGINT" | "SIGTERM";

/** Registers a process-level callback (wrap its body with {@link guarded}). */
export function onProcessEvent(event: ProcessEvent, handler: () => void, once = false): void {
	if (once) process.once(event, handler);
	else process.on(event, handler);
}

export function offProcessEvent(event: ProcessEvent, handler: () => void): void {
	process.off(event, handler);
}
