#!/usr/bin/env python3
"""
make_delete_patch.py -- builds a .patch for apply_patch.py from ANCHORS, copying the real file
text (so large deletions are never retyped). Standard library only. Lives next to apply_patch.py.

USAGE
  python3 make_delete_patch.py patches/NAME.spec.json
  -> writes the patch named by "out" in the spec; then run apply_patch.py on it as usual.

SPEC (JSON)
  {"out": "patches/NAME.patch",
   "files": [{"file": "auto_run.py", "blocks": [ ...block... ]}]}

BLOCKS (compared with trailing whitespace ignored)
  range : {"start": "<exact line>", "end": "<exact line>", "end_back": 0, "with": ["line", ...]}
          old text = file lines from the UNIQUE start line up to (not including) the first
          end line after it, minus "end_back" lines; "with" (default empty = delete) are raw lines.
  line  : {"line": "<line text, indentation ignored>", "with": ["...", ...], "keep_indent": false}
          the line must occur exactly once; keep_indent=true prefixes every "with" line with
          the found line's own indentation.
Any anchor that is missing/ambiguous, any empty range, or overlapping blocks aborts with a message
and nothing is written.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def die(msg):
    print("ERROR: " + msg)
    sys.exit(1)


def load_lines(path):
    with open(path, 'rb') as f:
        text = f.read().decode('utf-8')
    return text.replace('\r\n', '\n').split('\n')


def hits_of(lines, text, lo=0):
    t = text.rstrip()
    return [i for i in range(lo, len(lines)) if lines[i].rstrip() == t]


def build_file(rel, blocks):
    lines = load_lines(os.path.join(ROOT, rel))
    spans, out = [], []
    for n, b in enumerate(blocks, 1):
        tag = "%s block %d" % (rel, n)
        if 'start' in b:
            h = hits_of(lines, b['start'])
            if len(h) != 1:
                die("%s: start line %r found %d times (need exactly 1)" % (tag, b['start'], len(h)))
            s = h[0]
            e_hits = hits_of(lines, b['end'], s + 1)
            if not e_hits:
                die("%s: end line %r not found after the start line" % (tag, b['end']))
            e = e_hits[0] - int(b.get('end_back', 0))
            if e <= s:
                die("%s: empty range (start line %d, end %d)" % (tag, s + 1, e))
            old, new = lines[s:e], list(b.get('with', []))
        elif 'line' in b:
            want = b['line'].strip()
            h = [i for i, l in enumerate(lines) if l.strip() == want]
            if len(h) != 1:
                die("%s: line %r found %d times (need exactly 1)" % (tag, want, len(h)))
            s = h[0]
            e = s + 1
            old = [lines[s]]
            ind = lines[s][:len(lines[s]) - len(lines[s].lstrip())]
            new = [(ind + w) if b.get('keep_indent') else w for w in b.get('with', [])]
        else:
            die("%s: needs 'start'/'end' or 'line'" % tag)
        if not any(l.strip() for l in old):
            die("%s: old text is blank" % tag)
        if any(l.startswith('@@@') for l in old + new):
            die("%s: text contains a line starting with @@@" % tag)
        spans.append((s, e, tag))
        out.append("@@@ REPLACE")
        out.extend(old)
        out.append("@@@ WITH")
        out.extend(new)
        out.append("@@@ END")
        print("  %-28s lines %d-%d  (%d old -> %d new)" % (tag, s + 1, e, len(old), len(new)))
    spans.sort()
    for (s1, e1, t1), (s2, e2, t2) in zip(spans, spans[1:]):
        if s2 < e1:
            die("%s overlaps %s" % (t2, t1))
    return ["@@@ FILE " + rel] + out


def main():
    if len(sys.argv) != 2:
        die("usage: python3 make_delete_patch.py patches/NAME.spec.json")
    with open(sys.argv[1], encoding='utf-8') as f:
        spec = json.load(f)
    text = []
    for fs in spec['files']:
        print(fs['file'])
        text.extend(build_file(fs['file'], fs['blocks']))
    dest = os.path.join(ROOT, spec['out'])
    if os.path.exists(dest):
        die("refusing to overwrite existing " + spec['out'])
    with open(dest, 'w', encoding='utf-8', newline='\n') as f:
        f.write("\n".join(text) + "\n")
    print("wrote " + spec['out'] + "  ->  python3 apply_patch.py %s --dry-run" % spec['out'])


if __name__ == '__main__':
    main()
