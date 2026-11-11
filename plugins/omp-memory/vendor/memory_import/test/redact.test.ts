/** The local copy of omp's `redactMemorySecrets`: fixed vectors. */
import { describe, expect, test } from "bun:test";
import { redactMemorySecrets } from "../omp_redact";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

describe("redactMemorySecrets", () => {
	test("provider tokens with a fixed prefix", () => {
		expect(redactMemorySecrets("key AKIAIOSFODNN7EXAMPLE end")).toBe("key [REDACTED] end");
		expect(redactMemorySecrets(`token ghp_${"a1".repeat(12)} here`)).toBe("token [REDACTED] here");
		expect(redactMemorySecrets(`npm_${"Ab3".repeat(11)}`)).toBe("[REDACTED]");
	});

	test("a keyword followed by a credential-looking run", () => {
		expect(redactMemorySecrets("use secret_aB3dEfGh1JkLmN for it")).toBe("use [REDACTED] for it");
		expect(redactMemorySecrets("password-supersecretvalue")).toBe("[REDACTED]");
	});

	test("a JWT-shaped token", () => {
		const part = "abcdEFGH12345678";
		expect(redactMemorySecrets(`Bearer ${part}.${part}.${part} x`)).toBe("Bearer [REDACTED] x");
	});

	test("ordinary prose and code are left alone", () => {
		const text = "The authentication configuration lives in config.ts; see section 3.2 for the key rotation notes.";
		expect(redactMemorySecrets(text)).toBe(text);
	});
});
