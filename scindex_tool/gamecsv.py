"""Game-style CSV layout (the format of the game's own CSV files), derived without any hard-coded schema.

Layout rules (observed in the game CSVs)
  * one record = one group of rows; the first row carries Name / GlobalID / scalar fields
  * vector fields are spread over the group's rows (continuation rows have an empty Name)
  * a field holding a vector of tables becomes a nested section: sub-header row, type row, sub-rows
    (the sub-header lists only the columns that have data in that group)
  * booleans are TRUE / FALSE, an empty cell means "absent"

Column names, order and value conversions are LEARNED from a reference CSV when one is present
(by comparing values, see `learn`). Learned conversions:
    add k     stored value + k        (e.g. levels stored 0-based)
    enum      number <-> text         (e.g. 0 <-> "Origin")
    rowref    number <-> row name     (a reference to another row, -1 = none)
    mask      integer bit mask <-> one boolean per row
Without a reference CSV generic names (f0, f1, ...) are used and values stay raw.
"""
import json
import struct
from collections import Counter

from .infer import f32str


# ------------------------------------------------------------------ small helpers
def _f32(v):
	return f32str(struct.unpack("<I", struct.pack("<f", v))[0])


def new_col(h, src, ty, vec=False, xf=None, blank="absent", js=False):
	return {"h": h, "src": src, "ty": ty, "vec": vec, "xf": xf, "blank": blank, "js": js}


def trim(lst):
	lst = list(lst)
	while lst and lst[-1] == "":
		lst.pop()
	return lst


def is_scalar_kind(k):
	return k in ("i1", "i2", "i4", "i8", "f4")


# ------------------------------------------------------------------ value <-> cell text
def cell_of(col, v, quote=True):
	"""typed value -> (text, quoted)."""
	if v is None:
		return "", False
	if col["js"]:
		return "@" + json.dumps(v, ensure_ascii=False, separators=(",", ":")), True
	ty = col["ty"]
	if ty == "boolean":
		return ("TRUE" if v else "FALSE"), True
	if ty == "float":
		return (_f32(v) if isinstance(v, float) else str(v)), False
	if ty == "int":
		return str(v), isinstance(v, str)
	if v == "":
		return "", False
	return str(v), True


def value_of(col, s):
	"""cell text -> typed value (None if blank)."""
	if s == "":
		return None
	if col["js"]:
		return json.loads(s[1:]) if s.startswith("@") else s
	ty = col["ty"]
	if ty == "boolean":
		return 1 if s.strip().upper() == "TRUE" else 0
	if ty == "float":
		return float(s)
	if ty == "int":
		try:
			return int(s)
		except ValueError:
			return s									 # enum / rowref text
	return s


def to_view(col, raw, names):
	"""stored value -> shown value."""
	xf = col["xf"]
	if not xf:
		return raw
	if "add" in xf:
		return (raw or 0) + xf["add"]
	if raw is None:
		return (xf["enum"].get("") or None) if "enum" in xf else raw
	if "enum" in xf:
		return xf["enum"].get(str(raw), raw)
	if "rowref" in xf:
		if raw == -1:
			return None
		return names[raw] if 0 <= raw < len(names) else raw
	return raw


def from_view(col, v, names):
	xf = col["xf"]
	if v is None or not xf:
		return v
	if "add" in xf:
		return (v - xf["add"]) or None				  # 0 is the FlatBuffers default: not stored
	if "enum" in xf:
		inv = {t: int(k) for k, t in xf["enum"].items() if k != ""}
		if v == xf["enum"].get(""):					 # the text of the default (absent) value
			return None
		return inv.get(v, v)
	if "rowref" in xf:
		return v if isinstance(v, int) else (names.index(v) if v in names else -1)
	return v


