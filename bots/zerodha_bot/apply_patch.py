#!/usr/bin/env python3
"""
apply_patch.py -- safe, line-exact patch tool for the zerodha_bot project.
Standard library only (Python 3.8+). Lives in the project root, next to auto_run.py.

USAGE
  python3 apply_patch.py patches/NAME.patch [--dry-run] [--no-name-check]
  python3 apply_patch.py undo [STAMP]    restore the files changed by the last patch (or STAMP)
  python3 apply_patch.py list            list backups
  python3 apply_patch.py index           regenerate .patch_backups/index.txt (def -> line map)
  python3 apply_patch.py check           scan the whole project: undefined names + broken local imports
  python3 apply_patch.py selftest        run the built-in tests in a temp directory

PATCH FORMAT (paths are relative to this directory; one FILE section per file)
  @@@ FILE ui_components/patterns.py
  @@@ REPLACE
  <exact existing line(s)>
  @@@ WITH
  <replacement line(s); leave empty to delete the old lines>
  @@@ END
  ... more REPLACE blocks, then more FILE sections ...
  (blank lines and lines starting with '#' BETWEEN blocks are ignored)

GUARANTEES
  * Blocks match whole lines and must match EXACTLY ONE place (trailing whitespace is
    ignored only if there is no exact match). Zero or several matches abort everything.
  * Everything is computed in memory first. If ANY block, check or file fails, NOTHING
    is written (all-or-nothing across files).
  * Before writing, the result must pass: full compile (syntax), no NEW undefined names,
    and no NEW unresolved 'from x import y' between local modules (this also catches a
    removed/renamed function that another file still imports).
  * One timestamped backup set per patch under .patch_backups/ (git-ignored), written
    atomically, then re-read and compared byte-for-byte; any mismatch rolls back.
  * 'undo' restores the last backup set (and is itself undoable).
"""
import ast
import builtins
import difflib
import io
import os
import shutil
import symtable
import sys
import tempfile
import time
import warnings
from contextlib import redirect_stdout

warnings.simplefilter('ignore')

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(ROOT, '.patch_backups')
KEEP_BACKUPS = 40
SKIP_DIRS = {'__pycache__', 'daily_logs', 'node_modules'}


class PatchError(Exception):
    pass


# --------------------------------------------------------------------------- parsing

def parse_patch(text):
    """-> list of (relpath, [(old_lines, new_lines, start_lineno), ...])"""
    files = []
    seen = set()
    cur = None
    state = None
    old = new = None
    start = 0
    for lineno, raw in enumerate(text.split('\n'), 1):
        line = raw.rstrip('\r')
        if line.startswith('@@@ FILE '):
            if state is not None:
                raise PatchError("line %d: FILE inside an unfinished block" % lineno)
            rel = line[len('@@@ FILE '):].strip()
            if not rel:
                raise PatchError("line %d: FILE needs a path" % lineno)
            if rel in seen:
                raise PatchError("line %d: duplicate FILE section for %s" % (lineno, rel))
            seen.add(rel)
            cur = (rel, [])
            files.append(cur)
        elif line == '@@@ REPLACE':
            if cur is None:
                raise PatchError("line %d: REPLACE before any FILE" % lineno)
            if state is not None:
                raise PatchError("line %d: REPLACE inside an unfinished block" % lineno)
            state, old, new, start = 'old', [], [], lineno
        elif line == '@@@ WITH':
            if state != 'old':
                raise PatchError("line %d: WITH without a matching REPLACE" % lineno)
            state = 'new'
        elif line == '@@@ END':
            if state != 'new':
                raise PatchError("line %d: END without REPLACE ... WITH" % lineno)
            if not any(l.strip() for l in old):
                raise PatchError("block at line %d: old text is empty/blank" % start)
            cur[1].append((old, new, start))
            state = None
        elif state == 'old':
            old.append(line)
        elif state == 'new':
            new.append(line)
        elif line.strip() and not line.startswith('#'):
            raise PatchError("line %d: unexpected text outside a block: %r" % (lineno, line[:60]))
    if state is not None:
        raise PatchError("unterminated block starting at line %d" % start)
    if not files:
        raise PatchError("patch contains no FILE sections")
    for rel, blocks in files:
        if not blocks:
            raise PatchError("FILE %s has no REPLACE blocks" % rel)
    return files


