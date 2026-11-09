// A small JSON Schema checker for the subset the plugin's own schemas use: type, enum, const, properties, required,
// additionalProperties, items, minItems, maxItems, minimum, maximum, minLength, maxLength, pattern, local $ref, oneOf.
// It is not a general validator; `doctor` uses it to check the user's config file against the schema shipped in
// `schemas/`, and the tests keep the schema and the code in step.
export type Schema = Record<string, unknown>;

function typeOf(value: unknown): string {
	if (value === null) return "null";
	if (Array.isArray(value)) return "array";
	if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
	return typeof value;
}

function typeMatches(value: unknown, wanted: string): boolean {
	const actual = typeOf(value);
	return actual === wanted || (wanted === "number" && actual === "integer");
}

function resolveRef(ref: string, root: Schema): Schema {
	if (!ref.startsWith("#/")) throw new Error(`unsupported $ref ${ref}`);
	let node: unknown = root;
	for (const part of ref.slice(2).split("/")) {
		node = (node as Record<string, unknown> | undefined)?.[part.replaceAll("~1", "/").replaceAll("~0", "~")];
	}
	if (!node || typeof node !== "object") throw new Error(`unresolvable $ref ${ref}`);
	return node as Schema;
}

/** Returns one message per violation, each prefixed with the JSON pointer of the offending value. Empty means valid. */
export function validate(value: unknown, schema: Schema, root: Schema = schema, at = ""): string[] {
	const where = at || "/";
	if (typeof schema.$ref === "string") return validate(value, resolveRef(schema.$ref, root), root, at);
	const errors: string[] = [];
	if ("const" in schema && JSON.stringify(value) !== JSON.stringify(schema.const)) {
		errors.push(`${where}: must be ${JSON.stringify(schema.const)}`);
	}
	if (Array.isArray(schema.enum) && !schema.enum.some(e => JSON.stringify(e) === JSON.stringify(value))) {
		errors.push(`${where}: must be one of ${schema.enum.map(e => JSON.stringify(e)).join(", ")}`);
	}
	if (schema.type !== undefined) {
		const types = Array.isArray(schema.type) ? (schema.type as string[]) : [schema.type as string];
		if (!types.some(t => typeMatches(value, t))) {
			errors.push(`${where}: must be of type ${types.join(" or ")}, not ${typeOf(value)}`);
			return errors;
		}
	}
	if (Array.isArray(schema.oneOf)) {
		const matching = (schema.oneOf as Schema[]).filter(s => validate(value, s, root, at).length === 0).length;
		if (matching !== 1) errors.push(`${where}: must match exactly one alternative (matches ${matching})`);
	}
	if (typeof value === "number") {
		if (typeof schema.minimum === "number" && value < schema.minimum) errors.push(`${where}: must be at least ${schema.minimum}`);
		if (typeof schema.maximum === "number" && value > schema.maximum) errors.push(`${where}: must be at most ${schema.maximum}`);
	}
	if (typeof value === "string") {
		if (typeof schema.minLength === "number" && value.length < schema.minLength) errors.push(`${where}: is too short`);
		if (typeof schema.maxLength === "number" && value.length > schema.maxLength) errors.push(`${where}: is too long`);
		if (typeof schema.pattern === "string" && !new RegExp(schema.pattern, "u").test(value)) {
			errors.push(`${where}: does not match the required pattern`);
		}
	}
	if (Array.isArray(value)) {
		if (typeof schema.minItems === "number" && value.length < schema.minItems) errors.push(`${where}: needs at least ${schema.minItems} items`);
		if (typeof schema.maxItems === "number" && value.length > schema.maxItems) errors.push(`${where}: allows at most ${schema.maxItems} items`);
		if (schema.items && typeof schema.items === "object") {
			value.forEach((item, i) => errors.push(...validate(item, schema.items as Schema, root, `${at}/${i}`)));
		}
	}
	if (value && typeof value === "object" && !Array.isArray(value)) {
		const object = value as Record<string, unknown>;
		const properties = (schema.properties ?? {}) as Record<string, Schema>;
		for (const key of (schema.required ?? []) as string[]) {
			if (!(key in object)) errors.push(`${where}: missing required property "${key}"`);
		}
		for (const [key, child] of Object.entries(object)) {
			const pointer = `${at}/${key.replaceAll("~", "~0").replaceAll("/", "~1")}`;
			if (key in properties) errors.push(...validate(child, properties[key] as Schema, root, pointer));
			else if (schema.additionalProperties === false) errors.push(`${pointer}: unknown property`);
			else if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
				errors.push(...validate(child, schema.additionalProperties as Schema, root, pointer));
			}
		}
	}
	return errors;
}
