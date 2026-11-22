"""Container <-> clean CSV text + sidecar metadata.

The CSV contains only data columns: Name, Str1.., GlobalID and one column per field.
Everything needed for a byte-exact rebuild (hashes, entry table, schema, raw fallbacks)
lives in the sidecar dict, never in the CSV.

Cell rules
  empty cell		  -> field absent
  number			  -> integer / float field
  TRUE / FALSE		-> boolean (1-byte field that only holds 0/1)
  plain text		  -> string field
  text starting '@'   -> JSON (vector, nested table, empty string, string that starts with '@')
  {"$id":n,"v":..} / {"$ref":n} -> one object referenced from several places (sharing)
"""
import csv
import hashlib
import io
import json
import struct

from .builder import ORDERS, build_fb, orig_marks
from .container import GID_FLAG, Container
from .infer import _SCALAR, _schema_json, _schema_load, f32str, infer_schema, row_tree

FORMAT = "scindex-meta/1"


# ---------------------------------------------------------------- cell <-> value
def cell_text(v, kind):
	"""Python value -> (cell text, must_quote)."""
	if v is None:
		return "", False
	if kind == "b":
		return ("TRUE" if v else "FALSE"), False
	if kind in _SCALAR:
		if kind == "f4":
			return f32str(struct.unpack("<I", struct.pack("<f", v))[0]), False
		return str(v), False
	if isinstance(v, str) and v != "" and not v.startswith("@") and "\0" not in v:
		return v, True
	return "@" + json.dumps(v, ensure_ascii=False, separators=(",", ":")), True


def parse_cell(s, kind):
	"""Cell text -> Python value (None when empty)."""
	if s == "":
		return None
	if kind == "b" or kind in _SCALAR:
		u = s.strip().upper()
		if u in ("TRUE", "FALSE"):
			return 1 if u == "TRUE" else 0
		return float(s) if kind == "f4" else int(s)
	if s.startswith("@"):
		return json.loads(s[1:])
	return s


def csv_line(cells, quoted):
	out = []
	for c, q in zip(cells, quoted):
		if c == "":
			out.append("")
		elif q:
			out.append('"' + c.replace('"', '""') + '"')
		else:
			out.append(c)
	return ",".join(out)


def is_field_col(cn):
	return cn[0] == "f" and cn[1:].isdigit()


# ---------------------------------------------------------------- column names from a reference CSV
def match_names(cols, data, ref_csv):
	"""Name columns by comparing VALUES with a game CSV found next to the scindex (rows aligned by GlobalID)."""
	try:
		with open(ref_csv, "r", encoding="utf-8-sig", newline="") as fh:
			rr = [r for r in csv.reader(fh) if r]
	except OSError:
		return None
	hi = next((i for i, r in enumerate(rr) if "GlobalID" in r), None)
	if hi is None:
		return None
	hdr, gi, mine = rr[hi], rr[hi].index("GlobalID"), cols.index("GlobalID")
	ref = {}
	for r in rr[hi + 1:]:
		if len(r) > gi and r[gi].lstrip("-").isdigit():
			ref.setdefault(r[gi], r)
	own = {cells[mine]: cells for cells, _ in data if cells[mine] in ref}
	if not own:
		return None
	score = []
	for cn in cols:
		if not (cn == "Name" or is_field_col(cn) or (cn.startswith("Str") and cn[3:].isdigit())):
			continue
		ci = cols.index(cn)
		for j, h in enumerate(hdr):
			if j == gi or not h:
				continue
			hit = miss = 0
			for g, cells in own.items():
				x = cells[ci]
				y = ref[g][j] if j < len(ref[g]) else ""
				if x == "" and y == "":
					continue
				hit, miss = (hit + 1, miss) if x == y else (hit, miss + 1)
			if hit and miss * 10 <= hit:
				score.append((hit - miss, cn, h, j))
	out, used_c, used_j = {"GlobalID": "GlobalID"}, set(), set()
	for _, cn, h, j in sorted(score, key=lambda t: -t[0]):
		if cn not in used_c and j not in used_j:
			out[cn] = h
			used_c.add(cn)
			used_j.add(j)
	return out


