#!/usr/bin/env python3
r"""Maintain the permanent \gutentag tags of the LaTeX sources in sections/.

The tagged units are top-level environments and top-level paragraphs.
Headings (\section to \subsubsection) are not tagged, since LaTeX would place
their tags on the next line. Displayed math and diagrams (INLINE) belong to
the surrounding paragraph; lines holding a comment or a single command
(\label, \input, \todo, ...), possibly spanning lines while an argument
is open, belong to no unit. A unit's tag is a line
\gutentag{XXXX}% with four uppercase hex digits, placed
  - inside an environment, after the \begin line and its \label lines,
  - before the first line of a paragraph.
Environments must begin and end in the same file.

Tags are assigned in increasing order from 0001, and the file maxtag holds the
largest tag assigned so far (0000 before the first). Without options, report problems and exit 1 on errors,
including tags above maxtag, which only the script may assign. With --write,
also tag the untagged units in reading order and add every tag up to maxtag
that left the sources to removed.tags, as "TAG HASH" with HASH the last commit
on the first-parent history of HEAD containing it. CI runs --write on main
(.github/workflows/gutentag.yml).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = 'sections'
REMOVED = ROOT / 'removed.tags'
MAXTAG = ROOT / 'maxtag'
INLINE = {'equation', 'equation*', 'align', 'align*', 'alignat', 'alignat*',
          'gather', 'gather*', 'multline', 'multline*', 'flalign', 'flalign*',
          'displaymath', 'math', 'tikzcd', 'tikzpicture', 'tabular', 'array'}

TAG = re.compile(r'\\gutentag\{([^}]*)\}')
VALID = re.compile(r'[0-9A-F]{4}')
TAG_LINE = re.compile(r'\s*\\gutentag\{([0-9A-F]{4})\}%?\s*$')
HEADING = re.compile(r'\s*\\(part|chapter|section|subsection|subsubsection)'
                     r'\*?\s*[\[{]')
BEGIN = re.compile(r'\s*\\begin\{([^}]*)\}')
ENV = re.compile(r'\\(begin|end)\{')
LABEL = re.compile(r'\s*\\label\{[^}]*\}\s*(%.*)?$')
COMMENT = re.compile(r'(?<!\\)%.*')
COMMAND = re.compile(r'\\([A-Za-z@]+)\*?')
INPUT = re.compile(r'\\input\{([^}]*)\}')
DIFF_HEADER = re.compile(r'(\+\+\+|---) (a/|b/|/dev/null)')


def depth_change(line):
    return sum(1 if m[1] == 'begin' else -1
               for m in ENV.finditer(COMMENT.sub('', line)))


def skip_group(s, i):
    """Index after the {...} or [...] group opening at s[i], or -1."""
    close, depth, j = '}' if s[i] == '{' else ']', 0, i
    while j < len(s):
        c = s[j]
        if c == '\\':
            j += 2
            continue
        depth += (c == '{') - (c == '}')
        if depth < 0:
            return -1
        if depth == 0 and c == close:
            return j + 1
        j += 1
    return -1


def is_command(line):
    """Whether the line holds only a comment or a single command with arguments."""
    s = COMMENT.sub('', line).strip()
    m = COMMAND.match(s)
    if not m:
        return not s
    if m[1] in ('begin', 'end', 'item'):
        return False
    i = m.end()
    while i < len(s):
        if s[i].isspace():
            i += 1
        elif s[i] in '{[' and (i := skip_group(s, i)) > 0:
            continue
        else:
            return False
    return True


def command_end(lines, i):
    """The last line of the single command (with arguments) starting at line i,
    which may span lines while a group is open, or None."""
    text, depth = '', 0
    for j in range(i, min(len(lines), i + 50)):
        if j > i and not lines[j].strip():
            return None
        code = COMMENT.sub('', lines[j])
        text += code + '\n'
        depth += len(re.findall(r'(?<!\\)[{\[]', code)) - len(re.findall(r'(?<!\\)[}\]]', code))
        if is_command(text):
            return j
        if depth <= 0:
            return None
    return None


def parse(rel, lines, errors):
    """The units of a file as (tag or None, slot for a new tag line), and the
    indices of the tag lines they claim."""
    units, claimed = [], set()

    def claim(k):
        m = 0 <= k < len(lines) and k not in claimed and TAG_LINE.match(lines[k])
        if m:
            claimed.add(k)
            return m[1]
        return None

    def after_labels(k):
        while k < len(lines) and LABEL.match(lines[k]):
            k += 1
        return k

    def end_of_env(i):
        d, j = depth_change(lines[i]), i
        while d > 0 and j + 1 < len(lines):
            j += 1
            d += depth_change(lines[j])
        if d != 0:
            errors.append(f'{rel}:{i + 1}: unbalanced \\begin/\\end')
        return j

    i, para = 0, False
    while i < len(lines):
        line = lines[i]
        m = BEGIN.match(line)
        if not line.strip():
            para = False
        elif TAG_LINE.match(line):
            pass
        elif HEADING.match(line):
            para = False
        elif m and m[1] not in INLINE:
            para = False
            j = end_of_env(i)
            if j == i:
                units.append((claim(i - 1), i))
            else:
                k = after_labels(i + 1)
                units.append((claim(k) or claim(i - 1), k))
            i = j + 1
            continue
        elif (j := command_end(lines, i)) is not None:
            i = j + 1
            continue
        elif not para:
            para = True
            units.append((claim(i - 1), i))
        if depth_change(line) < 0:
            errors.append(f'{rel}:{i + 1}: \\end without \\begin')
        elif depth_change(line) > 0:
            i = end_of_env(i)
        i += 1
    return units, claimed


def reading_order():
    """The source files in the order main.tex inputs them, then the rest."""
    order = []

    def visit(path):
        if path not in order and path.exists():
            order.append(path)
            for m in INPUT.finditer(COMMENT.sub('', path.read_text())):
                visit(ROOT / (m[1] if m[1].endswith('.tex') else m[1] + '.tex'))

    visit(ROOT / 'main.tex')
    files = sorted((ROOT / SOURCES).rglob('*.tex'))
    return [p for p in order if p in files] + [p for p in files if p not in order]


def last_commits(tags):
    """For each of the tags removed on the first-parent history of HEAD, the
    last commit containing it."""
    log = subprocess.run(
        ['git', 'log', '--first-parent', '--diff-merges=first-parent', '-U0',
         '--format=%x00%H %P', 'HEAD', '--', SOURCES],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout
    last = {}
    for entry in log.split('\0')[1:]:
        header, _, diff = entry.partition('\n')
        parents = header.split()[1:]
        added, dropped = set(), set()
        for line in diff.split('\n'):
            if DIFF_HEADER.match(line):
                continue
            if line.startswith('+'):
                added.update(TAG.findall(line))
            elif line.startswith('-'):
                dropped.update(TAG.findall(line))
        for t in (dropped - added) & tags:
            if parents:
                last.setdefault(t, parents[0])
    return last


def main():
    ap = argparse.ArgumentParser(description='Check or assign \\gutentag tags.')
    ap.add_argument('--write', action='store_true',
                    help='tag untagged units and update removed.tags and maxtag')
    args = ap.parse_args()

    top = int(MAXTAG.read_text().strip() or '0', 16) if MAXTAG.exists() else 0
    errors, warnings, files, where = [], [], {}, {}
    for path in reading_order():
        rel = path.relative_to(ROOT)
        lines = path.read_text().split('\n')
        units, claimed = parse(rel, lines, errors)
        files[path] = (lines, [slot for tag, slot in units if tag is None])
        for n, line in enumerate(lines):
            for t in TAG.findall(line):
                if not VALID.fullmatch(t):
                    errors.append(f'{rel}:{n + 1}: malformed tag {t!r}')
                    continue
                where.setdefault(t, []).append(f'{rel}:{n + 1}')
                if not 0 < int(t, 16) <= top:
                    errors.append(f'{rel}:{n + 1}: tag {t} is not in 0001..maxtag, '
                                  'so tools/gutentag.py did not assign it')
                if n not in claimed:
                    warnings.append(f'{rel}:{n + 1}: tag {t} belongs to no unit')
    errors += [f'tag {t} occurs at {", ".join(locs)}'
               for t, locs in where.items() if len(locs) > 1]
    untagged = sum(len(slots) for _, slots in files.values())
    for w in warnings:
        print('warning:', w)
    for e in errors:
        print('error:', e)
    if errors:
        return 1
    if not args.write:
        print(f'{len(where)} tags, {untagged} untagged units')
        return 0

    old = REMOVED.read_text().split('\n') if REMOVED.exists() else []
    old = [l for l in old if l.strip()]
    listed = {l.split()[0] for l in old}
    gone = {f'{x:04X}' for x in range(1, top + 1)} - set(where) - listed
    last = last_commits(gone) if gone else {}
    for t in sorted(gone - set(last)):
        print(f'warning: tag {t} is gone but not in the history of HEAD')
    new = [f'{t} {last[t]}' for t in sorted(last)]
    keep = [l for l in old if l.split()[0] not in where]
    REMOVED.write_text(''.join(l + '\n' for l in keep + new))

    if top + untagged > 0xFFFF:
        print('error: out of tags')
        return 1
    fresh = iter(f'{x:04X}' for x in range(top + 1, top + 1 + untagged))
    for path, (lines, slots) in files.items():
        if not slots:
            continue
        out = []
        for n in range(len(lines) + 1):
            indent = re.match(r'\s*', lines[n])[0] if n < len(lines) else ''
            out += [f'{indent}\\gutentag{{{next(fresh)}}}%' for s in slots if s == n]
            if n < len(lines):
                out.append(lines[n])
        path.write_text('\n'.join(out))
    if untagged:
        MAXTAG.write_text(f'{top + untagged:04X}\n')
    print(f'assigned {untagged} tags, recorded {len(new)} removed tags')
    return 0


if __name__ == '__main__':
    sys.exit(main())