# ------------------------------------------------------------------ record access
def raw_of(col, rec):
	"""stored value of a column in a record: scalar, list (vector) or None."""
	s = col["src"]
	if s[0] == "x":
		return None
	if s[0] == "name":
		return rec.get("name")
	if s[0] == "gid":
		return rec.get("gid")
	if s[0] == "s":
		v = rec["strs"][s[1]] if s[1] < len(rec["strs"]) else ""
		return v if v != "" else None
	return rec["node"].get(str(s[1]))


def is_empty_group(cols, rec):
	"""True when a group has no vector content at all (it is shown as a single, mostly blank row)."""
	for c in cols:
		if c["vec"] and not (c["xf"] and "mask" in c["xf"]):
			raw = raw_of(c, rec)
			if isinstance(raw, list) and raw and not c["js"]:
				return False
	return True


def view_list(col, rec, names, n, empty=False):
	"""values shown in the CSV for this column: list (1 entry for scalars, n for masks, len for vectors)."""
	raw = raw_of(col, rec)
	if col["xf"] and "mask" in col["xf"]:
		if raw is None:
			d = col["xf"].get("dflt")
			return [bool(d == 1 or (d == 2 and empty))] * n
		return [bool(raw >> i & 1) for i in range(n)]
	if col["vec"]:
		if raw is None:
			return None
		if col["js"] or not isinstance(raw, list):
			return [raw]
		return [to_view(col, e, names) for e in raw]
	v = to_view(col, raw, names)
	return None if v is None else [v]


def rec_rows(cols, rec, names, first_cols_blank=0):
	"""Render one record (group) -> list of rows, each a list of (text, quoted) over `cols`."""
	lists = []
	# row count = longest vector
	nrows = 1
	empty = is_empty_group(cols, rec)
	for c in cols:
		if c["vec"] and not (c["xf"] and "mask" in c["xf"]):
			raw = raw_of(c, rec)
			if isinstance(raw, list) and not c["js"]:
				nrows = max(nrows, len(raw))
	for c in cols:
		lists.append(view_list(c, rec, names, nrows, empty))
	rows = []
	for i in range(nrows):
		row = []
		for c, lst in zip(cols, lists):
			masklike = c["xf"] and "mask" in c["xf"]
			if lst is None or (not c["vec"] and not masklike and i > 0):
				row.append(("", False))
			elif masklike or c["vec"]:
				row.append(cell_of(c, lst[i], True) if i < len(lst) else ("", False))
			else:
				row.append(cell_of(c, lst[0], True))
		rows.append(row)
	return rows


def rec_from_rows(cols, rows, names, blank_ok=True):
	"""Inverse of rec_rows: group rows (lists of text) -> (name?, node-like dict of col -> stored value)."""
	out = {}
	for ci, c in enumerate(cols):
		cells = [r[ci] if ci < len(r) else "" for r in rows]
		masklike = c["xf"] and "mask" in c["xf"]
		if masklike:
			bits = [value_of({**c, "ty": "boolean", "js": False}, s) for s in cells]
			v = sum((1 << i) for i, b in enumerate(bits) if b)
			full = v == (1 << len(bits)) - 1
			if c["xf"].get("dflt") == 1:				 # absent means "all TRUE"
				out[ci] = None if full else v
			elif c["xf"].get("dflt") == 2:			   # absent: FALSE, or TRUE in a group without content
				empty = all(not any(r[k] for r in rows if k < len(r))
							for k, cc in enumerate(cols) if cc["vec"] and not (cc["xf"] and "mask" in cc["xf"]))
				out[ci] = None if (full if empty else not v) else v
			else:
				out[ci] = v if v else (None if c["blank"] == "absent" else 0)
		elif c["vec"]:
			if c["js"]:
				out[ci] = value_of(c, cells[0])
				continue
			cells = trim(cells)
			if not cells:
				out[ci] = {"absent": None, "empty": [], "one": [""]}[c["blank"]]
			else:
				vals = []
				for s in cells:
					v = value_of(c, s)
					vals.append(from_view(c, v if v is not None else ("" if c["ty"] == "String" else 0), names))
				out[ci] = vals
		else:
			v = value_of(c, cells[0])
			if v is None:
				out[ci] = "" if (c["ty"] == "String" and c["blank"] == "empty") else None
			else:
				out[ci] = from_view(c, v, names)
	return out