def safe_target(rel):
    rel = rel.replace('\\', '/').strip()
    if not rel or rel.startswith('/') or (len(rel) > 1 and rel[1] == ':'):
        raise PatchError("path must be relative to the project directory: %r" % rel)
    norm = os.path.normpath(rel)
    if norm == '..' or norm.startswith('..' + os.sep):
        raise PatchError("path escapes the project directory: %r" % rel)
    full = os.path.abspath(os.path.join(ROOT, norm))
    if os.path.commonpath([ROOT, full]) != ROOT:
        raise PatchError("path escapes the project directory: %r" % rel)
    if norm.split(os.sep)[0] == '.patch_backups':
        raise PatchError("refusing to patch inside .patch_backups")
    return norm, full


# ------------------------------------------------------------------------ file text

def read_text(path):
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise PatchError("not valid UTF-8: %s" % path)
    crlf_count = text.count('\r\n')
    crlf = crlf_count > 0
    if crlf:
        if text.count('\n') != crlf_count:
            raise PatchError("mixed line endings (CRLF and LF) in %s -- refusing" % path)
        text = text.replace('\r\n', '\n')
    return text, crlf


def encode_text(text, crlf):
    if crlf:
        text = text.replace('\n', '\r\n')
    return text.encode('utf-8')


# ---------------------------------------------------------------------- block match

def _find_all(lines, key, rstrip):
    n = len(key)
    if rstrip:
        view = [l.rstrip() for l in lines]
        key = [l.rstrip() for l in key]
    else:
        view = lines
    hits = []
    first = key[0]
    for i in range(len(view) - n + 1):
        if view[i] == first and view[i:i + n] == key:
            hits.append(i)
    return hits


def _diagnose(lines, old):
    first = old[0].strip()
    for i, l in enumerate(lines):
        if l.strip() == first:
            for k, want in enumerate(old):
                got = lines[i + k] if i + k < len(lines) else '<end of file>'
                if got.rstrip() != want.rstrip():
                    return ("first line found at file line %d, but block line %d differs:\n"
                            "      expected: %r\n      file has: %r" % (i + 1, k + 1, want, got))
            return "first line found at file line %d" % (i + 1)
    close = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.6)
    if close:
        return "first line not found; closest line in file: %r" % close[0]
    return "first line of the block does not appear in the file: %r" % old[0]


def locate(lines, old, rel, k):
    hits = _find_all(lines, old, False)
    mode = 'exact'
    if not hits:
        hits = _find_all(lines, old, True)
        mode = 'ignoring trailing whitespace'
    if not hits:
        raise PatchError("%s block %d: old text not found.\n    %s" % (rel, k, _diagnose(lines, old)))
    if len(hits) > 1:
        raise PatchError("%s block %d: old text matches %d places (file lines %s) -- "
                         "add surrounding lines to make it unique"
                         % (rel, k, len(hits), ', '.join(str(h + 1) for h in hits[:8])))
    return hits[0], mode


def apply_blocks(rel, lines, blocks):
    spans, notes = [], []
    for k, (old, new, _start) in enumerate(blocks, 1):
        i, mode = locate(lines, old, rel, k)
        spans.append((i, i + len(old), new, k))
        if mode != 'exact':
            notes.append("block %d matched %s" % (k, mode))
    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if a[1] > b[0]:
            raise PatchError("%s: blocks %d and %d overlap" % (rel, a[3], b[3]))
    out = list(lines)
    for s, e, new, _k in reversed(spans):
        out[s:e] = new
    return out, notes


