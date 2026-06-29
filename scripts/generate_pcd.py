"""Convert the v1 Profilarr database (YAML in custom_formats/, profiles/,
regex_patterns/, media_management/) into the v2 "Profilarr Compliant Database"
(PCD) format: a pcd.json manifest plus operational SQL in ops/.

The v2 schema is defined by https://github.com/Dictionarry-Hub/schema . That
repo's ops (0.schema.sql, 1.languages.sql, 2.qualities.sql, ...) create the
tables and seed the `languages` and `qualities` lookup tables. This script only
emits the *data* rows that build on top of that schema, as a single base op.

Usage:
    python scripts/generate_pcd.py <repo_root> <output_dir>

Both default to the repository root, which means it converts this repo in place.
"""

import json
import os
import sys

import yaml

# ---------------------------------------------------------------------------
# Lookup mappings to the canonical names seeded by the schema repo.
# ---------------------------------------------------------------------------

# v1 language condition values (lowercase) -> schema `languages.name`.
LANGUAGE_NAMES = {
    "chinese": "Chinese",
    "english": "English",
    "french": "French",
    "german": "German",
    "japanese": "Japanese",
    "korean": "Korean",
    "original": "Original",
    "any": "Any",
}

# v1 profile-level language preference -> schema `languages.name`.
PROFILE_LANGUAGE_NAMES = {
    "any": "Any",
    "original": "Original",
    "french": "French",
}

# Sonarr exposes the two remux qualities under API names. Profiles store the
# API name, but the schema's `qualities` table uses the canonical name.
QUALITY_ALIASES = {
    "Bluray-1080p Remux": "Remux-1080p",
    "Bluray-2160p Remux": "Remux-2160p",
}

# v1 condition type -> (condition_* table, value-column, source key in YAML).
# Pattern types share condition_patterns and resolve the inline value (which is
# a regex *name*) against regular_expressions.name.
PATTERN_TYPES = {"release_title", "release_group", "edition"}
VALUE_CONDITIONS = {
    "source": ("condition_sources", "source", "source"),
    "resolution": ("condition_resolutions", "resolution", "resolution"),
    "quality_modifier": ("condition_quality_modifiers", "quality_modifier", "qualityModifier"),
    "release_type": ("condition_release_types", "release_type", "releaseType"),
    "indexer_flag": ("condition_indexer_flags", "flag", "flag"),
}


