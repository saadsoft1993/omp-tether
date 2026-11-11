/**
 * Embedding worker: a child process that runs the Mnemopi library's own `embed()` (fastembed + onnxruntime) so that
 * the importer process never loads onnxruntime (it is heavy: the importer's own memory stays small, and the
 * runtime's teardown cannot abort the importer). omp does the same with its embed worker.
 *
 * Protocol, one JSON object per line. In: `{"id": n, "texts": [...]}`. Out: `{"id": n, "vectors": [[...]]}` or
 * `{"id": n, "error": "..."}`. Texts are embedded in chunks to bound memory.
 */
import path from "node:path";

const pkgDir = process.env.MEMORY_IMPORT_LIB_PKG ?? "";
const model = process.env.MEMORY_IMPORT_EMBED_MODEL ?? "";
const CHUNK = 16;

// Dynamic: the library package is chosen at run time by the parent and passed in the environment.
const embeddings = await import(path.join(pkgDir, "src", "core", "embeddings.ts"));
const runtime = await import(path.join(pkgDir, "src", "core", "runtime-options.ts"));
const options = { embeddings: { model } };

async function embedAll(texts: string[]): Promise<number[][]> {
	const out: number[][] = [];
	for (let start = 0; start < texts.length; start += CHUNK) {
		const chunk = texts.slice(start, start + CHUNK);
		const matrix: ArrayLike<ArrayLike<number>> | null = await runtime.withMnemopiRuntimeOptions(options, () => embeddings.embed(chunk));
		if (matrix === null || matrix.length !== chunk.length) throw new Error(`embedding ${model} returned no vectors`);
		for (const row of Array.from(matrix)) out.push(Array.from(row));
	}
	return out;
}

for await (const line of console) {
	if (line.trim() === "") continue;
	const request: { id: number; texts: string[] } = JSON.parse(line);
	try {
		const vectors = await embedAll(request.texts);
		process.stdout.write(`${JSON.stringify({ id: request.id, vectors })}\n`);
	} catch (error) {
		process.stdout.write(`${JSON.stringify({ id: request.id, error: String(error) })}\n`);
	}
}
