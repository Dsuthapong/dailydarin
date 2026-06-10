#!/usr/bin/env python3
"""
sync-from-obsidian.py — Publish a curated subset of an Obsidian vault to Quartz.

Selects notes tagged #permanent (inline or frontmatter, incl. hierarchical
variants like #permanent/hub), excludes anything tagged #personal as a safety
guard for the public site, sanitizes Obsidian syntax, copies referenced images,
and syncs the result into Quartz's content/ directory.

Zero dependencies — runs on stock Python 3 (no pip install needed).

Usage:
    python3 sync-from-obsidian.py            # perform the sync
    python3 sync-from-obsidian.py --dry-run  # report only, write nothing
"""

import json
import os
import re
import shutil
import sys

# ---------------------------------------------------------------------------
# Config — edit these to taste
# ---------------------------------------------------------------------------
VAULT = "/Users/darinsuthapong/Darin Synced Vault"
CONTENT = "/Users/darinsuthapong/Desktop/AI World/quartz/content"

INCLUDE_TAGS = ["permanent"]   # publish if note has any of these (or "<tag>/...")
EXCLUDE_TAGS = ["personal"]    # never publish if note has any of these (safety guard)

# Directory names / relative paths to skip entirely while scanning the vault.
SKIP_DIRS = [".obsidian", ".trash", ".git", "[00] System/Templates"]

# Extensions treated as embeddable attachments (copied alongside notes).
ATTACHMENT_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf"}

MANIFEST_NAME = ".quartz-export-manifest.json"

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
INLINE_TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_/-]*)")
# Embed: ![[target|alias]]  /  Link: [[target#heading|alias]]
EMBED_RE = re.compile(r"!\[\[([^\]]+)\]\]")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


