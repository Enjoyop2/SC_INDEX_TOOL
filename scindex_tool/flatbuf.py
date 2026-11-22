"""FlatBuffers builder (byte-exact C++ clone) and schema-less reader helpers."""
import struct

M64 = (1 << 64) - 1


# ======================================================================
#  FlatBuffers builder (byte-for-byte clone of the C++ FlatBufferBuilder)
# ======================================================================

class FlatBufferBuilder:
	def __init__(self):
		self._rev = bytearray()	  # index 0 == highest address
		self._minalign = 1
		self._fields = []
		self._max_voffset = 0
		self._vtables = []
		self._nested = False
		self._objn = 0			   # created-object counter (string/vector/table/vtable)
		self._marks = []			 # total size after each object
		self._pads = {}			  # object no -> extra zero bytes inserted before it
		self._tmarks = None		  # target marks (from the original) -> padding is searched automatically
		self._padlog = {}			# paddings found

	def _obj(self, make):
		i = self._objn
		self._objn += 1
		if self._tmarks is not None and i < len(self._tmarks):
			want, before, ma = self._tmarks[i], self.size(), self._minalign
			r = None
			for p in range(0, min(want - before, 4096) + 1):
				self._fill(p)
				r = make()
				if self.size() == want:
					if p:
						self._padlog[i] = p
					break
				del self._rev[before:]
				self._minalign = ma
			else:
				r = make()
		elif i in self._pads:
			self._fill(self._pads[i])
			r = make()
		else:
			r = make()
		self._marks.append(self.size())
		return r

	def size(self):
		return len(self._rev)

	def _push_bytes(self, real_bytes):
		self._rev.extend(reversed(real_bytes))

	def _fill(self, n):
		self._rev.extend(b"\0" * n)

	def _track(self, a):
		if a > self._minalign:
			self._minalign = a

	def align(self, a):
		self._track(a)
		self._fill((-self.size()) % a)

	def pre_align(self, length, a):
		self._track(a)
		self._fill((-(self.size() + length)) % a)

	def _push_scalar(self, fmt, v):
		n = struct.calcsize(fmt)
		self.align(n)
		self._push_bytes(struct.pack("<" + fmt, v))
		return self.size()

	def _refer_to(self, off):
		self.align(4)
		return self.size() - off + 4

	# ---- objects ----
	def create_string(self, s):
		return self._obj(lambda: self._create_string(s))

	def _create_string(self, s):
		assert not self._nested
		b = s.encode("utf-8")
		self.pre_align(len(b) + 1, 4)
		self._fill(1)
		self._push_bytes(b)
		self._push_scalar("I", len(b))
		return self.size()

	def create_vector_of_offsets(self, offs):
		return self._obj(lambda: self._create_vector_of_offsets(offs))

	def _create_vector_of_offsets(self, offs):
		assert not self._nested
		self.pre_align(len(offs) * 4, 4)
		for o in reversed(offs):
			self._push_scalar("I", self._refer_to(o))
		self._push_scalar("I", len(offs))
		return self.size()

	def create_vector_of_strings(self, strs):
		offs = [self.create_string(s) for s in strs]
		return self.create_vector_of_offsets(offs)

	def create_vector_scalars(self, fmt, values):
		return self._obj(lambda: self._create_vector_scalars(fmt, values))

	def _create_vector_scalars(self, fmt, values):
		assert not self._nested
		es = struct.calcsize(fmt)
		n = len(values)
		self.pre_align(n * es, 4)
		self.pre_align(n * es, es)
		for v in reversed(values):
			self._push_scalar(fmt, v)
		self._push_scalar("I", n)
		return self.size()

	# ---- tables ----
	def start_table(self):
		assert not self._nested
		self._nested = True
		self._fields = []
		self._max_voffset = 0
		return self.size()

	def _track_field(self, fid, loc):
		self._fields.append((fid, loc))
		self._max_voffset = max(self._max_voffset, 4 + 2 * fid)

	def add_scalar(self, fid, fmt, value, default=0):
		if value == default:		 # FlatBuffers: default values are not written
			return
		self._track_field(fid, self._push_scalar(fmt, value))

	def add_offset(self, fid, off):
		if not off:
			return
		rel = self._refer_to(off)
		self._track_field(fid, self._push_scalar("I", rel))

	def end_table(self, start):
		assert self._nested
		vt_loc = self._push_scalar("i", 0)
		self._objn += 1
		self._marks.append(vt_loc)
		max_v = max(self._max_voffset + 2, 4)
		self._fill(max_v)
		vt = bytearray(max_v)
		struct.pack_into("<H", vt, 0, max_v)
		struct.pack_into("<H", vt, 2, vt_loc - start)
		for fid, loc in self._fields:
			struct.pack_into("<H", vt, 4 + 2 * fid, vt_loc - loc)
		self._rev[-max_v:] = bytes(reversed(vt))
		vt_use = self.size()
		real = None
		for old in self._vtables:		# oldest first (same as C++)
			if real is None:
				real = bytes(reversed(self._rev))
			seg = real[len(real) - old:]
			if struct.unpack_from("<H", seg, 0)[0] == max_v and seg[:max_v] == bytes(vt):
				vt_use = old
				del self._rev[-(self.size() - vt_loc):]
				break
		if vt_use == self.size():
			self._vtables.append(vt_use)
			self._objn += 1
			self._marks.append(vt_use)
		so = struct.pack("<i", vt_use - vt_loc)
		pos = vt_loc - 4
		self._rev[pos:pos + 4] = bytes(reversed(so))
		self._nested = False
		return vt_loc

	def finish(self, root):
		assert not self._nested
		self.pre_align(4, self._minalign)
		self._push_scalar("I", self._refer_to(root))
		return bytes(reversed(self._rev))