# ------------------------------------------------------------------ plan: generic
def blank_policy(kind, outcomes):
	c = Counter(outcomes)
	return c.most_common(1)[0][0] if c else "absent"


def make_cols(schema, path, recs_nodes, skip=()):
	"""Generic columns for the fields at `path` (sorted by field id)."""
	cols = []
	sch = schema.get(path, {})
	for fid in sorted(sch):
		if fid in skip:
			continue
		k = sch[fid]
		vals = [n.get(str(fid)) for n in recs_nodes]
		present = [v for v in vals if v is not None]
		if not present:
			continue
		src = ["f", fid]
		if is_scalar_kind(k):
			if k == "i1" and all(v in (0, 1) for v in present):
				cols.append(new_col("f%d" % fid, src, "boolean"))
			else:
				cols.append(new_col("f%d" % fid, src, "float" if k == "f4" else "int"))
		elif k == "s":
			outs = ["empty" if v == "" else "x" for v in present]
			cols.append(new_col("f%d" % fid, src, "String", blank="empty" if outs.count("empty") * 2 > len(present) else "absent"))
		elif k in ("vs", "vi4"):
			simple = all(isinstance(e, (str, int)) and not isinstance(e, bool) for v in present if isinstance(v, list) for e in v) \
				and all(isinstance(v, list) for v in present)
			if not simple:
				cols.append(new_col("f%d" % fid, src, "String", js=True))
				continue
			outs = []
			for v in vals:
				if v is None:
					outs.append("absent")
				elif v == []:
					outs.append("empty")
				elif all(e in ("", 0) for e in v) and len(v) == 1:
					outs.append("one")
				else:
					outs.append(None)
			outs = [o for o in outs if o]
			cols.append(new_col("f%d" % fid, src, "String" if k == "vs" else "int", vec=True,
								blank=blank_policy(k, outs)))
		else:											# t / vt : kept as JSON in one cell
			cols.append(new_col("f%d" % fid, src, "String", vec=False, js=True))
	return cols


def generic_plan(records, schema):
	top = schema.get("R", {})
	nodes = [r["node"] for r in records]
	nstr = max([len(r["strs"]) for r in records] or [1])
	cols = [new_col("Name", ["name"], "String")]
	if any(r.get("gid") for r in records):
		cols.append(new_col("GlobalID", ["gid"], "int"))
	for i in range(1, nstr):
		if any(i < len(r["strs"]) and r["strs"][i] for r in records):
			cols.append(new_col("Str%d" % i, ["s", i], "String"))
	# nested layout: one table-vector field (+ optional scalar fields on the group row)
	used = [f for f in sorted(top) if any(n.get(str(f)) is not None for n in nodes)]
	vts = [f for f in used if top[f] == "vt"]
	plan = {"layout": "flat", "cols": cols, "sub": None}
	if len(vts) == 1 and not any(len(r["strs"]) > 1 and any(r["strs"][1:]) for r in records) \
			and all(isinstance(n.get(str(vts[0])), list) and all(isinstance(e, dict) and "$id" not in e and "$ref" not in e
																for e in n.get(str(vts[0])))
					for n in nodes if n.get(str(vts[0])) is not None):
		fid = vts[0]
		sub_nodes = [e for n in nodes if n.get(str(fid)) for e in n[str(fid)]]
		plan["layout"] = "nested"
		plan["cols"] = [c for c in cols if c["src"][0] in ("name", "gid")] + make_cols(schema, "R", nodes, skip=(fid,))
		plan["sub"] = {"fid": fid, "cols": make_cols(schema, "R.%d" % fid, sub_nodes),
					   "blank": "empty" if any(n.get(str(fid)) == [] for n in nodes) else "absent"}
		return plan
	plan["cols"] = cols + make_cols(schema, "R", nodes)
	return plan


