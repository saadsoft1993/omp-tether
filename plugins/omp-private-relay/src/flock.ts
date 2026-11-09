// Advisory file locking through libc's flock(2): one lock implementation for the pairing store and the web client installer.
// bun:ffi exists only on Bun, and a runtime without it must fail closed with a message, so it is loaded on first use.
import { createRequire } from "node:module";

export type Flock = (fd: number, operation: number) => number;
export const LOCK_EX = 2;
export const LOCK_NB = 4;

let flockFn: Flock | null | undefined;

/** flock(2), or null when this runtime cannot call it (the callers then refuse to proceed). */
export function getFlock(): Flock | null {
	if (flockFn !== undefined) return flockFn;
	flockFn = null;
	try {
		const { dlopen, FFIType } = createRequire(import.meta.url)("bun:ffi") as typeof import("bun:ffi");
		const libs = process.platform === "darwin" ? ["libSystem.B.dylib"] : ["libc.so.6", "libc.so"];
		for (const lib of libs) {
			try {
				const { symbols } = dlopen(lib, { flock: { args: [FFIType.i32, FFIType.i32], returns: FFIType.i32 } });
				flockFn = symbols.flock as Flock;
				break;
			} catch {
				// try the next library name
			}
		}
	} catch {
		// no bun:ffi
	}
	return flockFn;
}
