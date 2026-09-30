#!/usr/bin/env python3
r"""Format the LaTeX sources in sections/, in three passes:

  1. replace \[ ... \] by the equation* environment and \( ... \) by $ ... $;
  2. put every inline formula $...$ longer than 20 characters on its own line;
  3. break lines longer than 80 characters.

Lines are only broken at existing whitespace (never after a control space
"\ "), so the typeset output does not change. Pass 2 moves the whitespace-free
word holding the formula, e.g. "($f$)," together with a following " ." or
" ,". Pass 3 breaks at the last whitespace after punctuation that leaves a
first line of at least 40 characters, else at the last whitespace that fits;
a comment continues on the next line as a comment. A line that cannot be
broken is left as it is, with a warning.

Coexistence with tools/gutentag.py, which reads the sources line by line:
equation* is one of its INLINE environments, so pass 1 creates no units; lines
that it reads as one piece (headings and \begin lines with their arguments)
are never broken; and no break starts a line with a
heading or a \begin of a tagged environment. So formatting never changes the
tagged units. CI runs --write before gutentag.py --write on main
(.github/workflows/gutentag.yml).

Without options, print what would change; with --write, rewrite the files.
"""

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gutentag import BEGIN, HEADING, INLINE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES = 'sections'
WIDTH = 80
FORMULA = 20
PUNCT = '.,;:!?'
MATH_ENVS = INLINE | {'equation*'}
CS = re.compile(r'\\([A-Za-z@]+|.)', re.S)
ENV = re.compile(r'\\(begin|end)\{([^}]*)\}')


def split_comment(line):
    """The index of the unescaped % that starts the comment, or len(line)."""
    i = 0
    while i < len(line):
        if line[i] == '\\':
            i += 2
        elif line[i] == '%':
            return i
        else:
            i += 1
    return len(line)


def indent_of(line):
    return re.match(r'[ \t]*', line)[0]


def safe_start(s):
    """Whether a line may start with s without gutentag.py reading a new unit."""
    m = BEGIN.match(s)
    return not HEADING.match(s) and not (m and m[1] not in INLINE)


def frozen(line):
    """Lines gutentag.py reads as one piece."""
    return bool(HEADING.match(line) or BEGIN.match(line))


# Pass 1 ---------------------------------------------------------------------

def delimiters(lines):
    out = []
    for line in lines:
        c = split_comment(line)
        code, comment, ind = line[:c], line[c:], indent_of(line)
        pieces, cur, i = [], '', 0
        while i < len(code):
            if code[i] != '\\':
                cur += code[i]
                i += 1
                continue
            m = CS.match(code, i)
            if not m:
                cur += code[i:]
                break
            name, i = m[1], m.end()
            if name in '()':
                cur += '$'
            elif name in '[]':
                env = '\\begin{equation*}' if name == '[' else '\\end{equation*}'
                if cur.strip():
                    pieces.append(cur.rstrip())
                    cur = ind
                cur += env
                rest = code[i:].lstrip()
                if rest and safe_start(rest):
                    pieces.append(cur)
                    cur, i = ind, len(code) - len(rest)
            else:
                cur += m[0]
        if cur.strip() or not pieces:
            pieces.append(cur)
        pieces[-1] += comment
        out += pieces
    return out


# Pass 2 ---------------------------------------------------------------------

def formulas(code, state):
    """The inline formulas (start, end, begun on an earlier line?) closed on
    this line; state holds whether a formula is open and the environments."""
    found, math, envs = [], state[0], state[1]
    start, depth, i = (0 if math else None), 0, 0
    while i < len(code):
        ch = code[i]
        if ch == '\\':
            m = ENV.match(code, i)
            if m and not math:
                if m[1] == 'begin':
                    envs.append(m[2])
                elif envs and envs[-1] == m[2]:
                    envs.pop()
                i = m.end()
            else:
                i += 2
            continue
        in_display = any(e in MATH_ENVS for e in envs)
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        elif ch == '$' and not in_display:
            if code.startswith('$$', i):
                i += 2
                continue
            if not math:
                math, start, depth = True, i, 0
            elif depth <= 0:
                math = False
                found.append((start, i + 1, state[0] and start == 0))
                start = None
        i += 1
    state[0] = math
    return found