# ------------------------------------------------------------------ whole-table rendering
TYPE_NAME = {"String": "String", "int": "int", "float": "float", "boolean": "boolean"}


def _pad(rows):
	w = max([len(r) for r in rows] or [0])
	return [r + [("", False)] * (w - len(r)) for r in rows]


def render(plan, records, header):
	"""records -> (rows of (text, quoted), spans[(start, end)] per record)."""
	names = [r["name"] for r in records]
	cols = plan["cols"]
	rows, spans = [], []
	if plan["layout"] == "flat":
		if header:
			rows.append([(c["h"], True) for c in cols])
			rows.append([(TYPE_NAME[c["ty"]], True) for c in cols])
		for rec in records:
			rr = rec_rows(cols, rec, names)
			spans.append((len(rows), len(rows) + len(rr)))
			rows += rr
		return _pad(rows), spans
	sub = plan["sub"]
	for rec in records:
		start = len(rows)
		top = [cell_of(c, raw_of(c, rec)) if raw_of(c, rec) not in (None, "") else ("", False) for c in cols]
		rows.append(top)
		lst = rec["node"].get(str(sub["fid"]))
		if lst:
			body = []
			for node in lst:
				body += rec_rows(sub["cols"], {"node": node, "strs": []}, [])
			present = [i for i in range(len(sub["cols"])) if any(r[i][0] != "" for r in body)]
			rows.append([("", False)] + [(sub["cols"][i]["h"], True) for i in present])
			rows.append([("", False)] + [(TYPE_NAME[sub["cols"][i]["ty"]], True) for i in present])
			for r in body:
				rows.append([("", False)] + [r[i] for i in present])
		spans.append((start, len(rows)))
	return _pad(rows), spans


def _group_starts(rows, idxs):
	st = []
	for i, r in enumerate(rows):
		if any(j < len(r) and r[j] != "" for j in idxs):
			st.append(i)
	return st


def parse(plan, rows, header):
	"""CSV rows (lists of text) -> (records, spans). Record: {name, gid, strs{i:text}, node}."""
	cols = plan["cols"]
	if plan["layout"] == "flat":
		if header:
			rows = rows[2:]
		base = 2 if header else 0
		idxs = [i for i, c in enumerate(cols) if c["src"][0] in ("name", "gid")]
		st = _group_starts(rows, idxs) or []
		recs, spans = [], []
		names = []
		for k, s in enumerate(st):
			e = st[k + 1] if k + 1 < len(st) else len(rows)
			names.append(rows[s][next(i for i, c in enumerate(cols) if c["src"][0] == "name")])
		for k, s in enumerate(st):
			e = st[k + 1] if k + 1 < len(st) else len(rows)
			out = rec_from_rows(cols, rows[s:e], names)
			recs.append(_build_rec(cols, out))
			spans.append((base + s, base + e))
		return recs, spans
	sub = plan["sub"]
	st = _group_starts(rows, [0])
	recs, spans = [], []
	for k, s in enumerate(st):
		e = st[k + 1] if k + 1 < len(st) else len(rows)
		rec = _build_rec(cols, rec_from_rows(cols, [rows[s]], []))
		body = rows[s + 1:e]
		if body:
			hdr = [h for h in body[0][1:]]
			hmap = {c["h"]: c for c in sub["cols"]}
			pcols = [hmap[h] for h in hdr if h in hmap]
			pidx = [i + 1 for i, h in enumerate(hdr) if h in hmap]
			data = [[r[j] if j < len(r) else "" for j in pidx] for r in body[2:]]
			keyc = 0 if pcols and pcols[0] is sub["cols"][0] else None
			groups = []
			for r in data:
				if keyc is None or r[keyc] != "" or not groups:
					groups.append([r])
				else:
					groups[-1].append(r)
			nodes = []
			for g in groups:
				out = rec_from_rows(pcols, g, [])
				nodes.append({str(c["src"][1]): out[i] for i, c in enumerate(pcols)
							  if c["src"][0] == "f" and out[i] is not None})
			rec["node"][str(sub["fid"])] = nodes
		else:
			rec["node"][str(sub["fid"])] = [] if sub["blank"] == "empty" else None
			if rec["node"][str(sub["fid"])] is None:
				del rec["node"][str(sub["fid"])]
		recs.append(rec)
		spans.append((s, e))
	return recs, spans


