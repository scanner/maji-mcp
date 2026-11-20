"""
The runtime libraries: those a script can import beyond the standard library, as
agents are told of them. (The skill library, where skills are kept, is another thing:
`broker/library.py`.)

The runtime image installs each one from `images/python-runtime/requirements.in`,
pinned in `requirements.txt` (`docs/design/runner.md`). This module says what each is
for and what to watch for: run_python's description names them, `read_api("python")`
lists them, and `read_api("python.<name>")` shows one's notes. The broker also uses the
list to tell which libraries a run used (`runlog.py`).

A test checks the list against both files, so a library added to the image, or a lock
that moves a version, needs its entry here too. That is the moment to read the
library's changes and correct its notes.
"""

# system imports
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeLibrary:
    """
    One library a script can import.

    Attributes:
        name: what a script imports, as in `import yaml`
        pins: each distribution the entry stands for, as `requirements.in` names it,
            with the version `requirements.txt` pins
        summary: one line saying what a script uses it for
        notes: the entry points to start from, and the traps
        modules: other top-level modules that belong to it, such as a grammar or a
            dependency it raises errors from; an error raised in one counts against
            this library
    """

    name: str
    pins: dict[str, str]
    summary: str
    notes: str
    modules: tuple[str, ...] = field(default=())

    def described(self) -> str:
        """The library's entry for `read_api("python.<name>")`."""
        pins = ", ".join(f"{dist} {version}" for dist, version in self.pins.items())
        return f"{self.name}: {pins}\n{self.summary}\n\n{self.notes}"