# ------------------------------------------------------------------------- checks

def check_syntax(rel, text):
    try:
        compile(text, rel, 'exec', dont_inherit=True)
    except SyntaxError as e:
        raise PatchError("%s: syntax error after patch (line %s): %s" % (rel, e.lineno, e.msg))
    except ValueError as e:
        raise PatchError("%s: cannot compile after patch: %s" % (rel, e))


def _tables(t):
    yield t
    for c in t.get_children():
        for x in _tables(c):
            yield x


def _has_star_import(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names):
            return True
    return False


def _bound(s):
    return s.is_assigned() or s.is_imported() or s.is_namespace()


def module_names(text, rel='<module>'):
    """Names bound at module level (None if unknowable: syntax error or star import)."""
    try:
        tree = ast.parse(text)
        if _has_star_import(tree):
            return None
        top = symtable.symtable(text, rel, 'exec')
    except (SyntaxError, ValueError):
        return None
    names = set(s.get_name() for s in top.get_symbols() if _bound(s))
    for t in _tables(top):
        if t is top:
            continue
        for s in t.get_symbols():
            if s.is_declared_global() and _bound(s):
                names.add(s.get_name())
    return names


def undefined_names(text, rel='<module>'):
    """Names that are read but defined nowhere reachable (None if unknowable)."""
    defined = module_names(text, rel)
    if defined is None:
        return None
    top = symtable.symtable(text, rel, 'exec')
    known = set(dir(builtins))
    bad = set()
    for t in _tables(top):
        for s in t.get_symbols():
            if s.is_referenced() and s.is_global():
                nm = s.get_name()
                if nm in defined or nm in known or (nm.startswith('__') and nm.endswith('__')):
                    continue
                bad.add(nm)
    return bad


def iter_py(root):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith('.') and d not in SKIP_DIRS]
        for f in fns:
            if f.endswith('.py'):
                yield os.path.normpath(os.path.join(dp, f))


class Project:
    """Read-only view of the project with an optional in-memory overlay of patched files."""

    def __init__(self, root, overlay=None):
        self.root = root
        self.overlay = overlay or {}
        self._names = {}

    def exists(self, path):
        return path in self.overlay or os.path.isfile(path)

    def read(self, path):
        if path in self.overlay:
            return self.overlay[path]
        try:
            return read_text(path)[0]
        except (PatchError, OSError):
            return None

    def names(self, path):
        if path not in self._names:
            text = self.read(path)
            self._names[path] = None if text is None else module_names(text, path)
        return self._names[path]

    def _resolve(self, fpath, node):
        """-> (target_file or None, directory where submodules of the target would live)"""
        if node.level:
            base = os.path.dirname(fpath)
            for _ in range(node.level - 1):
                base = os.path.dirname(base)
        else:
            base = self.root
        parts = node.module.split('.') if node.module else []
        tdir = os.path.join(base, *parts) if parts else base
        cands = [tdir + '.py', os.path.join(tdir, '__init__.py')] if parts \
            else [os.path.join(tdir, '__init__.py')]
        for c in cands:
            if self.exists(os.path.normpath(c)):
                return os.path.normpath(c), tdir
        return None, tdir

    def _is_submodule(self, tdir, name):
        return (self.exists(os.path.normpath(os.path.join(tdir, name + '.py'))) or
                self.exists(os.path.normpath(os.path.join(tdir, name, '__init__.py'))))

    def import_problems(self):
        out = set()
        files = set(iter_py(self.root)) | set(p for p in self.overlay if p.endswith('.py'))
        for fp in sorted(files):
            text = self.read(fp)
            if text is None:
                continue
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            rel = os.path.relpath(fp, self.root)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or any(a.name == '*' for a in node.names):
                    continue
                spec = '.' * node.level + (node.module or '')
                target, tdir = self._resolve(fp, node)
                if target is None:
                    if node.level:
                        out.add("%s: 'from %s import ...': module not found" % (rel, spec))
                    continue          # absolute and not local -> third-party, ignore
                tnames = self.names(target)
                if tnames is None:
                    continue
                for a in node.names:
                    if a.name in tnames or self._is_submodule(tdir, a.name):
                        continue
                    out.add("%s: 'from %s import %s': %s does not define it"
                            % (rel, spec, a.name, os.path.relpath(target, self.root)))
        return out