def _build_rec(cols, out):
	rec = {"name": "", "gid": None, "strs": {}, "node": {}}
	for i, c in enumerate(cols):
		v = out[i]
		s = c["src"]
		if s[0] == "name":
			rec["name"] = v or ""
		elif s[0] == "gid":
			rec["gid"] = v
		elif s[0] == "s":
			rec["strs"][s[1]] = v or ""
		elif s[0] == "f" and v is not None:
			rec["node"][str(s[1])] = v
	return rec


# ------------------------------------------------------------------ reference CSV
def parse_ref(rows):
	"""Reference CSV rows -> {'layout', 'hdr', 'types', 'groups'}."""
	rows = [r for r in rows if any(c != "" for c in r)]
	if rows and "Name" in rows[0]:
		hdr = rows[0]
		ty = rows[1] if len(rows) > 1 else []
		ni = hdr.index("Name")
		gi = hdr.index("GlobalID") if "GlobalID" in hdr else None
		groups = []
		for r in rows[2:]:
			if r[ni] != "" or (gi is not None and r[gi] != "") or not groups:
				groups.append([r])
			else:
				groups[-1].append(r)
		return {"layout": "flat", "hdr": hdr, "types": ty, "groups": groups}
	if len(rows) > 2 and rows[1][0] == "" and "Name" in rows[1][1:]:
		groups, cur = [], None
		for r in rows:
			if r[0] != "":
				cur = {"name": r[0], "rows": [], "gid": None}
				groups.append(cur)
			elif cur is not None:
				cur["rows"].append(r)
		for g in groups:
			r = g["rows"]
			g["hdr"] = trim(r[0][1:]) if r else []
			g["types"] = r[1][1:1 + len(g["hdr"])] if len(r) > 1 else []
			recs = []
			for row in r[2:]:
				cells = row[1:1 + len(g["hdr"])]
				if not recs or cells[0] != "":
					recs.append([cells])
				else:
					recs[-1].append(cells)
			g["subs"] = recs
		return {"layout": "nested", "groups": groups}
	return None


# ------------------------------------------------------------------ learning from the reference
RANK = {"id": 0, "bool": 1, "add": 2, "mask": 3, "rowref": 4, "enum": 5}


def ours_strs(src, kind, rec):
	"""stored value -> list of comparable strings."""
	raw = raw_of({"src": src, "xf": None}, rec)
	if raw is None:
		return []
	if isinstance(raw, list):
		return trim([_f32(e) if isinstance(e, float) else str(e) for e in raw])
	if isinstance(raw, float):
		return [_f32(raw)]
	return [str(raw)] if str(raw) != "" else []


def _ref_norm(lst):
	return trim([("" if x == "FALSE" else x) for x in lst])