LIBRARIES = (
    RuntimeLibrary(
        "git",
        {"GitPython": "3.2.1"},
        "Read a mounted repository's commits, trees, blobs and refs.",
        "`git.Repo('/mnt/<name>')`. A commit's `stats` runs git once per commit, so "
        "for history across many commits use `maji.git_log`, which is one git call, "
        "and `maji.blame` for the commit behind a line. Mounts are read-only: "
        "anything that writes, such as a checkout or a fetch, fails.",
        ("gitdb", "smmap"),
    ),
    RuntimeLibrary(
        "yaml",
        {"PyYAML": "6.0.3"},
        "Read YAML: CI pipelines, compose files, Kubernetes manifests, OpenAPI "
        "documents.",
        "`yaml.safe_load(text)` reads one document, `yaml.safe_load_all(text)` a "
        "file of several; `yaml.load(text, Loader=yaml.CSafeLoader)` is the same, "
        "faster. Use a safe loader on files from a mount: `yaml.unsafe_load`, or "
        "`yaml.load` with `yaml.Loader`, builds any Python object a file names, so "
        "the file can run code in your script, with its stubs. For line numbers, "
        "`yaml.compose(text)` gives nodes, each with `start_mark.line`, from 0.",
    ),
    RuntimeLibrary(
        "jinja2",
        {"Jinja2": "3.1.6"},
        "Parse Jinja templates: the variables they use, and what they include or "
        "extend.",
        "`tree = jinja2.Environment().parse(source)`. Then, after `from jinja2 "
        "import meta, nodes`: `meta.find_undeclared_variables(tree)` names the "
        "variables a template needs, `meta.find_referenced_templates(tree)` the "
        "templates it extends, includes or imports, and "
        "`tree.find_all(nodes.Name)` walks the tree, each node with `lineno`. "
        "Parse a template from a mount; don't render it: rendering runs its "
        "expressions, which can reach Python objects and run code in your script. "
        "A filter the environment lacks is no matter to `parse`, but a tag from an "
        "extension it lacks, such as `{% trans %}`, raises TemplateSyntaxError.",
        ("markupsafe",),
    ),
    RuntimeLibrary(
        "markdown_it",
        {"markdown-it-py": "4.2.0"},
        "Parse Markdown: headings, links, code blocks and tables.",
        "`tokens = markdown_it.MarkdownIt('commonmark').enable('table')"
        ".parse(text)` gives a flat list of tokens. A block token's `map` is its "
        "lines, `[first, last)` from 0. An `inline` token's `children` hold the "
        "links: `link_open`, with `attrGet('href')`. For the nesting, `from "
        "markdown_it.tree import SyntaxTreeNode`, then `SyntaxTreeNode(tokens)`.",
        ("mdurl",),
    ),
    RuntimeLibrary(
        "pygments",
        {"Pygments": "2.21.0"},
        "Split source code into tokens, for about 500 languages, and tell a file's "
        "language from its name.",
        "`import pygments.lexers, pygments.token`. `lexer = "
        "pygments.lexers.get_lexer_for_filename(path)` raises "
        "`pygments.util.ClassNotFound` for a kind it doesn't know. "
        "`lexer.get_tokens(text)` yields `(token type, text)`; `kind in "
        "pygments.token.Comment` tells a comment, and so do `String`, `Name` and "
        "`Keyword` for theirs. It gives tokens, not a syntax tree: for structure use "
        "`ast` for Python, or `tree_sitter`.",
    ),
    RuntimeLibrary(
        "tree_sitter",
        {
            "tree-sitter": "0.26.0",
            "tree-sitter-javascript": "0.25.0",
            "tree-sitter-typescript": "0.23.2",
            "tree-sitter-go": "0.25.0",
            "tree-sitter-rust": "0.24.2",
            "tree-sitter-ruby": "0.23.1",
            "tree-sitter-perl": "2.0.3",
        },
        "Syntax trees for JavaScript, TypeScript and TSX, Go, Rust, Ruby and Perl 5. "
        "Python's own `ast` covers Python.",
        "Each grammar is a module: `tree_sitter_javascript`, `tree_sitter_typescript` "
        "(`language_typescript()` and `language_tsx()`), `tree_sitter_go`, "
        "`tree_sitter_rust`, `tree_sitter_ruby`, `tree_sitter_perl`. `lang = "
        "tree_sitter.Language(tree_sitter_go.language())`, then `tree = "
        "tree_sitter.Parser(lang).parse(source_bytes)`. A node has `type`, `text`, "
        "`start_point` (row and column, from 0), `children` and "
        "`child_by_field_name('name')`; `str(node)` prints its subtree. Node types "
        "differ by grammar, so parse a small sample and print it first. To find "
        "nodes: `query = tree_sitter.Query(lang, '(call_expression function: "
        "(identifier) @fn)')`, then `tree_sitter.QueryCursor(query)"
        ".captures(tree.root_node)` returns a dict from each capture's name to its "
        "nodes. The Perl grammar warns that an int argument is deprecated; the "
        "warning is harmless.",
        (
            "tree_sitter_javascript",
            "tree_sitter_typescript",
            "tree_sitter_go",
            "tree_sitter_rust",
            "tree_sitter_ruby",
            "tree_sitter_perl",
        ),
    ),
    RuntimeLibrary(
        "unidiff",
        {"unidiff": "1.0.1"},
        "Parse unified diffs, such as `git diff` prints, into files, hunks and "
        "numbered lines.",
        "`patch = unidiff.PatchSet(text)`, with the text from `maji.git(name, "
        "'diff', ...)`. Each file has `path`, `added`, `removed`, `is_added_file` "
        "and `is_removed_file`; each of its hunks is a list of lines, each with "
        "`is_added`, `is_removed`, `source_line_no` and `target_line_no`.",
    ),
    RuntimeLibrary(
        "pathspec",
        {"pathspec": "1.1.1"},
        "Match paths against .gitignore-style patterns, such as a CODEOWNERS file's.",
        "`spec = pathspec.GitIgnoreSpec.from_lines(lines)`, then "
        "`spec.match_file(path)`, or `spec.match_tree_files(root)` to walk a "
        "folder. In CODEOWNERS the last matching line wins, so make a spec for each "
        "line and keep the last that matches.",
    ),
    RuntimeLibrary(
        "rapidfuzz",
        {"RapidFuzz": "3.14.6"},
        "Fuzzy string matching: group near-identical messages, or match a name "
        "against a list.",
        "`from rapidfuzz import fuzz, process`. `process.extractOne(query, "
        "choices, scorer=fuzz.WRatio)` returns `(match, score, index)`, with scores "
        "from 0 to 100; `process.cdist(a, b)` scores every pair at once, as an "
        "array. Take out what varies, such as ids, numbers and addresses, before "
        "comparing messages.",
    ),
    RuntimeLibrary(
        "duckdb",
        {"duckdb": "2.0.0"},
        "SQL over CSV, JSON-lines and Parquet files, and over pandas DataFrames, "
        "without loading a file into memory first.",
        "`duckdb.sql(\"select k, count(*) from '/mnt/logs/*.csv' group by k\")"
        ".fetchall()`; `read_csv(...)` and `read_json(...)` in the query take "
        "options. `.df()` returns a pandas DataFrame, and a query can name a "
        "DataFrame variable as a table. The json, parquet and icu extensions are "
        "built in. One that has to be downloaded, such as sqlite_scanner, fails "
        "after a few seconds, as a script has no network: read a SQLite database "
        "with `maji.sqlite`, then query its rows as a DataFrame. A script has 512 "
        "MB of memory; duckdb takes up to 80% of it and spills to /work, which "
        "holds 64 MB.",
        ("_duckdb",),
    ),
    RuntimeLibrary(
        "pandas",
        {"pandas": "3.1.0"},
        "DataFrames: load, join, group and reshape tabular data.",
        "This is pandas 3: copy-on-write is always on, so chained assignment such "
        "as `df['a'][0] = 1` changes nothing (use `df.loc[0, 'a'] = 1`), and text "
        "columns have the `str` dtype, not `object`. numpy comes with it. "
        "`pd.read_csv`, `pd.read_json(path, lines=True)` and `pd.read_sql(query, "
        "maji.sqlite(name, path))` read files; for a file larger than memory, query "
        "it with duckdb instead. Importing pandas takes about 0.3 s. Print a "
        "summary, such as `df.groupby(...).size().nlargest(20)`, not the frame.",
        ("numpy", "dateutil", "_cyutility"),
    ),
)

BY_NAME = {library.name: library for library in LIBRARIES}

# Which library each top-level module belongs to: its own name, and its `modules`.
#
OWNER = {
    module: library.name
    for library in LIBRARIES
    for module in (library.name, *library.modules)
}


def search(query: str) -> list[dict[str, str]]:
    """
    The libraries whose name or summary has every word of the query, for
    search_api, each as `import <name>` with its summary.
    """
    words = query.lower().split()
    return [
        {
            "name": f"python.{library.name}",
            "signature": f"import {library.name}",
            "summary": library.summary,
        }
        for library in LIBRARIES
        if words
        and all(w in f"{library.name} {library.summary}".lower() for w in words)
    ]


def listing() -> str:
    """The libraries, for `read_api("python")`: one line each, then how to read more."""
    lines = [
        "Libraries a script can import beyond the standard library. "
        'read_api("python.<name>") shows one\'s entry points and what to watch for; '
        "`help(...)` on a function in a script shows the rest.",
        "",
    ]
    for library in LIBRARIES:
        dist, version = next(iter(library.pins.items()))
        lines.append(f"{library.name} ({dist} {version}): {library.summary}")
    return "\n".join(lines)
