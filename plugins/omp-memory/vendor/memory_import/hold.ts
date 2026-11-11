/**
 * HOLD stage, after redaction. An item with a finding here is not imported: it is listed by originKey and finding
 * kind (never by text) and can be released one by one after a human review (`--release-held`).
 *
 * Deliberately stricter than the proxy redactor, which misses things like `user@host / Password1!`. Every value rule
 * has a false-positive guard: a value counts only if it looks like a secret (a digit, a symbol, or mixed case), is not a
 * placeholder (`[REDACTED...]`, `<x>`, `$X`, `{x}`), and is not a URL; plain prose such as "the password is required" or a
 * markdown link `[token](https://...)` does not hold.
 */
import { redactMemorySecrets } from "./omp_redact";

export const HOLD_KINDS = [
	"hold:password-value",
	"hold:credential-pair",
	"hold:private-key",
	"hold:bearer-token",
	"hold:token-prefix",
	"hold:url-credentials",
	"hold:long-token",
] as const;
export type HoldKind = (typeof HOLD_KINDS)[number];

/** A value that could be a secret: not a placeholder or URL, and with a digit, a symbol or mixed case. */
export function looksLikeSecret(raw: string): boolean {
	const value = raw.replace(/^[`'"([{*_~]+/, "").replace(/[`'")\]}*_~,.;:?]+$/, "");
	if (value.length < 4) return false;
	if (raw.startsWith("[REDACTED") || /^(<|\$|\{|\/\/|https?:)/i.test(raw.replace(/^[`'"*_~]+/, ""))) return false;
	return /\d/.test(value) || /[^A-Za-z0-9]/.test(value) || (/[a-z]/.test(value) && /[A-Z]/.test(value));
}

const PASSWORD_WORD = /\b(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key)\b/gi;
// separator within 40 chars: `:` `=` `is` `->` ; the gap itself may not contain link or call punctuation
const SEPARATOR = /^([^\n\]()]{0,40}?)(?::|=|\bis\b|->)\s*(\S{4,})/i;
const PAIR = /\b(?:login|ssh|username|user|root|admin)\b[^\n]{0,40}?[\w.@+-]+\s+\/\s+(\S{4,})/gi;
const PRIVATE_KEY = /-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----/;
const BEARER = /\bBearer\s+[A-Za-z0-9._~+/=-]{20,}/;
const PREFIX = /\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{10,}|xox[bp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,})/;
const URL_CREDENTIALS = /:\/\/[^\s/@:]+:([^\s/@]{3,})@/;
const KEY_WORD = /\b(?:key|token|secret)\b/gi;
const LONG_TOKEN = /^[^\n]{0,30}?(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{24,})(?![A-Za-z0-9_-])/;
const EXAMPLE_PASSWORDS = new Set(["password", "pass", "passwd", "pwd", "secret", "xxxx", "xxxxx", "****"]);

export function findHolds(text: string): HoldKind[] {
	const found = new Set<HoldKind>();
	for (const match of text.matchAll(PASSWORD_WORD)) {
		const rest = text.slice((match.index ?? 0) + match[0].length);
		const parsed = SEPARATOR.exec(rest);
		if (parsed && looksLikeSecret(parsed[2] ?? "")) found.add("hold:password-value");
	}
	for (const match of text.matchAll(PAIR)) {
		if (looksLikeSecret(match[1] ?? "")) found.add("hold:credential-pair");
	}
	if (PRIVATE_KEY.test(text)) found.add("hold:private-key");
	if (BEARER.test(text)) found.add("hold:bearer-token");
	if (PREFIX.test(text)) found.add("hold:token-prefix");
	const url = URL_CREDENTIALS.exec(text);
	if (url && !EXAMPLE_PASSWORDS.has((url[1] ?? "").toLowerCase()) && !(url[1] ?? "").startsWith("[REDACTED")) {
		found.add("hold:url-credentials");
	}
	for (const match of text.matchAll(KEY_WORD)) {
		const rest = text.slice((match.index ?? 0) + match[0].length);
		const token = LONG_TOKEN.exec(rest)?.[1];
		if (token && ((/[a-z]/.test(token) && /[A-Z]/.test(token)) || /\d/.test(token))) found.add("hold:long-token");
	}
	return HOLD_KINDS.filter((kind) => found.has(kind));
}

/**
 * Provenance holds: a string that comes from a source and is persisted (origin, origin key, folder, source extras) or printed
 * is checked like the text is. An item with a finding here is held, listed by an index only, and cannot be released: the fix
 * is to rename or remove what carries the secret, not to import it.
 */
export const PROVENANCE_SECRET = "hold:provenance-secret";
export const PROVENANCE_UNPRINTABLE = "hold:provenance-unprintable";

const KEYWORDS = ["password", "passwd", "pwd", "passphrase", "secret", "token", "apikey"];
const FILLER = ["is", "are", "was", "equals", "eq"];

/** `text` with its percent escapes decoded (twice at most, and one escape at a time when the whole is malformed). */
function urlDecoded(text: string): string {
	let out = text;
	for (let round = 0; round < 2; round += 1) {
		let next: string;
		try {
			next = decodeURIComponent(out);
		} catch {
			next = out.replace(/%([0-9a-f]{2})/gi, (_all, hex: string) => String.fromCharCode(Number.parseInt(hex, 16)));
		}
		if (next === out) break;
		out = next;
	}
	return out;
}

/** A value after a key word in a name: three or more characters with a digit, or four or more with a capital after the first letter. */
function valueInName(value: string): boolean {
	return (value.length >= 3 && /\d/.test(value)) || (value.length >= 4 && /[a-z]/.test(value) && /[A-Z]/.test(value.slice(1)));
}

/**
 * A key word followed by a value in a name, whatever separates them (`=`, `:`, `-`, `_`, `.`, a space, any other character):
 * `password-123456`, `Token_a1B2c3`, `api key 9f8e`, `secret.2024`, and a number glued to the word (`password123456`). A word that
 * only begins like a key word, or is followed by a plain word (`token-service`, `Secret Santa`), is not a finding.
 */
function keywordWithValue(text: string): boolean {
	const tokens = text.split(/[^\p{L}\p{N}]+/u).filter(token => token !== "");
	for (let index = 0; index < tokens.length; index += 1) {
		let word = (tokens[index] ?? "").toLowerCase();
		let next = index + 1;
		if (word === "api" && (tokens[next] ?? "").toLowerCase() === "key") {
			word = "apikey";
			next += 1;
		}
		if (KEYWORDS.includes(word)) {
			if (FILLER.includes((tokens[next] ?? "").toLowerCase())) next += 1;
			if (valueInName(tokens[next] ?? "")) return true;
			continue;
		}
		const glued = KEYWORDS.find(key => word.startsWith(key));
		if (glued !== undefined && /^\d{3,}$/.test(word.slice(glued.length))) return true;
	}
	return false;
}

/**
 * Whether a string that is going to be stored or shown carries a secret: a file name, a folder path, a tag. It is judged on the
 * RAW string, never on a sanitised form of it, and in each form a separator or an escape could hide it in: as written, with
 * its percent escapes decoded, and each of those in lower case; the whole string and each path segment by itself, through the
 * HOLD rules and omp's own redaction; and through {@link keywordWithValue} for names in which `=` and `:` became `-` or `_`.
 */
export function carriesSecret(raw: string): boolean {
	const decoded = urlDecoded(raw);
	const forms = [...new Set([raw, decoded, raw.toLowerCase(), decoded.toLowerCase()])];
	for (const form of forms) {
		for (const piece of [form, ...form.split("/")]) {
			if (piece === "") continue;
			if (findHolds(piece).length > 0 || redactMemorySecrets(piece) !== piece) return true;
		}
	}
	for (const form of [raw, decoded]) {
		for (const segment of form.split("/")) if (keywordWithValue(segment)) return true;
	}
	return false;
}