def try_transforms(src, kind, pairs, names):
	"""pairs: [(ours list[str], ref list[str])] -> best (score, matches, rank, xf, is_boolean) or None."""
	total = sum(1 for o, r in pairs if o or r)
	if total == 0 or not any(r for _, r in pairs):
		return None
	found = []

	def score(f):
		return sum(1 for o, r in pairs if (o or r) and f(o, r))
	m = score(lambda o, r: o == r)
	if m:
		found.append((m / total, m, "id", None, False))
	if kind == "i1":
		m = score(lambda o, r: _ref_norm(["TRUE" if x != "0" else "FALSE" for x in o]) == _ref_norm(r))
		if m:
			found.append((m / total, m, "bool", None, True))
	if kind in ("i1", "i2", "i4", "i8"):
		ks = [int(r[0]) - int(o[0]) for o, r in pairs
			  if o and r and o[0].lstrip("-").isdigit() and r[0].lstrip("-").isdigit()]
		if ks:
			k = Counter(ks).most_common(1)[0][0]
			if k != 0:
				m = score(lambda o, r: len(o) == len(r) and all(
					a.lstrip("-").isdigit() and b.lstrip("-").isdigit() and int(a) + k == int(b) for a, b in zip(o, r)))
				if m:
					found.append((m / total, m, "add", {"add": k}, False))

		def maskok(dflt):
			def f(o, r):
				if not o:
					if dflt == 2:
						return all(x in ("", "FALSE") for x in r) or all(x == "TRUE" for x in r)
					return all(x == "TRUE" for x in r) if dflt else all(x in ("", "FALSE") for x in r)
				if not o[0].lstrip("-").isdigit():
					return False
				v = int(o[0])
				return _ref_norm(["TRUE" if v >> i & 1 else "FALSE" for i in range(len(r))]) == _ref_norm(r) \
					and v >> max(len(r), 1) == 0
			return f
		if any(len(r) > 1 for _, r in pairs):
			for d in (0, 1, 2):
				m = score(maskok(d))
				if m:
					found.append((m / total, m, "mask", {"mask": 1, "dflt": d} if d else {"mask": 1}, True))
		if names:
			def rowok(o, r):
				if not o:
					return not r
				if not o[0].lstrip("-").isdigit():
					return False
				v = int(o[0])
				return (r == [] and v == -1) or (0 <= v < len(names) and r == [names[v]])
			m = score(rowok)
			if m:
				found.append((m / total, m, "rowref", {"rowref": 1}, False))
	mp, ok = {}, True									# enum: number <-> text, functional over aligned positions
	for o, r in pairs:
		n = max(len(o), len(r))
		for a, b in zip(o + [""] * (n - len(o)), r + [""] * (n - len(r))):
			if mp.setdefault(a, b) != b:
				ok = False
	nkeys = len([k for k in mp if k != ""])
	if ok and nkeys and any(a != b for a, b in mp.items() if a != "") and all(a.lstrip("-").isdigit() or a == "" or a == b for a, b in mp.items()) \
			and not all(b.lstrip("-").isdigit() or b == "" for b in mp.values()) \
			and not all(b in ("TRUE", "FALSE", "") for b in mp.values()) \
			and nkeys <= 16 and (total < 10 or nkeys * 2 <= total):
		found.append((1.0, total, "enum", {"enum": dict(mp)}, False))
	if not found:
		return None
	found = [f for f in found if f[0] >= 0.9 and (f[2] not in ("rowref", "enum") or f[0] == 1.0)]
	if not found:
		return None
	found.sort(key=lambda t: (-t[0], RANK[t[2]], -t[1]))
	sc, m, rk, xf, isb = found[0]
	return sc, m, RANK[rk], xf, isb


def match_level(cand_srcs, ref_cols, pairs_for, names, min_score=0.9):
	"""Greedy one-to-one matching of reference columns to stored sources -> {ref_col: (src, kind, xf, isbool)}."""
	options = []
	for j in ref_cols:
		for src, kind in cand_srcs:
			res = try_transforms(src, kind, pairs_for(src, kind, j), names)
			if res and res[0] >= min_score:
				options.append((-res[1], res[2], -res[0], str(j), src[1], j, src, kind, res[3], res[4]))
	options.sort(key=lambda t: t[:5])
	got, used = {}, set()
	for *_, j, src, kind, xf, isb in options:
		if j in got or (src[0], src[1]) in used:
			continue
		got[j] = (src, kind, xf, isb)
		used.add((src[0], src[1]))
	return got


def compatible(kind, rtype):
	return {"String": kind in ("s", "vs"), "int": kind in ("i2", "i4", "i8", "vi4"), "boolean": kind == "i1",
			"float": kind == "f4"}.get(rtype, True)


