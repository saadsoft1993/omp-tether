import { afterEach, describe, expect, it, vi } from "bun:test";
import { parseHTML } from "linkedom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MachineBanner } from "../src/app/MachineBanner";
import { machineHue, machineView } from "../src/app/machine";

const originalGlobals = new Map<string, PropertyDescriptor | undefined>();
let root: Root | null = null;

function installGlobal(name: string, value: unknown): void {
	originalGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
	Object.defineProperty(globalThis, name, { configurable: true, value, writable: true });
}

function mount(): { root: Root; container: Element } {
	const domWindow = parseHTML('<html><body><div id="root"></div></body></html>').window;
	installGlobal("window", domWindow);
	installGlobal("document", domWindow.document);
	installGlobal("navigator", domWindow.navigator);
	installGlobal("Node", domWindow.Node);
	installGlobal("Element", domWindow.Element);
	installGlobal("HTMLElement", domWindow.HTMLElement);
	installGlobal("IS_REACT_ACT_ENVIRONMENT", true);
	const container = domWindow.document.getElementById("root") as unknown as Element;
	root = createRoot(container);
	return { root, container };
}

afterEach(async () => {
	const active = root;
	if (active) {
		await act(async () => {
			active.unmount();
		});
		root = null;
	}
	vi.restoreAllMocks();
	for (const [name, descriptor] of originalGlobals) {
		if (descriptor) Object.defineProperty(globalThis, name, descriptor);
		else Reflect.deleteProperty(globalThis, name);
	}
	originalGlobals.clear();
});

describe("machine view", () => {
	it("gives each machine its own stable accent", () => {
		expect(machineHue("Work Laptop")).toBe(machineHue("  work laptop "));
		expect(machineHue("Work Laptop")).not.toBe(machineHue("Build Server"));
		expect(machineView("Work Laptop")?.accent).not.toBe(machineView("Build Server")?.accent);
	});

	it("spells out the scope with the noun the hub gives, and tidies the name", () => {
		const view = machineView("  Build \n Server  one ", "machine");
		expect(view?.name).toBe("Build Server one");
		expect(view?.label).toBe("Build Server one: sessions on this machine only");
		expect(machineView("Laptop", "Mac")?.scope).toBe("sessions on this Mac only");
	});

	it("has nothing to show without a name", () => {
		for (const missing of [undefined, null, "", "   "]) expect(machineView(missing)).toBeNull();
	});
});

describe("MachineBanner", () => {
	function stubFetch(respond: () => Response): void {
		installGlobal("fetch", vi.fn(async () => respond()));
	}

	// The query cache is module-global: the failing answer goes first so the cached name cannot leak into it.
	it("renders nothing when the hub has no machine endpoint or no name", async () => {
		stubFetch(() => Response.json({ error: "not found" }, { status: 404 }));
		const { root: view, container } = mount();
		await act(async () => view.render(<MachineBanner />));
		await act(async () => {
			await Bun.sleep(10);
		});
		expect(container.querySelector(".machine-banner") ?? null).toBeNull();
	});

	it("shows the machine name and noun the hub reports", async () => {
		stubFetch(() => Response.json({ name: "Work Laptop", noun: "Mac" }));
		const { root: view, container } = mount();
		await act(async () => view.render(<MachineBanner />));
		await act(async () => {
			await Bun.sleep(10);
		});
		const chip = container.querySelector(".machine-banner");
		expect(chip?.textContent).toContain("Work Laptop");
		expect(chip?.textContent).toContain("sessions on this Mac only");
		expect(chip?.getAttribute("aria-label")).toBe("Work Laptop: sessions on this Mac only");
	});
});
