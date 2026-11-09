// Narrowing helpers for parsed JSON in tests: fail with a clear message instead of trusting a cast.
export type Obj = { [key: string]: unknown };

export function obj(value: unknown): Obj {
	if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`expected an object, got ${JSON.stringify(value)}`);
	return value as Obj;
}

export function list(value: unknown): Obj[] {
	if (!Array.isArray(value)) throw new Error(`expected an array, got ${JSON.stringify(value)}`);
	return value.map(obj);
}

export function strings(value: unknown): string[] {
	if (!Array.isArray(value) || value.some(v => typeof v !== "string")) throw new Error(`expected an array of strings, got ${JSON.stringify(value)}`);
	return value as string[];
}