def blank_pairing(ref_cols, got, cand, ours_blank, ref_blank, rtype_of):
	"""Reference columns that are empty everywhere are paired (in order) with stored sources that are empty too."""
	taken = {(g[0][0], g[0][1]) for g in got.values()}
	free = sorted([s for s in cand if (s[0][0], s[0][1]) not in taken and ours_blank(*s)],
				  key=lambda s: (s[0][0] != "f", s[0][1]))
	out = {}
	for j in ref_cols:
		if j in got or not ref_blank(j):
			continue
		for s in free:
			if compatible(s[1], rtype_of(j)):
				free.remove(s)
				out[j] = (s[0], s[1], None, False)
				break
	return out


def fill_blank(col, raws):
	"""Decide how blank cells are read back (majority over the stored data)."""
	xf = col["xf"] or {}
	outs = []
	for v in raws:
		if "mask" in xf:
			outs.append("absent" if v is None else ("empty" if v == 0 else None))
		elif col["vec"]:
			if v is None:
				outs.append("absent")
			elif v == []:
				outs.append("empty")
			elif isinstance(v, list) and len(v) == 1 and v[0] in ("", 0):
				outs.append("one")
		elif col["ty"] == "String" and not col["js"]:
			outs.append("empty" if v == "" else ("absent" if v is None else None))
	outs = [o for o in outs if o]
	col["blank"] = Counter(outs).most_common(1)[0][0] if outs else "absent"


def ty_from_ref(t, kind, isbool):
	t = (t or "").strip()
	if isbool:
		return "boolean"
	if t in ("String", "int", "float", "boolean"):
		return t
	return "float" if kind == "f4" else ("int" if is_scalar_kind(kind) else "String")


def _mk(h, src, kind, xf, isb, rtype):
	vec = kind in ("vs", "vi4") or (xf is not None and "mask" in xf)
	return new_col(h, src, ty_from_ref(rtype, kind, isb), vec=vec, xf=xf)


def learn(plan, records, schema, ref, show_raw=False):
	"""Rename / reorder columns and learn value conversions from a parsed reference CSV."""
	if ref is None or ref["layout"] != plan["layout"]:
		return plan
	names = [r["name"] for r in records]
	out = (_learn_flat if plan["layout"] == "flat" else _learn_nested)(plan, records, schema, ref, names)
	if show_raw:										 # unmapped stored fields become visible raw columns "_f<id>"
		for c in out.pop("hidden", []):
			out["cols"].append(dict(c, h="_f%d" % c["src"][1]))
		if out.get("sub"):
			for c in out["sub"].pop("hidden", []):
				out["sub"]["cols"].append(dict(c, h="_f%d" % c["src"][1]))
	return out


def _learn_flat(plan, records, schema, ref, names):
	hdr, rtypes, groups = ref["hdr"], ref["types"], ref["groups"]
	ni = hdr.index("Name")
	gi = hdr.index("GlobalID") if "GlobalID" in hdr else None
	by_gid = {g[0][gi]: g for g in groups} if gi is not None else {}
	by_name = {}
	for g in groups:
		by_name.setdefault(g[0][ni], g)
	pairs = []
	for rec in records:
		g = by_gid.get(str(rec["gid"])) if gi is not None and rec.get("gid") is not None else by_name.get(rec["name"])
		if g:
			pairs.append((rec, g))
	if not pairs:
		return plan
	cand = [(["s", i], "s") for i in range(1, max([len(r["strs"]) for r in records] or [1]))]
	for fid, k in sorted(schema.get("R", {}).items()):
		if k not in ("t", "vt") and any(p[0]["node"].get(str(fid)) is not None for p in pairs):
			cand.append((["f", fid], k))
	ref_cols = [j for j in range(len(hdr)) if j not in (ni, gi) and hdr[j] != ""]

	def pairs_for(src, kind, j):
		return [(ours_strs(src, kind, rec), trim([row[j] if j < len(row) else "" for row in g])) for rec, g in pairs]
	got = match_level(cand, ref_cols, pairs_for, names)
	got.update(blank_pairing(ref_cols, got, cand,
							 lambda src, kind: all(not ours_strs(src, kind, rec) for rec, _ in pairs),
							 lambda j: all(not trim([row[j] if j < len(row) else "" for row in g]) for _, g in pairs),
							 lambda j: rtypes[j] if j < len(rtypes) else ""))
	cols, used = [], set()
	for j, h in enumerate(hdr):
		if j == ni:
			cols.append(new_col(h, ["name"], "String"))
		elif j == gi:
			cols.append(new_col(h, ["gid"], "int"))
		elif j in got:
			src, kind, xf, isb = got[j]
			c = _mk(h, src, kind, xf, isb, rtypes[j] if j < len(rtypes) else "")
			fill_blank(c, [raw_of(c, r) for r in records])
			cols.append(c)
			used.add((src[0], src[1]))
		elif h != "":									# shown by the reference but not derivable: empty placeholder
			cols.append(new_col(h, ["x", j], ty_from_ref(rtypes[j] if j < len(rtypes) else "", "s", False)))
	hidden = [c for c in plan["cols"]				   # stored data the reference does not show: kept in the sidecar
			  if c["src"][0] == "f" and (c["src"][0], c["src"][1]) not in used]
	return dict(plan, cols=cols, hidden=hidden)