# ---------------------------------------------------------------------------
# Lightweight frontmatter parsing (no PyYAML)
# ---------------------------------------------------------------------------
def split_frontmatter(text):
    """Return (frontmatter_dict_subset, body). Only extracts what we need:
    'tags', 'title', 'aliases'. Missing frontmatter -> ({}, text)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end():]
    return parse_simple_yaml(raw), body


def parse_simple_yaml(raw):
    """Tiny YAML-ish parser sufficient for tags/title/aliases. Handles:
       key: value
       key: [a, b]
       key:
         - a
         - b
    """
    data = {}
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        # Top-level "key: ..." (no leading indentation)
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val == "":
            # Collect following indented list items.
            items = []
            j = i + 1
            while j < len(lines):
                lm = re.match(r"^\s+-\s+(.*)$", lines[j])
                if lm:
                    items.append(strip_quotes(lm.group(1).strip()))
                    j += 1
                elif lines[j].strip() == "":
                    j += 1
                else:
                    break
            data[key] = items
            i = j
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            data[key] = [strip_quotes(x.strip()) for x in inner.split(",") if x.strip()]
            i += 1
        else:
            data[key] = strip_quotes(val)
            i += 1
    return data


def strip_quotes(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        # comma- or space-separated string of tags
        parts = re.split(r"[,\s]+", v.strip())
        return [p for p in parts if p]
    return []


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------
def strip_code(text):
    text = FENCED_CODE_RE.sub("", text)
    text = INLINE_CODE_RE.sub("", text)
    return text


def collect_tags(fm, body):
    """Return a set of normalized tags from frontmatter + inline body tags."""
    tags = set()
    for t in as_list(fm.get("tags")):
        tags.add(t.lstrip("#").strip().lower())
    for t in INLINE_TAG_RE.findall(strip_code(body)):
        tags.add(t.strip().lower())
    return {t for t in tags if t}


def matches(tag_set, wanted):
    """True if any tag equals a wanted tag or is a hierarchical child of it."""
    for tag in tag_set:
        for w in wanted:
            if tag == w or tag.startswith(w + "/"):
                return True
    return False


def link_target_base(raw):
    """Given the inside of [[...]] or ![[...]], return the bare note/file name
    (before # heading and | alias), and the display alias if present."""
    target = raw.split("|", 1)
    name = target[0]
    alias = target[1] if len(target) > 1 else None
    name = name.split("#", 1)[0].strip()
    return name, alias


def basename_no_ext(path_or_name):
    base = os.path.basename(path_or_name)
    stem, _ = os.path.splitext(base)
    return stem


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def should_skip_dir(rel_dir):
    norm = rel_dir.replace(os.sep, "/")
    for sk in SKIP_DIRS:
        if norm == sk or norm.startswith(sk + "/") or os.path.basename(norm) == sk:
            return True
    return False


def walk_markdown(root):
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        if rel and should_skip_dir(rel):
            dirnames[:] = []
            continue
        # prune skip dirs from descent
        dirnames[:] = [
            d for d in dirnames
            if not should_skip_dir(os.path.join(rel, d) if rel else d)
        ]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                yield os.path.join(dirpath, fn)


def index_attachments(root):
    """Map lowercased basename -> absolute path for every file in the vault,
    so we can resolve image embeds regardless of folder."""
    index = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        if rel and should_skip_dir(rel):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if not should_skip_dir(os.path.join(rel, d) if rel else d)
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in ATTACHMENT_EXTS:
                index.setdefault(fn.lower(), os.path.join(dirpath, fn))
    return index


def main():
    dry_run = "--dry-run" in sys.argv

    stats = {
        "scanned": 0,
        "published": 0,
        "skipped_no_permanent": 0,
        "excluded_personal": 0,
        "attachments_copied": 0,
        "links_stripped": 0,
        "embeds_stripped": 0,
        "collisions": 0,
    }

    print(f"Vault:   {VAULT}")
    print(f"Content: {CONTENT}")
    print(f"Mode:    {'DRY RUN (no writes)' if dry_run else 'SYNC'}\n")

    # --- Pass 1: scan & select -------------------------------------------
    notes = []  # list of dicts: {path, fm, body, tags, aliases}
    published_names = set()  # lowercased basenames + aliases that go public

    for path in walk_markdown(VAULT):
        stats["scanned"] += 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError) as e:
            print(f"  ! skip unreadable: {path} ({e})")
            continue

        fm, body = split_frontmatter(text)
        tags = collect_tags(fm, body)

        if not matches(tags, INCLUDE_TAGS):
            stats["skipped_no_permanent"] += 1
            continue
        if matches(tags, EXCLUDE_TAGS):
            stats["excluded_personal"] += 1
            continue

        aliases = [a.lower() for a in as_list(fm.get("aliases"))]
        notes.append({
            "path": path, "fm": fm, "body": body,
            "tags": tags, "aliases": aliases,
        })
        published_names.add(basename_no_ext(path).lower())
        for a in aliases:
            published_names.add(a)

    stats["published"] = len(notes)

    # --- Pass 0: clean previous export (manifest-based) ------------------
    manifest_path = os.path.join(CONTENT, MANIFEST_NAME)
    if not dry_run:
        os.makedirs(CONTENT, exist_ok=True)
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    old = json.load(f)
                for rel in old.get("files", []):
                    fp = os.path.join(CONTENT, rel)
                    if os.path.isfile(fp):
                        os.remove(fp)
            except (json.JSONDecodeError, OSError) as e:
                print(f"  ! could not read old manifest, skipping clean: {e}")

    # --- Pass 2: transform & write ---------------------------------------
    attachments_index = index_attachments(VAULT)
    used_filenames = {}        # lowercased final name -> count (collision handling)
    written = []               # relative paths for the new manifest
    copied_attachments = set()

    for note in notes:
        out_name = unique_filename(basename_no_ext(note["path"]), used_filenames)
        new_body, n_links, n_embeds, atts = transform_body(
            note["body"], published_names, attachments_index
        )
        stats["links_stripped"] += n_links
        stats["embeds_stripped"] += n_embeds

        front = build_frontmatter(note["fm"], note["path"])
        out_text = front + new_body

        out_path = os.path.join(CONTENT, out_name)
        written.append(out_name)

        if not dry_run:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(out_text)

        # Copy referenced attachments (flattened).
        for att_abs, att_name in atts:
            if att_name.lower() in copied_attachments:
                continue
            copied_attachments.add(att_name.lower())
            written.append(att_name)
            stats["attachments_copied"] += 1
            if not dry_run:
                shutil.copy2(att_abs, os.path.join(CONTENT, att_name))

    # --- Write manifest ---------------------------------------------------
    if not dry_run:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"files": written}, f, indent=2)

    stats["collisions"] = getattr(unique_filename, "collisions", 0)

    # --- Report -----------------------------------------------------------
    print("\n" + "=" * 48)
    print("Summary")
    print("=" * 48)
    print(f"  Markdown files scanned : {stats['scanned']}")
    print(f"  Published              : {stats['published']}")
    print(f"  Skipped (no #permanent): {stats['skipped_no_permanent']}")
    print(f"  Excluded (#personal)   : {stats['excluded_personal']}")
    print(f"  Attachments copied     : {stats['attachments_copied']}")
    print(f"  Wikilinks stripped     : {stats['links_stripped']}")
    print(f"  Embeds stripped        : {stats['embeds_stripped']}")
    print(f"  Filename collisions    : {stats['collisions']}")
    if dry_run:
        print("\n  (dry run — nothing was written)")
    else:
        print(f"\n  Wrote {len(written)} files into {CONTENT}")
    return stats


