"""Tree -> FlatBuffers bytes (inverse of infer.row_tree)."""
from .flatbuf import FlatBufferBuilder, _u32
from .infer import Walk, _SCALAR

# Object creation order (differs per writer; chosen automatically per file):
#   "A"  : fields by ascending id, each object when its turn comes
#   "B1" : (1) elements of string vectors (2) vectors + tables (3) plain strings
#   "B2" : (1) elements of string vectors (2) vectors (3) plain strings + tables
ORDERS = ("A", "B1", "B2")

def _collect_defs(v, defs):
	if isinstance(v, dict):
		if "$id" in v:
			defs[v["$id"]] = v
			_collect_defs(v["v"], defs)
		elif "$ref" not in v:
			for x in v.values():
				_collect_defs(x, defs)
	elif isinstance(v, list):
		for x in v:
			_collect_defs(x, defs)


def build_fb(tree, schema, order="A", pads=None, tmarks=None, log=None):
	"""pads: {object_no: extra zero bytes};  tmarks: original marks (padding is searched and written to log)."""
	b = FlatBufferBuilder()
	b._pads = pads or {}
	b._tmarks = tmarks
	defs = {}
	_collect_defs(tree, defs)
	root = _bt(b, tree, "R", schema, {}, order, defs)
	out = b.finish(root)
	if log is not None:
		log.update(b._padlog)
	return out


def orig_marks(fb, schema):
	w = Walk(fb, schema)
	w.table(_u32(fb, 0), "R")
	return sorted(len(fb) - a for a in w.starts)


def _inner(v, defs):
	while isinstance(v, dict) and ("$id" in v or "$ref" in v):
		v = v["v"] if "$id" in v else defs[v["$ref"]]["v"]
	return v


def _bo(b, v, path, schema, shared, order, defs, pre=None):
	if isinstance(v, dict) and "$ref" in v:
		n = v["$ref"]
		if n not in shared:
			shared[n] = _bo(b, defs[n]["v"], path, schema, shared, order, defs)
		return shared[n]
	if isinstance(v, dict) and "$id" in v:
		n = v["$id"]
		if n in shared:
			return shared[n]
		off = _bo(b, v["v"], path, schema, shared, order, defs, pre)
		shared[n] = off
		return off
	if isinstance(v, str):
		return b.create_string(v)
	if isinstance(v, dict):
		return _bt(b, v, path, schema, shared, order, defs)
	if not v:
		return b.create_vector_scalars("i", [])
	if any(isinstance(e, (str, dict, list)) for e in v):
		offs = pre if pre is not None else [_bo(b, e, path, schema, shared, order, defs) for e in v]
		return b.create_vector_of_offsets(offs)
	return b.create_vector_scalars("i", v)


def _bt(b, node, path, schema, shared, order, defs):
	sch = schema.get(path, {})
	fids = sorted(int(k) for k in node)
	ptr = [f for f in fids if sch.get(f, "i4") not in _SCALAR]
	objs, pre = {}, {}
	if order == "A":
		for f in ptr:
			objs[f] = _bo(b, node[str(f)], "%s.%d" % (path, f), schema, shared, order, defs)
	else:
		cp = lambda f: "%s.%d" % (path, f)
		for f in ptr: # 1) elements of string vectors
			v = _inner(node[str(f)], defs)
			if isinstance(v, list) and v and all(isinstance(_inner(e, defs), str) for e in v):
				pre[f] = [_bo(b, e, cp(f), schema, shared, order, defs) for e in v]
		for f in ptr: # 2) vectors (+ B1: tables)
			v = _inner(node[str(f)], defs)
			if isinstance(v, list) or (order == "B1" and isinstance(v, dict)):
				objs[f] = _bo(b, node[str(f)], cp(f), schema, shared, order, defs, pre.get(f))
		for f in ptr: # 3) the rest (strings, B2: tables)
			if f not in objs:
				objs[f] = _bo(b, node[str(f)], cp(f), schema, shared, order, defs)
	t = b.start_table()
	for f in fids:
		k = sch.get(f, "i4")
		if k in _SCALAR:
			b._track_field(f, b._push_scalar(_SCALAR[k], node[str(f)]))
		else:
			b.add_offset(f, objs[f])
	return b.end_table(t)
