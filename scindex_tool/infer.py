"""Schema inference: field kinds are voted over all rows of a table.

Kinds: i1 i2 i4 i8 f4 (scalars)  s (string)  vs (string vector)  vi4 (int32 vector)
	   vt (table vector)  t (sub-table)
No table or column names are known to the code; everything is derived from the bytes.
"""
import struct

from .flatbuf import FBTable, _i32, _u16, _u32

def _valid_string(fb, t):
	if t < 4 or t % 4 or t + 4 > len(fb):
		return None
	n = _u32(fb, t)
	if t + 4 + n + 1 > len(fb) or fb[t + 4 + n] != 0:
		return None
	raw = fb[t + 4:t + 4 + n]
	if any(x < 0x20 and x not in (9, 10, 13) for x in raw):
		return None						  # a "string" with control characters is really a vector/integer
	# padding bytes after the string must be zero
	end = t + 4 + n + 1
	if any(fb[end:end + ((-end) % 4)]):
		return None
	try:
		return raw.decode("utf-8")
	except UnicodeDecodeError:
		return None


def _valid_table(fb, t):
	if t < 4 or t % 4 or t + 4 > len(fb):
		return False
	vt = t - _i32(fb, t)
	if vt < 0 or vt % 2 or vt + 4 > len(fb):
		return False
	vsz, tsz = _u16(fb, vt), _u16(fb, vt + 2)
	if vsz < 4 or vsz % 2 or vt + vsz > len(fb) or tsz < 4 or t + tsz > len(fb):
		return False
	for i in range((vsz - 4) // 2):
		o = _u16(fb, vt + 4 + 2 * i)
		if o >= tsz or (o and o < 4):
			return False
	return True


def _ptr(fb, a):
	v = _u32(fb, a)
	t = a + v
	if v == 0 or v % 4 or t + 4 > len(fb):
		return None
	return t


class Inferer:
	def __init__(self, extents=None):
		self.schema = {}					 # path -> {fid: kind}
		self.extents = extents or {}		 # id(fb) -> sorted object start addresses

	def run(self, roots):
		"""roots: [(fb, pos)]  (root table of each row)"""
		self._path("R", roots)
		return self.schema

	def _extent(self, fb, t):
		st = self.extents.get(id(fb))
		if not st:
			return None
		import bisect
		i = bisect.bisect_right(st, t)
		return (st[i] if i < len(st) else len(fb)) - t

	def _path(self, path, insts):
		per = {}
		for fb, pos in insts:
			tb = FBTable(fb, pos)
			for fid, o in enumerate(tb.offs):
				if o:
					per.setdefault(fid, []).append((fb, pos + o, tb.sizes[fid]))
		sch = {}
		children = {}
		for fid, items in sorted(per.items()):
			kind, kids = self._kind(items, self)
			sch[fid] = kind
			if kids:
				children[fid] = kids
		self.schema[path] = sch
		for fid, kids in children.items():
			self._path("%s.%d" % (path, fid), kids)

	@staticmethod
	def _kind(items, self=None):
		sizes = {s for _, _, s in items}
		if sizes != {4}:
			s = min(sizes)
			return "i%d" % s, None
		ptrs = [(fb, a, _ptr(fb, a)) for fb, a, _ in items]
		if any(t is None for _, _, t in ptrs):
			return "i4", None
		# tablo?
		if all(t > a and _valid_table(fb, t) for fb, a, t in ptrs):
			return "t", [(fb, t) for fb, a, t in ptrs]
		# string? (at least one non-empty string and all valid)
		if all(t > a and _valid_string(fb, t) is not None for fb, a, t in ptrs):
			if any(_u32(fb, t) > 0 for fb, a, t in ptrs):
				return "s", None
		# vector?
		ok, nonempty = True, []
		for fb, a, t in ptrs:
			if t <= a:
				ok = False
				break
			n = _u32(fb, t)
			if n > 200000 or t + 4 + 4 * n > len(fb):
				ok = False
				break
			if n:
				nonempty.append((fb, t, n))
		if not ok:
			return "i4", None
		if not nonempty:
			# all empty: empty string (8 bytes) or empty vector (4 bytes)? decided by object extent
			ext = [self._extent(fb, t) for fb, a, t in ptrs] if self else [None]
			if any(e is not None for e in ext):
				return ("vs", None) if any(e == 4 for e in ext if e is not None) else ("s", None)
			if all(_u32(fb, t + 4) == 0 if t + 8 <= len(fb) else False for fb, a, t in ptrs):
				return "s", None
			return "vs", None
		def elems(fb, t, n):
			return [t + 4 + 4 * k + _u32(fb, t + 4 + 4 * k) for k in range(n)]
		if all(all(e > t and _valid_table(fb, e) for e in elems(fb, t, n)) for fb, t, n in nonempty):
			kids = [(fb, e) for fb, t, n in nonempty for e in elems(fb, t, n)]
			return "vt", kids
		if all(all(e > t and _valid_string(fb, e) is not None for e in elems(fb, t, n)) for fb, t, n in nonempty):
			return "vs", None
		return "vi4", None


_FMT = {"i1": "B", "i2": "H", "i4": "i", "i8": "q", "f4": "f"}


def _collect_starts(fb, pos, path, schema, starts, done):
	if (pos, path) in done:
		return
	done.add((pos, path))
	starts.add(pos)
	starts.add(pos - _i32(fb, pos))
	tb = FBTable(fb, pos)
	sch = schema.get(path, {})
	for fid, o in enumerate(tb.offs):
		if not o or sch.get(fid, "i4") in _FMT:
			continue
		t = _ptr(fb, pos + o)
		if t is None:
			continue
		starts.add(t)
		kind = sch[fid]
		if kind == "t":
			_collect_starts(fb, t, "%s.%d" % (path, fid), schema, starts, done)
		elif kind in ("vs", "vt"):
			n = _u32(fb, t)
			for k in range(min(n, 100000)):
				e = t + 4 + 4 * k + _u32(fb, t + 4 + 4 * k)
				starts.add(e)
				if kind == "vt":
					_collect_starts(fb, e, "%s.%d" % (path, fid), schema, starts, done)



def _schema_json(schema):
	return {p: {str(f): k for f, k in sch.items()} for p, sch in schema.items()}


def _schema_load(js):
	return {p: {int(f): k for f, k in sch.items()} for p, sch in js.items()}




_SCALAR = {"i1": "B", "i2": "H", "i4": "i", "i8": "q", "f4": "f"}
_PTRK = ("s", "vs", "vi4", "vt", "t")


def f32str(bits):
	"""Shortest round-trippable text of a float32 bit pattern."""
	v = struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]
	for p in range(1, 10):
		s = "%.*g" % (p, v)
		if struct.pack("<f", float(s)) == struct.pack("<f", v):
			return s
	return repr(v)