# ---------------------------------------------------------------- backups / writes

def _ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    gi = os.path.join(BACKUP_DIR, '.gitignore')
    if not os.path.exists(gi):
        with open(gi, 'w') as f:
            f.write('*\n')


def _new_stamp_dir():
    base = time.strftime('%Y%m%d_%H%M%S')
    d, n = os.path.join(BACKUP_DIR, base), 1
    while os.path.exists(d):
        n += 1
        d = os.path.join(BACKUP_DIR, '%s_%d' % (base, n))
    return d


def stamps():
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted(d for d in os.listdir(BACKUP_DIR)
                  if os.path.isdir(os.path.join(BACKUP_DIR, d)))


def make_backup(abs_paths, note):
    _ensure_backup_dir()
    d = _new_stamp_dir()
    os.makedirs(d)
    rels = []
    for p in abs_paths:
        rel = os.path.relpath(p, ROOT)
        dst = os.path.join(d, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(p, dst)
        rels.append(rel)
    with open(os.path.join(d, 'MANIFEST.txt'), 'w') as f:
        f.write('NOTE %s\n' % note)
        for r in rels:
            f.write('FILE %s\n' % r)
    for old in stamps()[:-KEEP_BACKUPS]:
        shutil.rmtree(os.path.join(BACKUP_DIR, old), ignore_errors=True)
    return d


def read_manifest(d):
    note, files = '', []
    p = os.path.join(d, 'MANIFEST.txt')
    if os.path.exists(p):
        with open(p) as f:
            for line in f.read().splitlines():
                if line.startswith('NOTE '):
                    note = line[5:]
                elif line.startswith('FILE '):
                    files.append(line[5:])
    return note, files


def atomic_write(path, data):
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.apply_patch_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(path, tmp)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)    # keep the original owner when run as root
        except (AttributeError, OSError):
            pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _log(line):
    _ensure_backup_dir()
    with open(os.path.join(BACKUP_DIR, 'patch.log'), 'a') as f:
        f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), line))


# ------------------------------------------------------------------------- index

def write_index():
    _ensure_backup_dir()
    out = ['# generated by apply_patch.py %s -- top-level defs/classes with line ranges'
           % time.strftime('%Y-%m-%d %H:%M:%S')]
    for fp in sorted(iter_py(ROOT)):
        try:
            text, _ = read_text(fp)
            tree = ast.parse(text)
        except (PatchError, SyntaxError, OSError, ValueError):
            continue
        total = len(text.split('\n')) - (1 if text.endswith('\n') else 0)
        out.append('== %s (%d lines)' % (os.path.relpath(fp, ROOT), total))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append('  def %s L%d-%d' % (node.name, node.lineno, node.end_lineno))
            elif isinstance(node, ast.ClassDef):
                out.append('  class %s L%d-%d' % (node.name, node.lineno, node.end_lineno))
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append('    def %s L%d-%d' % (m.name, m.lineno, m.end_lineno))
    path = os.path.join(BACKUP_DIR, 'index.txt')
    with open(path, 'w') as f:
        f.write('\n'.join(out) + '\n')
    return path


# --------------------------------------------------------------------------- apply

def _diff_stats(old, new):
    plus = minus = 0
    for l in difflib.unified_diff(old.split('\n'), new.split('\n'), lineterm='', n=0):
        if l.startswith('+') and not l.startswith('+++'):
            plus += 1
        elif l.startswith('-') and not l.startswith('---'):
            minus += 1
    return plus, minus