# =========================
#  FlatBuffers reader helpers
# =========================

def _u32(b, o): return struct.unpack_from("<I", b, o)[0]
def _i32(b, o): return struct.unpack_from("<i", b, o)[0]
def _u16(b, o): return struct.unpack_from("<H", b, o)[0]


def p2floor(n):
	s = 1
	while s * 2 <= n:
		s *= 2
	return min(s, 8)


class FBTable:
	"""Schema-less access to a single FlatBuffers table."""
	def __init__(self, fb, pos):
		self.fb, self.pos = fb, pos
		vt = pos - _i32(fb, pos)
		self.vt = vt
		vsz = _u16(fb, vt)
		self.tsz = _u16(fb, vt + 2)
		self.offs = [_u16(fb, vt + 4 + 2 * i) for i in range((vsz - 4) // 2)]
		# field size: power-of-two floor of the gap to the next (higher-offset) field
		self.sizes = {}
		prev = self.tsz
		for o, fid in sorted(((o, f) for f, o in enumerate(self.offs) if o), reverse=True):
			self.sizes[fid] = p2floor(prev - o)
			prev = o

	def has(self, fid):
		return fid < len(self.offs) and self.offs[fid] != 0

	def at(self, fid):
		return self.pos + self.offs[fid]

	def i32(self, fid, default=0):
		return _i32(self.fb, self.at(fid)) if self.has(fid) else default

	def u8(self, fid, default=0):
		return self.fb[self.at(fid)] if self.has(fid) else default

	def f32(self, fid, default=0.0):
		return struct.unpack_from("<f", self.fb, self.at(fid))[0] if self.has(fid) else default

	def _str_at(self, a):
		t = a + _u32(self.fb, a)
		n = _u32(self.fb, t)
		return self.fb[t + 4:t + 4 + n].decode("utf-8")

	def string(self, fid, default=""):
		return self._str_at(self.at(fid)) if self.has(fid) else default

	def _vec_at(self, a):
		v = a + _u32(self.fb, a)
		return v, _u32(self.fb, v)

	def vec_strings(self, fid):
		if not self.has(fid):
			return []
		v, n = self._vec_at(self.at(fid))
		return [self._str_at(v + 4 + 4 * i) for i in range(n)]

	def vec_scalars(self, fid, fmt):
		if not self.has(fid):
			return []
		v, n = self._vec_at(self.at(fid))
		es = struct.calcsize(fmt)
		return [struct.unpack_from("<" + fmt, self.fb, v + 4 + es * i)[0] for i in range(n)]

	def vec_tables(self, fid):
		if not self.has(fid):
			return []
		v, n = self._vec_at(self.at(fid))
		out = []
		for i in range(n):
			ep = v + 4 + 4 * i
			out.append(FBTable(self.fb, ep + _u32(self.fb, ep)))
		return out

	def vec_len(self, fid):
		if not self.has(fid):
			return None
		return self._vec_at(self.at(fid))[1]

def fb_root(fb):
	return FBTable(fb, _u32(fb, 0))
