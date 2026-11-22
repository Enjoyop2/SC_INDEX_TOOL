"""Typed JSON view of a table (numbers, true/false, strings, nested values) <-> CSV text + sidecar."""
import csv
import io
import json

from .infer import _schema_load
from .tablecodec import cell_text, csv_line, is_field_col, parse_cell


def to_json(text, sidecar):
	cols, schema = sidecar["cols"], _schema_load(sidecar["schema"])
	bools = set(sidecar.get("bools", []))
	rows = [r for r in csv.reader(io.StringIO(text, newline="")) if r]
	if sidecar.get("header"):
		rows = rows[2:]
	out = []
	for r, info in zip(rows, sidecar["rows"]):
		d = {}
		for i, cn in enumerate(cols):
			s = r[i] if i < len(r) else ""
			if s == "":
				continue
			if is_field_col(cn):
				f = int(cn[1:])
				v = parse_cell(s, schema["R"].get(f, "s"))
				d[cn] = bool(v) if f in bools else v
			elif cn == "GlobalID":
				d[cn] = int(s)
			else:
				d[cn] = s
		d["_x"] = {k: v for k, v in info.items()}		# hashes etc.; keep when editing
		out.append(d)
	meta = {k: v for k, v in sidecar.items() if k != "rows"}
	meta["header"] = False
	return json.dumps({"meta": meta, "rows": out}, ensure_ascii=False, indent=1)


def from_json(js):
	obj = json.loads(js)
	meta = obj["meta"]
	cols, schema = meta["cols"], _schema_load(meta["schema"])
	bools = set(meta.get("bools", []))
	lines, side = [], []
	for pos, d in enumerate(obj["rows"]):
		cells, q = [], []
		for cn in cols:
			v = d.get(cn)
			if is_field_col(cn):
				f = int(cn[1:])
				s, qq = cell_text(None if v is None else (int(v) if f in bools else v),
					"b" if f in bools else schema["R"].get(f, "s"))
			else:
				s, qq = ("" if v is None else str(v)), (cn == "Name" or cn.startswith("Str"))
			cells.append(s)
			q.append(qq)
		lines.append(csv_line(cells, q))
		x = dict(d.get("_x") or {})
		if x:
			x["key"] = d.get("GlobalID") and str(d["GlobalID"]) or "#%d" % pos
			side.append(x)
	sidecar = dict(meta)
	sidecar["rows"] = side
	return "\n".join(lines) + "\n", sidecar