def apply_patch_text(text, label, dry_run=False, name_check=True):
    """Returns 0 on success (or nothing to do), 1 on any failure (nothing written)."""
    try:
        sections = parse_patch(text)
        plan, overlay = [], {}
        for rel, blocks in sections:
            norm, full = safe_target(rel)
            if not os.path.isfile(full):
                raise PatchError("%s: file does not exist (this tool never creates files)" % norm)
            old_text, crlf = read_text(full)
            new_lines, notes = apply_blocks(norm, old_text.split('\n'), blocks)
            new_text = '\n'.join(new_lines)
            plan.append(dict(rel=norm, full=full, old=old_text, new=new_text, crlf=crlf,
                             nblocks=len(blocks), notes=notes))
            if new_text != old_text:
                overlay[full] = new_text

        changed = [p for p in plan if p['new'] != p['old']]
        for p in plan:
            if p['new'] == p['old']:
                print("  (no change) %s" % p['rel'])
        if not changed:
            print("Nothing to do: the patch makes no changes.")
            return 0

        checks = ['compile']
        for p in changed:
            if p['rel'].endswith('.py'):
                check_syntax(p['rel'], p['new'])
        if name_check:
            for p in changed:
                if not p['rel'].endswith('.py'):
                    continue
                before, after = undefined_names(p['old'], p['rel']), undefined_names(p['new'], p['rel'])
                if after is None:
                    p['notes'].append("name check skipped (star import or unparsable)")
                    continue
                new_bad = sorted(after - (before or set()))
                if new_bad:
                    raise PatchError("%s: patch introduces undefined name(s): %s"
                                     % (p['rel'], ', '.join(new_bad)))
            new_imp = sorted(Project(ROOT, overlay).import_problems() - Project(ROOT).import_problems())
            if new_imp:
                raise PatchError("patch breaks local imports:\n    " + "\n    ".join(new_imp))
            checks += ['undefined names', 'local imports']
    except PatchError as e:
        print("FAILED -- nothing was changed.\n  %s" % e)
        return 1

    print("Patch: %s  (%d file(s), %d block(s))"
          % (label, len(changed), sum(p['nblocks'] for p in changed)))
    for p in changed:
        plus, minus = _diff_stats(p['old'], p['new'])
        print("  %-44s +%d -%d  (%d block%s)" % (p['rel'], plus, minus, p['nblocks'],
                                                 '' if p['nblocks'] == 1 else 's'))
        for n in p['notes']:
            print("      note: %s" % n)
    print("Checks passed: %s" % ', '.join(checks))
    for p in changed:
        print('--- diff: %s' % p['rel'])
        for l in difflib.unified_diff(p['old'].split('\n'), p['new'].split('\n'),
                                      'a/' + p['rel'], 'b/' + p['rel'], lineterm='', n=2):
            print(l)
    if dry_run:
        print("\nDry run: nothing was written.")
        return 0

    backup = make_backup([p['full'] for p in changed], 'patch %s' % label)
    try:
        for p in changed:
            atomic_write(p['full'], encode_text(p['new'], p['crlf']))
        for p in changed:                                   # read back and compare exactly
            with open(p['full'], 'rb') as f:
                if f.read() != encode_text(p['new'], p['crlf']):
                    raise PatchError("read-back mismatch in %s" % p['rel'])
    except (PatchError, OSError) as e:
        print("\nWRITE VERIFICATION FAILED (%s) -- rolling back." % e)
        _restore(backup, [p['rel'] for p in changed])
        return 1
    _log('APPLY %s -> %s : %s' % (label, os.path.basename(backup), ', '.join(p['rel'] for p in changed)))
    try:
        write_index()
    except Exception:
        pass
    print("\nApplied. Backup: %s\nUndo with: python3 apply_patch.py undo" % os.path.relpath(backup, ROOT))
    return 0


