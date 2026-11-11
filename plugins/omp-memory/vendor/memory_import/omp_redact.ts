/**
 * `redactMemorySecrets`: the credential-shaped token redaction omp applies to every memory write. The importer runs it
 * on imported text so that a row never holds anything omp's own retain would have scrubbed.
 *
 * This is a copy of `redactMemorySecrets` and the helpers it needs from omp's coding agent
 * (packages/coding-agent/src/memory-backend/redact.ts, MIT License, Copyright (c) 2025 Mario Zechner,
 * (c) 2025-2026 Can Bölük, (c) 2026 Stencil Labs, Inc.; https://github.com/can1357/oh-my-pi). It is copied, not imported,
 * because that file ships in the coding agent, not in the published `@oh-my-pi/pi-mnemopi` package the importer links
 * against. test/redact.test.ts pins its behaviour with fixed vectors.
 */

// Fixed-prefix provider tokens. Each is anchored on a literal, so it matches in one
// pass with no backtracking.
const PATTERNS = [
	/(?:AKIA|ASIA)[A-Z0-9]{16}/g,
	// Common provider token prefixes (GitHub, npm, Slack, Google).
	/(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/g,
	/github_pat_[A-Za-z0-9_]{20,}/g,
	/npm_[A-Za-z0-9]{30,}/g,
	/xox[baprs]-[A-Za-z0-9-]{10,}/g,
	/AIza[A-Za-z0-9_-]{30,}/g,
];

// Longest first, so `token_` wins over `tok` and reports the full match start.
const KEYWORDS = ["password", "secret", "token", "key", "tok", "sk", "pk", "rk"];
// A segment mixing letters and digits is credential-like at 12 characters. Letters
// alone need 16, which still catches `password-supersecretvalue` and
// `token-abcdefghijklmnop` while leaving `authentication` and `configuration` alone.
const MIN_MIXED_SEGMENT = 12;
const MIN_LETTERS_SEGMENT = 16;

const isDelimiter = (code: number) => code === 45 || code === 95;

function isCredentialSegment(length: number, letter: boolean, digit: boolean): boolean {
	if (!letter && !digit) return false;
	if (letter && digit) return length >= MIN_MIXED_SEGMENT;
	return length >= MIN_LETTERS_SEGMENT;
}
const isTokenChar = (code: number) =>
	(code >= 48 && code <= 57) || (code >= 65 && code <= 90) || (code >= 97 && code <= 122) || isDelimiter(code);

function keywordStart(input: string, delimiter: number): number {
	for (const keyword of KEYWORDS) {
		const start = delimiter - keyword.length;
		if (start >= 0 && input.startsWith(keyword, start)) return start;
	}
	return -1;
}

/**
 * Redact a keyword, a delimiter, and the credential-looking run after it, as in
 * `secret_aB3dEfGh1JkLmN`.
 *
 * A regex cannot express "some later segment of this run mixes letters and digits"
 * without a lookahead over the tail, which rescans it once per candidate. Text like
 * `token_aaaa-token_aaaa-…` is one unbroken run, so that rescan is quadratic, and
 * `retainMessages` runs this synchronously over a whole transcript. Instead each run is
 * visited once: split it into segments, mark which ones look like credentials, then
 * sweep the flags backwards so every delimiter can be judged in constant time.
 */
function redactKeywordSecrets(input: string): string {
	let out = "";
	let copied = 0;
	let index = 0;
	while (index < input.length) {
		if (!isTokenChar(input.charCodeAt(index))) {
			index++;
			continue;
		}

		const runStart = index;
		const starts: number[] = [runStart];
		const credential: boolean[] = [];
		let length = 0;
		let letter = false;
		let digit = false;
		while (index < input.length && isTokenChar(input.charCodeAt(index))) {
			const current = input.charCodeAt(index);
			if (isDelimiter(current)) {
				credential.push(isCredentialSegment(length, letter, digit));
				starts.push(index + 1);
				length = 0;
				letter = false;
				digit = false;
			} else {
				length++;
				if (current >= 48 && current <= 57) digit = true;
				else letter = true;
			}
			index++;
		}
		credential.push(isCredentialSegment(length, letter, digit));
		const runEnd = index;

		// suffix[k] answers "does any segment from k onwards look like a credential".
		const suffix: boolean[] = Array.from({ length: credential.length + 1 }, () => false);
		for (let k = credential.length - 1; k >= 0; k--) suffix[k] = suffix[k + 1] || credential[k]!;

		for (let k = 1; k < starts.length; k++) {
			if (!suffix[k]) continue;
			const start = keywordStart(input, starts[k]! - 1);
			if (start < 0 || start < copied || start < runStart) continue;
			out += `${input.slice(copied, start)}[REDACTED]`;
			copied = runEnd;
			break;
		}
	}
	return copied === 0 ? input : out + input.slice(copied);
}

const MIN_JWT_SEGMENT = 16;

/**
 * Redact `header.payload.signature` tokens.
 *
 * The regex this replaces, `[A-Za-z0-9_-]{16,}\.` twice over, re-scanned the tail from
 * every start position when the text held long identifier runs and no dots, which is
 * what a coding transcript looks like. Dots are sparse, so anchoring on them and
 * measuring outward visits each character a constant number of times.
 */
function redactJwts(input: string): string {
	let out = "";
	let copied = 0;
	let dot = input.indexOf(".");
	while (dot > 0) {
		let start = dot;
		while (start > copied && isTokenChar(input.charCodeAt(start - 1))) start--;
		let middle = dot + 1;
		while (middle < input.length && isTokenChar(input.charCodeAt(middle))) middle++;
		let end = middle + 1;
		while (end < input.length && isTokenChar(input.charCodeAt(end))) end++;
		const looksLikeJwt =
			dot - start >= MIN_JWT_SEGMENT &&
			input.charCodeAt(middle) === 46 &&
			middle - dot - 1 >= MIN_JWT_SEGMENT &&
			end - middle - 1 >= MIN_JWT_SEGMENT;
		if (looksLikeJwt) {
			out += `${input.slice(copied, start)}[REDACTED]`;
			copied = end;
			dot = input.indexOf(".", end);
			continue;
		}
		dot = input.indexOf(".", dot + 1);
	}
	return copied === 0 ? input : out + input.slice(copied);
}

export function redactMemorySecrets(input: string): string {
	let out = redactJwts(redactKeywordSecrets(input));
	for (const pattern of PATTERNS) out = out.replace(pattern, "[REDACTED]");
	return out;
}
