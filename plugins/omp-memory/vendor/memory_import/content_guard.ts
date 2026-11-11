/**
 * Content that is never imported, decided on the text as read (before redaction), so that no source file is ever
 * pushed through the redaction step or into a bank unless it is text a person wrote:
 *
 *  - binary: a NUL byte, or more than 2% replacement characters (U+FFFD, what decoding invalid UTF-8 leaves behind);
 *  - too large: more than `MAX_TEXT_CHARS` characters, the longest text the redaction step accepts
 *    (`hindsight_proxy.redact.MAX_TEXT_CHARS`; a test compares the two numbers).
 */
export const MAX_TEXT_CHARS = 1_000_000;

/** More than this fraction of replacement characters means the file was not text. */
export const MAX_REPLACEMENT_FRACTION = 0.02;

/** Why `text` is binary, or null. */
export function binaryReason(text: string): string | null {
	if (text.includes("\0")) return "it contains a NUL byte";
	if (text.length === 0) return null;
	const replacements = text.split("\uFFFD").length - 1;
	if (replacements / text.length > MAX_REPLACEMENT_FRACTION) {
		return `${((replacements / text.length) * 100).toFixed(1)}% of its characters are not valid text (limit ${MAX_REPLACEMENT_FRACTION * 100}%)`;
	}
	return null;
}

/** The number of characters (code points, as the redaction step counts them) when `text` exceeds the limit, else null. */
export function charsOverLimit(text: string): number | null {
	if (text.length <= MAX_TEXT_CHARS) return null; // UTF-16 units are never fewer than code points
	let count = 0;
	for (const _ of text) count += 1;
	return count > MAX_TEXT_CHARS ? count : null;
}

export const describeTooLarge = (chars: number): string =>
	`${chars.toLocaleString("en-US")} characters; the limit is ${MAX_TEXT_CHARS.toLocaleString("en-US")} characters (the redaction step refuses longer text)`;
