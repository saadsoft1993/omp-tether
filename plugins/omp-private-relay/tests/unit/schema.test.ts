// The small JSON Schema checker the config file is validated with.
import { describe, expect, test } from "bun:test";
import { validate } from "../../src/schema";

const ok = (v: unknown, s: Record<string, unknown>) => expect(validate(v, s)).toEqual([]);
const bad = (v: unknown, s: Record<string, unknown>, text: RegExp) => expect(validate(v, s).join("\n")).toMatch(text);

describe("validate", () => {
	test("types, with integer a kind of number and null kept apart", () => {
		ok(1, { type: "integer" });
		ok(1, { type: "number" });
		bad(1.5, { type: "integer" }, /integer/);
		bad("1", { type: "integer" }, /not string/);
		ok(null, { type: ["string", "null"] });
		bad(null, { type: "string" }, /not null/);
		bad([], { type: "object" }, /not array/);
	});

	test("const, enum, bounds, length, pattern", () => {
		ok(1, { const: 1 });
		bad(2, { const: 1 }, /must be 1/);
		ok("a", { enum: ["a", "b"] });
		bad("c", { enum: ["a", "b"] }, /one of/);
		bad(0, { minimum: 1 }, /at least 1/);
		bad(11, { maximum: 10 }, /at most 10/);
		bad("", { minLength: 1 }, /too short/);
		bad("abc", { maxLength: 2 }, /too long/);
		bad("abc", { pattern: "^x" }, /pattern/);
	});

	test("objects: required, unknown properties, nested pointers", () => {
		const s = { type: "object", additionalProperties: false, required: ["a"], properties: { a: { type: "object", additionalProperties: false, properties: { b: { type: "integer" } } } } };
		ok({ a: { b: 1 } }, s);
		bad({}, s, /missing required property "a"/);
		bad({ a: {}, z: 1 }, s, /\/z: unknown property/);
		bad({ a: { b: "x" } }, s, /\/a\/b: must be of type integer/);
		bad({ a: { c: 1 } }, s, /\/a\/c: unknown property/);
	});

	test("arrays: items and counts", () => {
		const s = { type: "array", minItems: 1, maxItems: 2, items: { type: "string" } };
		ok(["a"], s);
		bad([], s, /at least 1/);
		bad(["a", "b", "c"], s, /at most 2/);
		bad(["a", 1], s, /\/1: must be of type string/);
	});

	test("$ref and oneOf", () => {
		const root = { $defs: { p: { type: "integer", minimum: 1 } }, oneOf: [{ $ref: "#/$defs/p" }, { type: "null" }] };
		ok(3, root);
		ok(null, root);
		bad(0, root, /exactly one/);
		bad("x", root, /exactly one/);
	});

	test("a $ref outside the document is refused, not followed", () => {
		expect(() => validate(1, { $ref: "https://example.invalid/x" })).toThrow(/unsupported/);
	});
});