def q(value):
    """Quote a value as a SQL string literal (or NULL)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def arr_of(name, tags):
    """Determine the arr a named entity belongs to from its name prefix/tags."""
    n = (name or "").lower()
    if n.startswith("radarr"):
        return "radarr"
    if n.startswith("sonarr"):
        return "sonarr"
    tagset = {str(t).lower() for t in (tags or [])}
    if "radarr" in tagset and "sonarr" not in tagset:
        return "radarr"
    if "sonarr" in tagset and "radarr" not in tagset:
        return "sonarr"
    return "all"


def load_dir(path):
    """Yield (filename, parsed-yaml) for every .yml file in a directory."""
    for fn in sorted(os.listdir(path)):
        if fn.endswith((".yml", ".yaml")):
            with open(os.path.join(path, fn), encoding="utf-8") as fh:
                yield fn, yaml.safe_load(fh)


class SqlBuilder:
    """Accumulates rows per table and renders ordered INSERT statements."""

    def __init__(self):
        self.tables = {}  # table -> (columns tuple, list of value-tuples)
        self.tags = set()

    def add(self, table, columns, *values):
        cols, rows = self.tables.setdefault(table, (columns, []))
        assert cols == columns, f"column mismatch for {table}"
        rows.append(values)

    def render(self, table):
        columns, rows = self.tables[table]
        if not rows:
            return ""
        col_sql = ", ".join(columns)
        lines = [f"INSERT INTO {table} ({col_sql}) VALUES"]
        body = [f"  ({', '.join(q(v) for v in row)})" for row in rows]
        return lines[0] + "\n" + ",\n".join(body) + ";\n"


def build(root):
    b = SqlBuilder()

    # --- regular expressions ------------------------------------------------
    for _fn, d in load_dir(os.path.join(root, "regex_patterns")):
        b.add(
            "regular_expressions",
            ("name", "pattern", "description"),
            d["name"], d["pattern"], d.get("description") or None,
        )
        for t in d.get("tags") or []:
            b.tags.add(str(t))
            b.add("regular_expression_tags",
                  ("regular_expression_name", "tag_name"), d["name"], str(t))

    # --- custom formats + conditions ----------------------------------------
    for _fn, d in load_dir(os.path.join(root, "custom_formats")):
        cf_name = d["name"]
        cf_arr = arr_of(cf_name, d.get("tags"))
        b.add("custom_formats",
              ("name", "description", "include_in_rename"),
              cf_name, d.get("description") or None, 0)
        for t in d.get("tags") or []:
            b.tags.add(str(t))
            b.add("custom_format_tags",
                  ("custom_format_name", "tag_name"), cf_name, str(t))

        seen_conditions = set()
        for c in d.get("conditions") or []:
            cname = c["name"]
            if cname in seen_conditions:  # schema requires unique names per CF
                continue
            seen_conditions.add(cname)
            ctype = c["type"]
            b.add("custom_format_conditions",
                  ("custom_format_name", "name", "type", "arr_type", "negate", "required"),
                  cf_name, cname, ctype, cf_arr,
                  bool(c.get("negate")), bool(c.get("required")))

            if ctype in PATTERN_TYPES:
                b.add("condition_patterns",
                      ("custom_format_name", "condition_name", "regular_expression_name"),
                      cf_name, cname, c["pattern"])
            elif ctype == "language":
                lang = LANGUAGE_NAMES[str(c["language"]).lower()]
                b.add("condition_languages",
                      ("custom_format_name", "condition_name", "language_name"),
                      cf_name, cname, lang)
            elif ctype in VALUE_CONDITIONS:
                table, col, key = VALUE_CONDITIONS[ctype]
                b.add(table,
                      ("custom_format_name", "condition_name", col),
                      cf_name, cname, c[key])
            else:
                raise ValueError(f"Unhandled condition type {ctype!r} in {cf_name}")

    # --- quality profiles ---------------------------------------------------
    for _fn, d in load_dir(os.path.join(root, "profiles")):
        p_name = d["name"]
        p_arr = arr_of(p_name, d.get("tags"))
        b.add("quality_profiles",
              ("name", "description", "upgrades_allowed", "minimum_custom_format_score",
               "upgrade_until_score", "upgrade_score_increment"),
              p_name, d.get("description") or None,
              bool(d.get("upgradesAllowed", True)),
              int(d.get("minCustomFormatScore", 0)),
              int(d.get("upgradeUntilScore", 0)),
              max(1, int(d.get("minScoreIncrement", 1))))

        for t in d.get("tags") or []:
            b.tags.add(str(t))
            b.add("quality_profile_tags",
                  ("quality_profile_name", "tag_name"), p_name, str(t))

        lang = PROFILE_LANGUAGE_NAMES.get(str(d.get("language", "any")).lower())
        if lang:
            b.add("quality_profile_languages",
                  ("quality_profile_name", "language_name", "type"),
                  p_name, lang, "simple")

        # CF scores
        for cf in d.get("custom_formats") or []:
            b.add("quality_profile_custom_formats",
                  ("quality_profile_name", "custom_format_name", "arr_type", "score"),
                  p_name, cf["name"], p_arr, int(cf["score"]))

        # Quality list: each item is either a single quality or a group.
        upgrade_until_name = (d.get("upgrade_until") or {}).get("name")
        for position, item in enumerate(d.get("qualities") or []):
            members = item.get("qualities")
            if members:  # quality group
                group = item["name"]
                b.add("quality_groups",
                      ("quality_profile_name", "name"), p_name, group)
                for m_pos, member in enumerate(members):
                    qn = QUALITY_ALIASES.get(member["name"], member["name"])
                    b.add("quality_group_members",
                          ("quality_profile_name", "quality_group_name", "quality_name", "position"),
                          p_name, group, qn, m_pos)
                b.add("quality_profile_qualities",
                      ("quality_profile_name", "quality_name", "quality_group_name",
                       "position", "enabled", "upgrade_until"),
                      p_name, None, group, position, 1,
                      1 if group == upgrade_until_name else 0)
            else:  # single quality
                qn = QUALITY_ALIASES.get(item["name"], item["name"])
                b.add("quality_profile_qualities",
                      ("quality_profile_name", "quality_name", "quality_group_name",
                       "position", "enabled", "upgrade_until"),
                      p_name, qn, None, position, 1,
                      1 if item["name"] == upgrade_until_name else 0)

    # --- media management ---------------------------------------------------
    build_media_management(root, b)

    # --- tags (must be inserted first; rendered first below) ----------------
    for tag in sorted(b.tags):
        b.add("tags", ("name",), tag)

    return b


def build_media_management(root, b):
    mm_dir = os.path.join(root, "media_management")

    naming_path = os.path.join(mm_dir, "naming.yml")
    if os.path.exists(naming_path):
        naming = yaml.safe_load(open(naming_path, encoding="utf-8"))
        r = naming.get("radarr")
        if r:
            b.add("radarr_naming",
                  ("name", "rename", "movie_format", "movie_folder_format",
                   "replace_illegal_characters", "colon_replacement_format"),
                  "Default", bool(r.get("rename", True)), r["movieFormat"],
                  r["movieFolderFormat"], bool(r.get("replaceIllegalCharacters")),
                  r.get("colonReplacementFormat", "smart"))
        s = naming.get("sonarr")
        if s:
            b.add("sonarr_naming",
                  ("name", "rename", "standard_episode_format", "daily_episode_format",
                   "anime_episode_format", "series_folder_format", "season_folder_format",
                   "replace_illegal_characters", "colon_replacement_format",
                   "custom_colon_replacement_format", "multi_episode_style"),
                  "Default", bool(s.get("rename", True)), s["standardEpisodeFormat"],
                  s["dailyEpisodeFormat"], s["animeEpisodeFormat"], s["seriesFolderFormat"],
                  s["seasonFolderFormat"], bool(s.get("replaceIllegalCharacters")),
                  int(s.get("colonReplacementFormat", 4)),
                  s.get("customColonReplacementFormat") or None,
                  int(s.get("multiEpisodeStyle", 5)))

    misc_path = os.path.join(mm_dir, "misc.yml")
    if os.path.exists(misc_path):
        misc = yaml.safe_load(open(misc_path, encoding="utf-8"))
        for arr, table in (("radarr", "radarr_media_settings"), ("sonarr", "sonarr_media_settings")):
            m = misc.get(arr)
            if m:
                b.add(table,
                      ("name", "propers_repacks", "enable_media_info"),
                      "Default", m.get("propersRepacks", "doNotPrefer"),
                      bool(m.get("enableMediaInfo", True)))

    qd_path = os.path.join(mm_dir, "quality_definitions.yml")
    if os.path.exists(qd_path):
        qd = yaml.safe_load(open(qd_path, encoding="utf-8")).get("qualityDefinitions", {})
        for arr, table in (("radarr", "radarr_quality_definitions"), ("sonarr", "sonarr_quality_definitions")):
            for quality_name, sizes in (qd.get(arr) or {}).items():
                quality_name = QUALITY_ALIASES.get(quality_name, quality_name)
                b.add(table,
                      ("name", "quality_name", "min_size", "max_size", "preferred_size"),
                      "Default", quality_name, sizes.get("min", 0),
                      sizes["max"], sizes["preferred"])


# Order matters: parents before children so FKs resolve on replay.
RENDER_ORDER = [
    "tags",
    "regular_expressions",
    "custom_formats",
    "quality_profiles",
    "custom_format_conditions",
    "condition_patterns", "condition_languages", "condition_sources",
    "condition_resolutions", "condition_quality_modifiers",
    "condition_release_types", "condition_indexer_flags",
    "quality_groups",
    "quality_group_members",
    "quality_profile_qualities",
    "quality_profile_custom_formats",
    "quality_profile_languages",
    "regular_expression_tags", "custom_format_tags", "quality_profile_tags",
    "radarr_naming", "sonarr_naming",
    "radarr_media_settings", "sonarr_media_settings",
    "radarr_quality_definitions", "sonarr_quality_definitions",
]


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    out = sys.argv[2] if len(sys.argv) > 2 else root

    b = build(root)

    ops_dir = os.path.join(out, "ops")
    os.makedirs(ops_dir, exist_ok=True)

    sections = []
    for table in RENDER_ORDER:
        if table in b.tables:
            rendered = b.render(table)
            if rendered:
                sections.append(f"-- {table}\n{rendered}")
    # Any table we forgot to order explicitly.
    for table in b.tables:
        if table not in RENDER_ORDER:
            raise ValueError(f"Table {table!r} not in RENDER_ORDER")

    header = ("-- Base content for the TRaSH-Guides PCD.\n"
              "-- Generated by scripts/generate_pcd.py from the v1 YAML database.\n"
              "-- Builds on the schema + lookup data provided by the `schema` dependency.\n\n")
    with open(os.path.join(ops_dir, "1.base.sql"), "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(sections))

    manifest = {
        "name": "trash-guides",
        "version": "2.0.0",
        "description": "Unofficial TRaSH-Guides database for Profilarr (v2 PCD format)",
        "arr_types": ["radarr", "sonarr"],
        "dependencies": {"schema": "^1.1.0"},
        "license": "MIT",
        "repository": "https://github.com/Sc00tz/profilarr-trash-guides",
        "tags": ["trash-guides", "radarr", "sonarr", "quality", "remux"],
        "profilarr": {"minimum_version": "2.0.0"},
    }
    with open(os.path.join(out, "pcd.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=4)
        fh.write("\n")

    os.makedirs(os.path.join(out, "tweaks"), exist_ok=True)

    counts = {t: len(rows) for t, (_c, rows) in b.tables.items()}
    print("Wrote pcd.json and ops/1.base.sql")
    for t in RENDER_ORDER:
        if counts.get(t):
            print(f"  {t}: {counts[t]}")


if __name__ == "__main__":
    main()
