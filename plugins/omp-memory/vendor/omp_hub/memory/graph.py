"""Memory map assembly shared by the adapters: pick the nodes worth showing and the edges between them.

The adapters hand over every candidate node and edge in item-id terms; `build` returns the API answer of
GET /api/hub/memory/map. Nothing here does I/O.

	node in:  {"id", "label"|"text", "kind", "scope", "state", "recallCount", "lastRecalledAt", "createdAt"}
	edge in:  {"source", "target", "kind": "entity"|"link"|"fact", "weight", "label"}

When more nodes exist than `limit`, the most useful ones are kept: highest recall count first, then highest
degree (edges to other candidates), then newest. Only edges between kept nodes are returned.
"""

from .model import utc_now

MAP_DEFAULT = 250
MAP_MAX = 400
LABEL_CHARS = 80
EDGE_KINDS = ("entity", "link", "fact")


def label_of(text):
	"""One line of at most LABEL_CHARS characters from a memory's text."""
	flat = " ".join((text or "").split())
	return flat if len(flat) <= LABEL_CHARS else flat[: LABEL_CHARS - 1] + "\u2026"


def clamp_limit(limit):
	"""The node limit: MAP_DEFAULT when absent, else 1..MAP_MAX."""
	return MAP_DEFAULT if limit is None else max(1, min(int(limit), MAP_MAX))


def _number(value, default=0.0):
	return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default


def build(nodes, edges, limit, source, total=None):
	"""{"nodes", "edges", "stats", "source", "generatedAt"}; `total` = nodes the bank has when more than given."""
	limit = clamp_limit(limit)
	by_id = {n["id"]: n for n in nodes}
	seen, clean = set(), []
	for edge in edges:
		a, b, kind = edge["source"], edge["target"], edge["kind"]
		if a == b or a not in by_id or b not in by_id or kind not in EDGE_KINDS:
			continue
		label = edge.get("label")
		key = (min(a, b), max(a, b), kind, label)
		if key in seen:
			continue
		seen.add(key)
		clean.append(
			{
				"source": a,
				"target": b,
				"kind": kind,
				"weight": _number(edge.get("weight"), 1.0),
				"label": label,
			}
		)
	degree = dict.fromkeys(by_id, 0)
	for edge in clean:
		degree[edge["source"]] += 1
		degree[edge["target"]] += 1
	ranked = sorted(
		nodes, key=lambda n: n.get("createdAt") or "", reverse=True
	)  # stable: recency breaks ties
	ranked.sort(key=lambda n: degree[n["id"]], reverse=True)
	ranked.sort(key=lambda n: n.get("recallCount") or 0, reverse=True)
	kept = ranked[:limit]
	kept_ids = {n["id"] for n in kept}
	out_edges = [e for e in clean if e["source"] in kept_ids and e["target"] in kept_ids]
	shown_degree = dict.fromkeys(kept_ids, 0)
	for edge in out_edges:
		shown_degree[edge["source"]] += 1
		shown_degree[edge["target"]] += 1
	out_nodes = [
		{
			"id": n["id"],
			"label": label_of(n.get("label") or n.get("text")),
			"kind": n.get("kind") or "memory",
			"scope": n.get("scope"),
			"state": n.get("state") or "active",
			"recallCount": n.get("recallCount"),
			"lastRecalledAt": n.get("lastRecalledAt"),
			"degree": shown_degree[n["id"]],
		}
		for n in kept
	]
	everything = max(len(nodes), int(total)) if isinstance(total, int) else len(nodes)
	return {
		"nodes": out_nodes,
		"edges": out_edges,
		"stats": {"total": everything, "shown": len(out_nodes), "truncated": len(out_nodes) < everything},
		"source": source,
		"generatedAt": utc_now(),
	}