def _looks_float(vals):
	nz = [v for v in vals if v]
	if not nz:
		return False
	for v in nz:
		e = (v >> 23) & 0xFF
		if not (0x20 <= e <= 0xDF):
			return False
	return True


class AutoInfer(Inferer):
	"""Inferer + detects float32 among 4-byte fields."""

	def _path(self, path, insts):
		per = {}
		for fb, pos in insts:
			tb = FBTable(fb, pos)
			for fid, o in enumerate(tb.offs):
				if o:
					per.setdefault(fid, []).append((fb, pos + o, tb.sizes[fid]))
		sch, children = {}, {}
		for fid, items in sorted(per.items()):
			kind, kids = self._kind(items, self)
			if kind == "i4" and _looks_float([_u32(fb, a) for fb, a, _ in items]):
				kind = "f4"
			sch[fid] = kind
			if kids:
				children[fid] = kids
		self.schema[path] = sch
		for fid, kids in children.items():
			self._path("%s.%d" % (path, fid), kids)


class Walk:
	"""Turns the FlatBuffers bytes of one row into a JSON-able tree."""

	def __init__(self, fb, schema, ext=None, counts=None):
		self.fb, self.schema, self.ext, self.counts = fb, schema, ext, counts
		self.starts, self.visits, self.ids = set(), {}, {}

	def extent(self, t):
		if not self.ext:
			return None
		import bisect
		i = bisect.bisect_right(self.ext, t)
		return (self.ext[i] if i < len(self.ext) else len(self.fb)) - t

	def table(self, pos, path):
		fb = self.fb
		self.starts.add(pos)
		self.starts.add(pos - _i32(fb, pos))
		tb = FBTable(fb, pos)
		sch = self.schema.get(path, {})
		out = {}
		for fid, o in enumerate(tb.offs):
			if not o:
				continue
			a = pos + o
			k = sch.get(fid, "i4")
			if k in _SCALAR:
				v = struct.unpack_from("<" + _SCALAR[k], fb, a)[0]
				out[str(fid)] = float(f32str(_u32(fb, a))) if k == "f4" else v
			else:
				t = _ptr(fb, a)
				if t is None:
					raise ValueError("invalid pointer")
				out[str(fid)] = self.obj(t, k, "%s.%d" % (path, fid))
		return out

	def obj(self, t, hint, path):
		self.starts.add(t)
		if self.counts is None:					   # pass 1: only count visits and collect start addresses
			n = self.visits.get(t, 0)
			self.visits[t] = n + 1
			if n:
				return None
			return self.plain(t, hint, path)
		if self.counts.get(t, 0) > 1:
			if t in self.ids:
				return {"$ref": self.ids[t]}
			sid = self.ids[t] = len(self.ids) + 1
			return {"$id": sid, "v": self.plain(t, hint, path)}
		return self.plain(t, hint, path)

	def fits(self, t, k):
		fb = self.fb
		n = _u32(fb, t)
		if k == "t":
			return _valid_table(fb, t)
		if k == "s":
			return _valid_string(fb, t) is not None and n > 0
		if n == 0 or n > 200000 or t + 4 + 4 * n > len(fb):
			return False
		el = [t + 4 + 4 * i + _u32(fb, t + 4 + 4 * i) for i in range(n)]
		if k == "vt":
			return all(e > t and _valid_table(fb, e) for e in el)
		if k == "vs":
			return all(e > t and _valid_string(fb, e) is not None for e in el)
		return True

	def plain(self, t, hint, path):
		fb = self.fb
		n = _u32(fb, t)
		kind = None
		if hint == "t" and _valid_table(fb, t):
			kind = "t"
		elif n == 0:
			e = self.extent(t)
			kind = "v0" if e == 4 else ("s" if e is not None else ("s" if hint == "s" else "v0"))
		else:
			for k in [hint] + ["s", "t", "vt", "vs", "vi4"]:
				if k in _PTRK and self.fits(t, k):
					kind = k
					break
			kind = kind or "vi4"
		if kind == "s":
			return fb[t + 4:t + 4 + n].decode("utf-8")
		if kind == "v0":
			return []
		if kind == "t":
			return self.table(t, path)
		if kind == "vi4":
			return [struct.unpack_from("<i", fb, t + 4 + 4 * i)[0] for i in range(n)]
		sub = "t" if kind == "vt" else "s"
		return [self.obj(t + 4 + 4 * i + _u32(fb, t + 4 + 4 * i), sub, path) for i in range(n)]


def row_tree(fb, schema):
	"""fb -> tree.  None on failure."""
	try:
		pos = _u32(fb, 0)
		w1 = Walk(fb, schema)
		w1.table(pos, "R")
		counts = w1.visits
		ext = sorted(w1.starts)
		w2 = Walk(fb, schema, ext=ext, counts=counts)
		return w2.table(pos, "R")
	except Exception:
		return None


def infer_schema(fbs):
	roots = [(fb, _u32(fb, 0)) for fb in fbs if fb is not None]
	if not roots:
		return {"R": {}}
	s1 = AutoInfer().run(roots)
	starts = {}
	for fb, pos in roots:
		st = set()
		try:
			_collect_starts(fb, pos, "R", s1, st, set())
		except Exception:
			st = set()
		starts[id(fb)] = sorted(st)
	return AutoInfer(starts).run(roots)