def own_lines(lines):
    out, state = [], [False, []]
    for line in lines:
        c = split_comment(line)
        code = line[:c]
        spans = formulas(code, state)
        inside = [False] * len(code)
        for s, e, _ in spans:
            inside[s:e] = [True] * (e - s)
        cuts = set()
        for s, e, cont in spans:
            if cont or e - s - 2 <= FORMULA:
                continue
            a = s
            while a > 0 and not (code[a - 1].isspace() and not inside[a - 1]):
                a -= 1
            b = e
            while b < len(code) and not (code[b].isspace() and not inside[b]):
                b += 1
            m = re.match(r'[ \t]+[' + re.escape(PUNCT) + r'](?=\s|$)', code[b:])
            if m:
                b += m.end()
            while a > 0 and code[a - 1].isspace():
                a -= 1
            if code[:a].strip():
                cuts.add(a)
            if code[b:].strip():
                cuts.add(b)
        cuts = [k for k in sorted(cuts) if safe_start(code[k:].lstrip())]
        if frozen(line) or not cuts:
            out.append(line)
            continue
        ind, bounds = indent_of(line), [0] + cuts + [len(line)]
        out += [line[:cuts[0]].rstrip()] + [
            ind + line[x:y].lstrip() for x, y in zip(bounds[1:], bounds[2:])]
        out[-1] = out[-1] if c < len(line) else out[-1].rstrip()
        for k in range(len(out) - len(cuts) - 1, len(out) - 1):
            out[k] = out[k].rstrip()
    return out


# Pass 3 ---------------------------------------------------------------------

def break_line(line):
    """The line broken into lines of at most WIDTH characters, and whether
    that failed."""
    out = []
    while len(line) > WIDTH:
        c, ind = split_comment(line), indent_of(line)
        marker = re.match(r'%+[ \t]*', line[c:])[0] if c < len(line) else ''
        best = punct = None
        for i in range(len(ind) + 1, min(len(line), WIDTH + 1)):
            if not line[i].isspace() or line[i - 1].isspace():
                continue
            k = i - 1
            while k >= 0 and line[k] == '\\':
                k -= 1
            if (i - 1 - k) % 2 or c < i <= c + len(marker):
                continue
            first, rest = line[:i].rstrip(), line[i:].lstrip()
            if not rest or rest[0] in PUNCT or (i < c and not safe_start(rest)):
                continue
            best = i
            if first[-1] in PUNCT and len(first) >= WIDTH // 2:
                punct = i
        i = punct or best
        if i is None:
            break
        out.append(line[:i].rstrip())
        rest = line[i:].lstrip()
        line = ind + (marker.rstrip() + ' ' if i > c else '') + rest
    out.append(line)
    return out, len(line) > WIDTH


def wrap(lines, warn):
    out = []
    for line in lines:
        if len(line) > WIDTH and frozen(line):
            warn(len(out) + 1, 'line not broken, since gutentag.py reads it as one piece')
            out.append(line)
            continue
        broken, failed = break_line(line)
        out += broken
        if failed:
            warn(len(out), f'no whitespace to break this line at within {WIDTH} characters')
    return out


def format_file(text, warn):
    lines = text.split('\n')
    return '\n'.join(wrap(own_lines(delimiters(lines)), warn))


def main():
    ap = argparse.ArgumentParser(description='Format the LaTeX sources.')
    ap.add_argument('--write', action='store_true', help='rewrite the files')
    ap.add_argument('paths', nargs='*', help=f'files or directories (default {SOURCES}/)')
    args = ap.parse_args()
    gha = os.environ.get('GITHUB_ACTIONS') == 'true'
    files = []
    for p in args.paths or [ROOT / SOURCES]:
        p = Path(p)
        files += sorted(p.rglob('*.tex')) if p.is_dir() else [p]
    changed = 0
    for path in files:
        def warn(n, msg):
            print(f'::warning file={path},line={n}::{msg}' if gha
                  else f'warning: {path}:{n}: {msg}')
        text = path.read_text()
        new = format_file(text, warn)
        if new != text:
            changed += 1
            if args.write:
                path.write_text(new)
            else:
                print(f'would reformat {path}')
    print(f'{"reformatted" if args.write else "would reformat"} {changed} of {len(files)} files')
    return 0


if __name__ == '__main__':
    sys.exit(main())
