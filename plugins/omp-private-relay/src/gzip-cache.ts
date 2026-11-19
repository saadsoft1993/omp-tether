// Static build assets only. A new build discards all entries from the previous generation.
export const MAX_GZIP_ASSET_BYTES = 4 * 1024 * 1024;
export const MAX_GZIP_CACHE_BYTES = 16 * 1024 * 1024;

export class GzipCache {
	private generation = "";
	private entries = new Map<string, { key: string; bytes: Uint8Array }>();
	private size = 0;

	get byteLength(): number { return this.size; }
	get count(): number { return this.entries.size; }

	get(generation: string, file: string, key: string, load: () => Uint8Array | null): Uint8Array | null {
		if (this.generation !== generation) {
			this.entries.clear();
			this.size = 0;
			this.generation = generation;
		}
		const old = this.entries.get(file);
		if (old?.key === key) {
			this.entries.delete(file);
			this.entries.set(file, old);
			return old.bytes;
		}
		if (old) {
			this.entries.delete(file);
			this.size -= old.bytes.byteLength;
		}
		const bytes = load();
		if (bytes === null) return null;
		if (bytes.byteLength > MAX_GZIP_CACHE_BYTES) return bytes;
		while (this.size + bytes.byteLength > MAX_GZIP_CACHE_BYTES) {
			const first = this.entries.keys().next().value as string;
			const evicted = this.entries.get(first)!;
			this.entries.delete(first);
			this.size -= evicted.bytes.byteLength;
		}
		this.entries.set(file, { key, bytes });
		this.size += bytes.byteLength;
		return bytes;
	}
}