def _restore(backup_dir, rels):
    for rel in rels:
        src = os.path.join(backup_dir, rel)
        dst = os.path.join(ROOT, rel)
        with open(src, 'rb') as f:
            data = f.read()
        atomic_write(dst, data)


def cmd_apply(path, dry_run, name_check):
    cand = [path, os.path.join(ROOT, path)]
    for c in cand:
        if os.path.isfile(c):
            with open(c, 'rb') as f:
                text = f.read().decode('utf-8')
            return apply_patch_text(text, os.path.basename(c), dry_run, name_check)
    print("Patch file not found: %s" % path)
    return 1


def cmd_undo(stamp):
    all_s = stamps()
    if not all_s:
        print("No backups found.")
        return 1
    stamp = stamp or all_s[-1]
    d = os.path.join(BACKUP_DIR, stamp)
    if stamp not in all_s:
        print("No such backup: %s   (see: python3 apply_patch.py list)" % stamp)
        return 1
    note, rels = read_manifest(d)
    if not rels:
        print("Backup %s has no files." % stamp)
        return 1
    missing = [r for r in rels if not os.path.isfile(os.path.join(d, r)) or not os.path.isfile(os.path.join(ROOT, r))]
    if missing:
        print("Cannot undo -- missing: %s" % ', '.join(missing))
        return 1
    safety = make_backup([os.path.join(ROOT, r) for r in rels], 'pre-undo of %s' % stamp)
    _restore(d, rels)
    _log('UNDO %s (safety copy %s): %s' % (stamp, os.path.basename(safety), ', '.join(rels)))
    try:
        write_index()
    except Exception:
        pass
    print("Restored %d file(s) from %s (%s):" % (len(rels), stamp, note))
    for r in rels:
        print("  " + r)
    print("The undone state was saved as %s (undo again to redo)." % os.path.basename(safety))
    return 0


def cmd_list():
    all_s = stamps()
    if not all_s:
        print("No backups yet.")
        return 0
    for s in reversed(all_s):
        note, rels = read_manifest(os.path.join(BACKUP_DIR, s))
        print("%-18s %-34s %s" % (s, note[:34], ', '.join(rels)))
    return 0


def cmd_check():
    """Report undefined names and unresolved local imports across the whole project."""
    problems = 0
    for fp in sorted(iter_py(ROOT)):
        try:
            text, _ = read_text(fp)
            compile(text, fp, 'exec', dont_inherit=True)
        except (PatchError, SyntaxError, ValueError, OSError) as e:
            print("%s: cannot analyse (%s)" % (os.path.relpath(fp, ROOT), e))
            continue
        bad = undefined_names(text, fp)
        if bad is None:
            print("%s: skipped (star import)" % os.path.relpath(fp, ROOT))
        elif bad:
            problems += len(bad)
            print("%s: undefined name(s): %s" % (os.path.relpath(fp, ROOT), ', '.join(sorted(bad))))
    for line in sorted(Project(ROOT).import_problems()):
        problems += 1
        print(line)
    print("\n%s" % ("No problems found." if not problems else "%d problem(s) found." % problems))
    return 1 if problems else 0


# ----------------------------------------------------------------------- selftest