# -- scindex -> CSV
def container_to_csv(c, ref_csv=None, headers=None, filename=None):
	"""Returns (csv_text, sidecar, n_raw_fallback_rows)."""
	schema = infer_schema([r["fb"] for r in c.rows])
	top = sorted(schema.get("R", {}).items())
	nstr = max([len(r["strings"]) for r in c.rows if r["strings"]] or [1])
	cols = ["Name"] + ["Str%d" % i for i in range(1, nstr)] + ["GlobalID"] + ["f%d" % f for f, _ in top]
	trees = [(row_tree(r["fb"], schema) if r["fb"] is not None else None) for r in c.rows]
	bools = [f for f, k in top if k == "i1" and
			 [t.get(str(f)) for t in trees if t and t.get(str(f)) is not None] and
			 all(t.get(str(f)) in (0, 1) for t in trees if t and t.get(str(f)) is not None)]

	# pick the object-creation order that reproduces most of a sample exactly
	order, best = "A", -1
	sample = [(r, t) for r, t in zip(c.rows, trees) if t is not None and r["fb"] is not None][:25]
	for od in ORDERS:
		ok = 0
		for r, t in sample:
			try:
				ok += build_fb(t, schema, od, tmarks=orig_marks(r["fb"], schema)) == r["fb"]
			except Exception:
				pass
		if ok > best:
			order, best = od, ok
		if ok == len(sample):
			break

	data, side_rows, fallback = [], [], 0
	for pos, (r, tree) in enumerate(zip(c.rows, trees)):
		ss = r["strings"] or []
		cells = [ss[i] if i < len(ss) else "" for i in range(nstr)]
		quoted = [True] * nstr
		ent = c.entries[r["idx"]] if r["idx"] < len(c.entries) else None
		gid = str(ent[0]) if ent and (ent[1], ent[2], ent[3]) == (0, 0, GID_FLAG) else ""
		cells.append(gid)
		quoted.append(False)
		info = {"key": gid or "#%d" % pos, "idx": r["idx"], "hash8": r["hash8"].hex()}
		if r["fb"] is None:							  # payload could not be parsed: keep the row raw
			info["rowraw"] = r["raw"].hex()
			tree = {}
		else:
			exact = False
			if tree is not None:
				try:
					exact = build_fb(tree, schema, order) == r["fb"]
				except Exception:
					pass
			tree = tree or {}
			if not exact and tree:					   # look for the original's extra zero padding
				try:
					log = {}
					out = build_fb(tree, schema, order, tmarks=orig_marks(r["fb"], schema), log=log)
					if out == r["fb"]:
						exact = True
						if log:
							info["pads"] = ";".join("%d:%d" % kv for kv in sorted(log.items()))
				except Exception:
					pass
			if not exact:
				info["fbraw"] = r["fb"].hex()
				fallback += 1
		for f, k in top:
			s, q = cell_text(tree.get(str(f)), "b" if f in bools else k)
			cells.append(s)
			quoted.append(q)
		data.append((cells, quoted))
		side_rows.append(info)

	names = match_names(cols, data, ref_csv) if ref_csv else None
	use_header = (names is not None) if headers is None else headers
	lines = []
	if use_header:
		kinds = {"f%d" % f: ("boolean" if f in bools else k) for f, k in top}

		def type_of(cn):
			k = kinds.get(cn)
			if cn == "GlobalID" or k in ("i1", "i2", "i4", "i8"):
				return "int"
			return {"boolean": "boolean", "f4": "float"}.get(k, "String")
		head = [(names or {}).get(cn, cn) for cn in cols]
		lines.append(csv_line(head, [True] * len(head)))
		lines.append(csv_line([type_of(cn) for cn in cols], [True] * len(cols)))
	lines += [csv_line(cells, q) for cells, q in data]

	sidecar = {"format": FORMAT, "file": filename, "table": c.table_name, "version": c.version,
			   "marker": c.marker, "hash16": c.hash16.hex(), "flags": c.flags.hex() if c.flags else "",
			   "flagnames": {str(k): v for k, v in c.flagnames.items()},
			   "entries": [list(e) for e in c.entries], "schema": _schema_json(schema),
			   "order": order, "bools": bools, "cols": cols, "header": bool(use_header),
			   "rows": side_rows}
	return "\n".join(lines) + "\n", sidecar, fallback