def unique_filename(stem, used):
    """Produce a unique '<stem>.md', appending -2/-3 on collision."""
    base = f"{stem}.md"
    key = base.lower()
    if key not in used:
        used[key] = 1
        return base
    used[key] += 1
    n = used[key]
    print(f"  ! filename collision: '{base}' -> '{stem}-{n}.md'")
    # bump global collision counter via attribute on function
    unique_filename.collisions = getattr(unique_filename, "collisions", 0) + 1
    return f"{stem}-{n}.md"


def build_frontmatter(fm, path):
    """Rebuild a minimal, safe frontmatter block: title + non-excluded tags."""
    title = fm.get("title") or basename_no_ext(path)
    tags = [
        t.lstrip("#").strip()
        for t in as_list(fm.get("tags"))
        if not matches({t.lstrip("#").strip().lower()}, EXCLUDE_TAGS)
    ]
    lines = ["---", f"title: {yaml_scalar(title)}"]
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {yaml_scalar(t)}")
    lines.append("---\n")
    return "\n".join(lines)


def yaml_scalar(s):
    s = str(s)
    if re.search(r"[:#\[\]{}&*!|>'\"%@`]", s) or s.strip() != s:
        return '"' + s.replace('"', '\\"') + '"'
    return s


def transform_body(body, published_names, attachments_index):
    """Strip excluded inline tags, copy/keep image embeds, strip links and
    embeds to non-published notes. Returns (body, n_links_stripped,
    n_embeds_stripped, [(att_abs, att_name), ...])."""
    n_links = 0
    n_embeds = 0
    attachments = []

    # 1) Embeds first (they are a superset prefix of wikilinks).
    def embed_sub(m):
        nonlocal n_embeds
        raw = m.group(1)
        name, _alias = link_target_base(raw)
        ext = os.path.splitext(name)[1].lower()
        if ext in ATTACHMENT_EXTS:
            abs_path = attachments_index.get(os.path.basename(name).lower())
            if abs_path:
                attachments.append((abs_path, os.path.basename(name)))
                return m.group(0)  # keep embed; file will be copied
            # referenced image not found in vault -> strip
            n_embeds += 1
            return ""
        # Note embed (transclusion): keep only if target is published.
        if basename_no_ext(name).lower() in published_names:
            return m.group(0)
        n_embeds += 1
        return ""

    body = EMBED_RE.sub(embed_sub, body)

    # 2) Plain wikilinks.
    def link_sub(m):
        nonlocal n_links
        raw = m.group(1)
        name, _alias = link_target_base(raw)
        if basename_no_ext(name).lower() in published_names:
            return m.group(0)  # keep; Quartz resolves it
        n_links += 1
        return ""  # strip entirely

    body = WIKILINK_RE.sub(link_sub, body)

    # 3) Remove excluded inline tags (e.g. #personal, #personal/...) from body.
    def tag_sub(m):
        tag = m.group(1).lower()
        if matches({tag}, EXCLUDE_TAGS):
            # preserve the leading whitespace char that the regex consumed
            lead = m.group(0)[0] if m.group(0) and m.group(0)[0].isspace() else ""
            return lead
        return m.group(0)

    body = INLINE_TAG_RE.sub(tag_sub, body)

    return body, n_links, n_embeds, attachments


if __name__ == "__main__":
    unique_filename.collisions = 0
    s = main()
    # surface collisions captured during filename assignment
    s["collisions"] = getattr(unique_filename, "collisions", 0)
