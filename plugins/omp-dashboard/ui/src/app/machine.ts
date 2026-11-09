/** Which machine this dashboard belongs to: the name the hub reports and the accent colour derived from it. */

/** Hue buckets: 12 steps of 30° keep two machines visibly apart instead of two near-identical hues. */
const HUE_STEP = 30;

/** Stable hue (FNV-1a over the lower-cased name) so a machine keeps its colour across reloads and devices. */
export function machineHue(name: string): number {
	let hash = 0x811c9dc5;
	for (const ch of name.trim().toLowerCase()) {
		hash ^= ch.codePointAt(0) ?? 0;
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return (hash % (360 / HUE_STEP)) * HUE_STEP;
}

export interface MachineView {
	name: string;
	/** Mid-lightness accent: at least 3:1 against both the dark (#000) and light (#fff) page for strokes and dots. */
	accent: string;
	scope: string;
	/** Accessible name, spelling out what the chip means. */
	label: string;
}

/**
 * Banner content for the hub's answer, or null when there is no usable name (older hub, hub down). `noun` is what the
 * hub calls this machine ("Mac" on macOS, "machine" elsewhere).
 */
export function machineView(name: string | null | undefined, noun = "machine"): MachineView | null {
	const trimmed = typeof name === "string" ? name.replace(/\s+/g, " ").trim() : "";
	if (!trimmed) return null;
	const word = noun.replace(/\s+/g, " ").trim() || "machine";
	const scope = `sessions on this ${word} only`;
	return {
		name: trimmed,
		accent: `oklch(0.62 0.17 ${machineHue(trimmed)})`,
		scope,
		label: `${trimmed}: ${scope}`,
	};
}
