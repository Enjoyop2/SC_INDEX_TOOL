"""Command line interface.

    python -m scindex_tool decode [ROOT] [--json] [--headers | --no-headers] [--no-verify]
    python -m scindex_tool encode [ROOT] [--json]

ROOT layout (default ROOT is the current directory):
    scindex/   *.scindex files (+ optional reference *.csv from the game; never modified)
    csv/       generated *.csv (or *.json with --json); edit these
    meta/      generated sidecar files (*.meta.json); needed by `encode`, keep them
"""
import json
import os
import sys
import time

from .container import Container
from .gamecodec import from_game_csv, to_game_csv
from .jsoncodec import from_json, to_json
from .tablecodec import container_to_csv, csv_to_container

USAGE = ("usage:\n"
		 "  scindex_tool decode [ROOT] [--json] [--headers | --no-headers] [--no-verify]\n"
		 "  scindex_tool encode [ROOT] [--json]\n"
		 "ROOT contains the folders: scindex/ (input), csv/ (output), meta/ (sidecars)")

def base_name(filename):
	stem = filename.rsplit(".", 1)[0]
	return stem[:-len("_logic")] if stem.endswith("_logic") else stem

def write_file(path, data):
	"""Write atomically; overwrites an existing file."""
	tmp = path + ".tmp"
	if isinstance(data, bytes):
		with open(tmp, "wb") as f:
			f.write(data)
	else:
		with open(tmp, "w", encoding="utf-8", newline="") as f:
			f.write(data)
	os.replace(tmp, path)

def read_text(path):
	with open(path, "r", encoding="utf-8-sig", newline="") as f:
		return f.read()

def dump_json(obj):
	return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def decode(root, as_json=False, headers=None, verify=True, show_raw=False):
	src, out, meta = (os.path.join(root, d) for d in ("scindex", "csv", "meta"))
	os.makedirs(out, exist_ok=True)
	os.makedirs(meta, exist_ok=True)
	ok = failed = 0
	for name in sorted(os.listdir(src)):
		if not name.lower().endswith(".scindex"):
			continue
		t0 = time.time()
		base = base_name(name)
		try:
			data = open(os.path.join(src, name), "rb").read()
			ref = os.path.join(src, base + ".csv")
			kw = {} if as_json else {"show_raw": show_raw}
			conv = container_to_csv if as_json else to_game_csv
			text, side, raw = conv(Container.read(data), ref_csv=ref if os.path.isfile(ref) else None,
								   headers=headers, filename=name, **kw)
			if verify:
				back = csv_to_container if as_json else from_game_csv
				rebuilt = back(text, side)[0].write()
				if rebuilt != data:
					raise ValueError("round-trip check failed (rebuilt file differs)")
			if as_json:
				write_file(os.path.join(out, base + ".json"), to_json(text, side))
				target = base + ".json"
			else:
				write_file(os.path.join(out, base + ".csv"), text)
				write_file(os.path.join(meta, base + ".meta.json"), dump_json(side))
				target = base + ".csv"
			ok += 1
			print("decode  %-36s -> %-28s %5d rows, %d raw%s  (%.1fs)" % (
				name, target, len(side["rows"]), raw, ", verified" if verify else "", time.time() - t0))
		except Exception as e:
			failed += 1
			print("error   %-36s %s" % (name, e))
	print("done: %d ok, %d failed" % (ok, failed))
	return 1 if failed else 0

def encode(root, as_json=False):
	dst, src, meta = (os.path.join(root, d) for d in ("scindex", "csv", "meta"))
	ext = ".json" if as_json else ".csv"
	ok = failed = 0
	for name in sorted(os.listdir(src)):
		if not name.lower().endswith(ext):
			continue
		base = name[:-len(ext)]
		try:
			if as_json:
				text, side = from_json(read_text(os.path.join(src, name)))
			else:
				mpath = os.path.join(meta, base + ".meta.json")
				if not os.path.isfile(mpath):
					raise ValueError("missing meta/%s.meta.json (run decode first)" % base)
				text, side = read_text(os.path.join(src, name)), json.loads(read_text(mpath))
			c, warnings = (csv_to_container if as_json else from_game_csv)(text, side)
			target = side.get("file") or base + "_logic.scindex"
			data = c.write()
			write_file(os.path.join(dst, target), data)
			ok += 1
			print("encode  %-36s -> %-36s %5d rows, %d bytes" % (name, target, len(c.rows), len(data)))
			for w in warnings[:5]:
				print("note: " + w)
			if len(warnings) > 5:
				print("NOTE: ... and %d more new rows" % (len(warnings) - 5))
		except Exception as e:
			failed += 1
			print("error   %-36s %s" % (name, e))
	print("done: %d ok, %d failed" % (ok, failed))
	return 1 if failed else 0

def main(argv=None):
	argv = list(sys.argv[1:] if argv is None else argv)
	flags = {x for x in argv if x.startswith("--")}
	args = [x for x in argv if not x.startswith("--")]
	if not args or args[0] not in ("decode", "encode") or len(args) > 2:
		print(USAGE)
		return 2
	root = args[1] if len(args) == 2 else "."
	if not os.path.isdir(os.path.join(root, "scindex")):
		print("error: folder not found: " + os.path.join(root, "scindex"))
		return 2
	if args[0] == "decode":
		headers = True if "--headers" in flags else (False if "--no-headers" in flags else None)
		return decode(root, "--json" in flags, headers, "--no-verify" not in flags, "--raw-columns" in flags)
	if not os.path.isdir(os.path.join(root, "csv")):
		print("error: folder not found: " + os.path.join(root, "csv"))
		return 2
	return encode(root, "--json" in flags)