# -- CSV -> scindex
def csv_to_container(text, sidecar):
	"""Clean CSV text + sidecar -> (Container, warnings). Edits, new and deleted rows are supported."""
	if sidecar.get("format") != FORMAT:
		raise ValueError("unknown sidecar format")
	cols, schema = sidecar["cols"], _schema_load(sidecar["schema"])
	order = sidecar.get("order", "A")
	c = Container()
	c.table_name, c.version, c.marker = sidecar["table"], sidecar["version"], sidecar["marker"]
	c.hash16 = bytes.fromhex(sidecar["hash16"])
	c.flags = bytes.fromhex(sidecar["flags"]) if sidecar.get("flags") else None
	c.flagnames = {int(k): v for k, v in sidecar.get("flagnames", {}).items()}
	c.entries = [tuple(e) for e in sidecar["entries"]]
	ent_index = {}
	for i, e in enumerate(c.entries):
		if (e[1], e[2], e[3]) == (0, 0, GID_FLAG):
			ent_index.setdefault(e[0], i)
	saved = {}
	for info in sidecar["rows"]:
		saved.setdefault(info["key"], []).append(info)

	rows = [r for r in csv.reader(io.StringIO(text, newline="")) if r]
	if sidecar.get("header"):
		rows = rows[2:]
	ci = {cn: i for i, cn in enumerate(cols)}
	strcols = [ci["Name"]] + [ci[cn] for cn in cols if cn.startswith("Str") and cn[3:].isdigit()]
	fcols = sorted((int(cn[1:]), i) for cn, i in ci.items() if is_field_col(cn))
	warnings = []
	for pos, r in enumerate(rows):
		def get(i):
			return r[i] if i < len(r) else ""
		gid = get(ci["GlobalID"])
		key = gid or "#%d" % pos
		info = saved[key].pop(0) if saved.get(key) else None
		row = {}
		if info:
			row["idx"] = info["idx"]
		elif gid:
			g = int(gid)
			if g not in ent_index:					   # new GlobalID -> new entry
				c.entries.append((g, 0, 0, GID_FLAG))
				ent_index[g] = len(c.entries) - 1
				if c.flags is not None:
					c.flags += b"\0"
			row["idx"] = ent_index[g]
		else:
			raise ValueError("row %d has no GlobalID and is not in the sidecar" % (pos + 1))
		if info and info.get("rowraw"):
			row.update(strings=None, fb=None, raw=bytes.fromhex(info["rowraw"]))
		else:
			node = {}
			for f, i in fcols:
				v = parse_cell(get(i), schema["R"].get(f, "s"))
				if v is not None:
					node[str(f)] = v
			pads = None
			if info and info.get("pads"):
				pads = {int(a): int(b) for a, b in (x.split(":") for x in info["pads"].split(";") if x)}
			built = build_fb(node, schema, order, pads=pads)
			if info and info.get("fbraw"):			   # untouched rows keep their original bytes
				raw = bytes.fromhex(info["fbraw"])
				if row_tree(raw, schema) == node:
					built = raw
			row.update(strings=[get(i) for i in strcols], fb=built)
		if info:
			row["hash8"] = bytes.fromhex(info["hash8"])
		else:
			name = row["strings"][0] if row.get("strings") else ""
			row["hash8"] = hashlib.blake2b(("%s\0%s" % (c.table_name, name)).encode("utf-8"),
										   digest_size=8).digest()
			warnings.append("new row '%s': hash8 is generated (see README)" % name)
		c.rows.append(row)
	return c, warnings