def selftest():
    global ROOT, BACKUP_DIR
    saved = (ROOT, BACKUP_DIR)
    tmp = os.path.realpath(tempfile.mkdtemp(prefix='apply_patch_selftest_'))
    ROOT, BACKUP_DIR = tmp, os.path.join(tmp, '.patch_backups')
    results = []

    def w(rel, text, crlf=False):
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as f:
            f.write((text.replace('\n', '\r\n') if crlf else text).encode('utf-8'))

    def r(rel):
        with open(os.path.join(tmp, rel), 'rb') as f:
            return f.read().decode('utf-8')

    def run(patch, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = apply_patch_text(patch, 'test.patch', **kw)
        return rc, buf.getvalue()

    def reset():
        shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        w('main_mod.py', "from helper import add, mul\nfrom pkg import util\n\n"
                         "def run(x):\n    return add(x, 1) + mul(x, 2)\n\n"
                         "def other():\n    return 1  \n\n"
                         "def twin_a():\n    y = 5\n    return y\n\n"
                         "def twin_b():\n    y = 5\n    return y\n")
        w('helper.py', "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        w('pkg/__init__.py', "from .util import helper_fn\n")
        w('pkg/util.py', "def helper_fn():\n    return 'h'\n")
        w('crlf.py', "def f():\n    return 1\n", crlf=True)

    def check(name, cond, detail=''):
        results.append((name, bool(cond), detail))

    try:
        reset()
        P = "@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 10) + mul(x, 2)\n@@@ END\n"
        rc, out = run(P)
        check('apply exact replace', rc == 0 and 'add(x, 10)' in r('main_mod.py'), out)
        check('backup created', len(stamps()) == 1)
        with redirect_stdout(io.StringIO()):
            rc2 = cmd_undo(None)
        check('undo restores original', rc2 == 0 and 'add(x, 1) +' in r('main_mod.py') and 'add(x, 10)' not in r('main_mod.py'))
        check('undo is itself undoable', len(stamps()) == 2)

        reset()
        rc, _ = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 99)\n@@@ WITH\n    return 0\n@@@ END\n")
        check('no match fails and changes nothing', rc == 1 and 'add(x, 99)' not in r('main_mod.py') and not stamps())

        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    y = 5\n    return y\n@@@ WITH\n    return 5\n@@@ END\n")
        check('ambiguous match fails', rc == 1 and 'matches 2 places' in out)

        rc, _ = run("@@@ FILE main_mod.py\n@@@ REPLACE\ndef other():\n    return 1\n@@@ WITH\ndef other():\n    return 2\n@@@ END\n")
        check('trailing-whitespace tolerance', rc == 0 and 'return 2' in r('main_mod.py'))

        reset()
        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 1 +\n@@@ END\n")
        check('syntax error blocked', rc == 1 and 'syntax error' in out and 'add(x, 1) + mul' in r('main_mod.py'))

        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 1) + mul(x, 2) + missing_name\n@@@ END\n")
        check('new undefined name blocked', rc == 1 and 'missing_name' in out)

        rc, _ = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 1) + mul(x, 2) + missing_name\n@@@ END\n", name_check=False)
        check('--no-name-check bypass works', rc == 0)

        reset()
        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\nfrom helper import add, mul\n@@@ WITH\nfrom helper import add, mul, nothing_here\n@@@ END\n")
        check('import of missing local name blocked', rc == 1 and 'nothing_here' in out)

        rc, out = run("@@@ FILE helper.py\n@@@ REPLACE\ndef mul(a, b):\n    return a * b\n@@@ WITH\n@@@ END\n")
        check('removing a still-imported function blocked', rc == 1 and 'mul' in out)

        rc, out = run("@@@ FILE pkg/__init__.py\n@@@ REPLACE\nfrom .util import helper_fn\n@@@ WITH\nfrom .util import helper_fn, ghost\n@@@ END\n")
        check('relative import of missing name blocked', rc == 1 and 'ghost' in out)

        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 3) + mul(x, 2)\n@@@ END\n"
                      "@@@ FILE helper.py\n@@@ REPLACE\n    return a + zzz\n@@@ WITH\n    return a + b\n@@@ END\n")
        check('multi-file is all-or-nothing', rc == 1 and 'add(x, 3)' not in r('main_mod.py'))

        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\ndef run(x):\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\nX\n@@@ END\n"
                      "@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\nY\n@@@ END\n")
        check('overlapping blocks blocked', rc == 1 and 'overlap' in out)

        rc, _ = run("@@@ FILE crlf.py\n@@@ REPLACE\n    return 1\n@@@ WITH\n    return 2\n@@@ END\n")
        check('CRLF preserved', rc == 0 and r('crlf.py') == "def f():\r\n    return 2\r\n")

        for bad in ('../evil.py', '/etc/passwd', 'C:/x.py'):
            rc, _ = run("@@@ FILE %s\n@@@ REPLACE\nx\n@@@ WITH\ny\n@@@ END\n" % bad)
            check('path rejected: %s' % bad, rc == 1)

        rc, out = run("@@@ FILE nope.py\n@@@ REPLACE\nx\n@@@ WITH\ny\n@@@ END\n")
        check('missing file rejected (never creates)', rc == 1 and 'does not exist' in out)

        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\nfoo\n@@@ WITH\nbar\n")
        check('malformed patch rejected', rc == 1 and 'unterminated' in out)

        reset()
        before = r('main_mod.py')
        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 7) + mul(x, 2)\n@@@ END\n", dry_run=True)
        check('dry run writes nothing', rc == 0 and r('main_mod.py') == before and not stamps())

        rc, out = run("@@@ FILE main_mod.py\n@@@ REPLACE\n    return add(x, 1) + mul(x, 2)\n@@@ WITH\n    return add(x, 1) + mul(x, 2)\n@@@ END\n")
        check('no-op patch reports nothing to do', rc == 0 and 'Nothing to do' in out)

        w('pkg2/__init__.py', "def hook():\n    return 1\n\nfrom .a import fa\n")
        w('pkg2/a.py', "from . import hook\n\ndef fa():\n    return hook()\n")
        rc, out = run("@@@ FILE pkg2/a.py\n@@@ REPLACE\n    return hook()\n@@@ WITH\n    return hook() + 1\n@@@ END\n")
        check('package layout (from . import hook) passes', rc == 0, out)
        rc, out = run("@@@ FILE pkg2/a.py\n@@@ REPLACE\nfrom . import hook\n@@@ WITH\nfrom . import hook, no_such_hook\n@@@ END\n")
        check('package: missing name in __init__ blocked', rc == 1 and 'no_such_hook' in out)
        with redirect_stdout(io.StringIO()):
            crc = cmd_check()
        check('check command clean on a clean tree', crc == 0)
        w('broken.py', "def f():\n    return undefined_thing\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            crc = cmd_check()
        check('check command flags an undefined name', crc == 1 and 'undefined_thing' in buf.getvalue())
        os.remove(os.path.join(tmp, 'broken.py'))
        idx = write_index()
        check('index written', os.path.isfile(idx) and 'def run' in open(idx).read())
    finally:
        ROOT, BACKUP_DIR = saved
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [x for x in results if not x[1]]
    for name, ok, detail in results:
        print("%s  %s" % ('PASS' if ok else 'FAIL', name))
        if not ok and detail:
            print("      " + detail.replace('\n', '\n      '))
    print("\n%d/%d passed" % (len(results) - len(bad), len(results)))
    return 1 if bad else 0


# --------------------------------------------------------------------------- main

def main(argv):
    if len(argv) < 2 or argv[1] in ('-h', '--help', 'help'):
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd == 'undo':
        return cmd_undo(argv[2] if len(argv) > 2 else None)
    if cmd == 'list':
        return cmd_list()
    if cmd == 'index':
        print(write_index())
        return 0
    if cmd == 'check':
        return cmd_check()
    if cmd == 'selftest':
        return selftest()
    flags = set(a for a in argv[2:] if a.startswith('--'))
    unknown = flags - {'--dry-run', '--no-name-check'}
    if unknown:
        print("Unknown option(s): %s" % ', '.join(sorted(unknown)))
        return 1
    return cmd_apply(cmd, '--dry-run' in flags, '--no-name-check' not in flags)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