def _learn_nested(plan, records, schema, ref, names):
	sub = plan["sub"]
	fid = sub["fid"]
	by_name = {g["name"]: g for g in ref["groups"]}
	pairs, order, rtype = [], [], {}
	for rec in records:
		g = by_name.get(rec["name"])
		if not g:
			continue
		nodes = rec["node"].get(str(fid)) or []
		if len(nodes) == len(g["subs"]):
			for node, sr in zip(nodes, g["subs"]):
				cols = {h: trim([row[i] if i < len(row) else "" for row in sr]) for i, h in enumerate(g["hdr"])}
				pairs.append(({"node": node, "strs": []}, cols))
		prev = None									  # merge the per-group header orders
		for h, t in zip(g["hdr"], g["types"] + [""] * len(g["hdr"])):
			rtype.setdefault(h, t)
			if h not in order:
				order.insert(order.index(prev) + 1 if prev in order else len(order), h)
			prev = h
	if not pairs:
		return plan
	path = "R.%d" % fid
	cand = [(["f", f], k) for f, k in sorted(schema.get(path, {}).items())
			if k not in ("t", "vt") and any(p[0]["node"].get(str(f)) is not None for p in pairs)]
	got = match_level(cand, order, lambda src, kind, h: [(ours_strs(src, kind, rec), cs[h]) for rec, cs in pairs if h in cs], [])
	got.update(blank_pairing(order, got, cand,
							 lambda src, kind: all(not ours_strs(src, kind, rec) for rec, _ in pairs),
							 lambda h: all(not cs.get(h) for _, cs in pairs), lambda h: rtype.get(h, "")))
	cols, used = [], set()
	for h in order:
		if h in got:
			src, kind, xf, isb = got[h]
			c = _mk(h, src, kind, xf, isb, rtype.get(h))
			fill_blank(c, [raw_of(c, p[0]) for p in pairs])
			cols.append(c)
			used.add(src[1])
		else:
			cols.append(new_col(h, ["x", h], ty_from_ref(rtype.get(h), "s", False)))
	hidden = [c for c in sub["cols"] if c["src"][1] not in used]
	top = [c for c in plan["cols"] if c["src"][0] == "name"]   # the nested reference shows only the Name
	tophid = [c for c in plan["cols"] if c["src"][0] == "f"]
	return dict(plan, cols=top, hidden=tophid, sub=dict(sub, cols=cols, hidden=hidden))


def rows_to_text(rows, _unused=True):
	"""rows of (text, quoted) -> CSV text (empty cells stay empty, text cells are quoted when flagged)."""
	out = []
	for r in rows:
		out.append(",".join(('"' + t.replace('"', '""') + '"') if (q and t != "") else t for t, q in r))
	return "\n".join(out) + "\n"
