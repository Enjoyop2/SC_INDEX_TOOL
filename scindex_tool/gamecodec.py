"""Container <-> game-style CSV text + sidecar.  See gamecsv.py for the layout rules."""
import csv
import hashlib
import io
import json

from . import gamecsv as G
from .builder import ORDERS, build_fb, orig_marks
from .container import GID_FLAG, Container
from .infer import _schema_json, _schema_load, infer_schema, row_tree

FORMAT = "scindex-meta/2"

def _rows_hash(rows):
	return hashlib.md5(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

def _text_rows(rows):
	return [[t for t, _ in r] for r in rows]

def _gid_of(c, r):
	ent = c.entries[r["idx"]] if r["idx"] < len(c.entries) else None
	return ent[0] if ent and (ent[1], ent[2], ent[3]) == (0, 0, GID_FLAG) else None

def _key(plan, name, gid):
	has_gid = any(c["src"][0] == "gid" for c in plan["cols"])
	return str(gid) if (has_gid and gid) else "n:" + name

def _merge_hidden(node, info, sub):
	node = dict(node)
	node.update(info.get("hid", {}))
	if sub and info.get("hids"):
		lst = node.get(str(sub["fid"]))
		if lst is not None and len(lst) == len(info["hids"]):
			node[str(sub["fid"])] = [dict(n, **h) for n, h in zip(lst, info["hids"])]
	return node

def to_game_csv(c, ref_csv=None, headers=None, filename=None, show_raw=False):
	"""Returns (csv_text, sidecar, n_raw_rows)."""
	schema = infer_schema([r["fb"] for r in c.rows])
	trees = [(row_tree(r["fb"], schema) if r["fb"] is not None else None) for r in c.rows]
	nstr = max([len(r["strings"]) for r in c.rows if r["strings"]] or [1])
	records = []
	for r, t in zip(c.rows, trees):
		ss = list(r["strings"] or [])
		ss += [""] * (nstr - len(ss))
		records.append({"name": ss[0], "strs": ss, "gid": _gid_of(c, r), "node": t or {}})
	plan = G.generic_plan(records, schema)
	ref = None
	if ref_csv:
		try:
			with open(ref_csv, "r", encoding="utf-8-sig", newline="") as fh:
				ref = G.parse_ref(list(csv.reader(fh)))
		except OSError:
			ref = None
	learned = ref is not None and ref["layout"] == plan["layout"]
	if learned:
		plan = G.learn(plan, records, schema, ref, show_raw)
	if not learned:
		for col in plan["cols"]:
			if col["src"][0] == "f":
				G.fill_blank(col, [G.raw_of(col, r) for r in records])
		if plan["sub"]:
			nodes = [n for r in records for n in (r["node"].get(str(plan["sub"]["fid"])) or [])]
			for col in plan["sub"]["cols"]:
				G.fill_blank(col, [G.raw_of(col, {"node": n, "strs": []}) for n in nodes])
	header = learned if headers is None else headers
	if plan["layout"] == "nested":
		header = False								  # nested sections carry their own header rows
	rows, spans = G.render(plan, records, header)
	trows = _text_rows(rows)
	try:
		recs2, spans2 = G.parse(plan, trows, header)
	except Exception:
		recs2 = []
	ok_layout = len(recs2) == len(records)

	def node_strs(rec2):
		return [rec2["name"]] + [rec2["strs"].get(i, "") for i in range(1, nstr)]

	order, best = "A", -1
	sample = [(rec2, r) for rec2, r in zip(recs2, c.rows) if r["fb"] is not None][:25] if ok_layout else []
	for od in ORDERS:
		n_ok = 0
		for rec2, r in sample:
			try:
				n_ok += build_fb(rec2["node"], schema, od, tmarks=orig_marks(r["fb"], schema)) == r["fb"]
			except Exception:
				pass
		if n_ok > best:
			order, best = od, n_ok
		if n_ok == len(sample):
			break

	side_rows, fallback = [], 0
	hid_cols = plan.get("hidden", [])
	sub = plan["sub"]
	for pos, r in enumerate(c.rows):
		rec = records[pos]
		info = {"key": _key(plan, rec["name"], rec["gid"]), "idx": r["idx"], "hash8": r["hash8"].hex()}
		if r["fb"] is None:
			info["rowraw"] = r["raw"].hex()
		else:
			exact = False
			node = recs2[pos]["node"] if ok_layout else None
			hid = {str(col["src"][1]): G.raw_of(col, rec) for col in hid_cols if G.raw_of(col, rec) is not None}
			if hid:
				info["hid"] = hid
			if sub and sub.get("hidden") and node is not None:
				subs = rec["node"].get(str(sub["fid"])) or []
				hs = [{str(c2["src"][1]): G.raw_of(c2, {"node": n, "strs": []}) for c2 in sub["hidden"]
					   if G.raw_of(c2, {"node": n, "strs": []}) is not None} for n in subs]
				if any(hs):
					info["hids"] = hs
			if node is not None:
				node = _merge_hidden(node, info, sub)
			if node is not None:
				try:
					exact = build_fb(node, schema, order) == r["fb"] and node_strs(recs2[pos]) == list(r["strings"])
				except Exception:
					pass
				if not exact:
					try:
						log = {}
						out = build_fb(node, schema, order, tmarks=orig_marks(r["fb"], schema), log=log)
						if out == r["fb"] and node_strs(recs2[pos]) == list(r["strings"]):
							exact = True
							if log:
								info["pads"] = ";".join("%d:%d" % kv for kv in sorted(log.items()))
					except Exception:
						pass
			if not exact:
				info["fbraw"] = r["fb"].hex()
				fallback += 1
		a, b = spans[pos]
		info["h"] = _rows_hash(trows[a:b])
		side_rows.append(info)

	text = G.rows_to_text(rows, True)
	sidecar = {"format": FORMAT, "file": filename, "table": c.table_name, "version": c.version,
			   "marker": c.marker, "hash16": c.hash16.hex(), "flags": c.flags.hex() if c.flags else "",
			   "flagnames": {str(k): v for k, v in c.flagnames.items()},
			   "entries": [list(e) for e in c.entries], "schema": _schema_json(schema),
			   "order": order, "plan": plan, "header": bool(header), "nstr": nstr, "rows": side_rows}
	return text, sidecar, fallback


def from_game_csv(text, sidecar):
	"""Game-style CSV text + sidecar -> (Container, warnings)."""
	schema, plan = _schema_load(sidecar["schema"]), sidecar["plan"]
	order, nstr, header = sidecar.get("order", "A"), sidecar["nstr"], sidecar["header"]
	c = Container()
	c.table_name, c.version, c.marker = sidecar["table"], sidecar["version"], sidecar["marker"]
	c.hash16 = bytes.fromhex(sidecar["hash16"])
	c.flags = bytes.fromhex(sidecar["flags"]) if sidecar.get("flags") else None
	c.flagnames = {int(k): v for k, v in sidecar.get("flagnames", {}).items()}
	c.entries = [tuple(e) for e in sidecar["entries"]]
	ent_index = {e[0]: i for i, e in reversed(list(enumerate(c.entries))) if (e[1], e[2], e[3]) == (0, 0, GID_FLAG)}
	saved = {}
	for info in sidecar["rows"]:
		saved.setdefault(info["key"], []).append(info)
	rows = [r for r in csv.reader(io.StringIO(text, newline="")) if r]
	recs, spans = G.parse(plan, rows, header)
	warnings = []
	next_gid = max(ent_index or {0}) + 1
	for pos, rec in enumerate(recs):
		key = _key(plan, rec["name"], rec["gid"])
		info = saved[key].pop(0) if saved.get(key) else None
		row = {}
		if info:
			row["idx"] = info["idx"]
		else:
			g = rec["gid"]
			if not g:
				g, next_gid = next_gid, next_gid + 1
			if g not in ent_index:
				c.entries.append((g, 0, 0, GID_FLAG))
				ent_index[g] = len(c.entries) - 1
				if c.flags is not None:
					c.flags += b"\0"
			row["idx"] = ent_index[g]
		a, b = spans[pos]
		same = bool(info) and info.get("h") == _rows_hash(rows[a:b])
		if info and info.get("rowraw"):
			row.update(strings=None, fb=None, raw=bytes.fromhex(info["rowraw"]))
		else:
			strs = [rec["name"]] + [rec["strs"].get(i, "") for i in range(1, nstr)]
			pads = None
			if same and info.get("pads"):
				pads = {int(x): int(y) for x, y in (p.split(":") for p in info["pads"].split(";") if p)}
			node = _merge_hidden(rec["node"], info or {}, plan.get("sub"))
			built = build_fb(node, schema, order, pads=pads)
			if same and info.get("fbraw"):
				built = bytes.fromhex(info["fbraw"])
			row.update(strings=strs, fb=built)
		if info:
			row["hash8"] = bytes.fromhex(info["hash8"])
		else:
			row["hash8"] = hashlib.blake2b(("%s\0%s" % (c.table_name, rec["name"])).encode("utf-8"),
					digest_size=8).digest()
			warnings.append("new row '%s': hash8 is generated (see README)" % rec["name"])
		c.rows.append(row)
	return c, warnings
