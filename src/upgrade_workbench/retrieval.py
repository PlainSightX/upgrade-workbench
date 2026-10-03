"""从版本绑定的完整原文检索，不导入目标代码，不把排序分数当作正确性。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import tokenize
from contextlib import closing
from importlib.metadata import version

from markdown_it import MarkdownIt

from .cases.manifest import CaseValidationError, LoadedCase, read_verified_file
from .evidence import load_version_evidence

METHOD = "sqlite_fts5_bm25_parent_child_v1"
MAX_DOCUMENT_BYTES = 2_000_000
MAX_QUERY_BYTES = 1024
PUBLIC_FAILURE_QUERY_POLICY = "current_public_exceptions_v1"
# 词法路由只影响证据选择，不建立类型推断；no_ast 使用同一组查询。
SYMBOL_TOPICS = {
    "BaseModel": ("BaseModel", "base-model"),
    "Field": ("Field", "field"),
    "Config": ("config", "config"),
    "ConfigDict": ("config", "config"),
    "validator": ("validators", "validators"),
    "root_validator": ("root_validator", "validators"),
    "field_validator": ("validators", "validators"),
    "Optional": ("optional nullable required", "required-nullable-fields"),
    "Any": ("optional nullable required", "required-nullable-fields"),
    "None": ("optional nullable required", "required-nullable-fields"),
    "BaseSettings": ("BaseSettings", "basesettings"),
    "GenericModel": ("GenericModel", "import-moves"),
    "Protocol": ("Protocol", "import-moves"),
    "ValidationError": ("ValidationError", "import-moves"),
    "Color": ("Color", "import-moves"),
    "PaymentCardNumber": ("PaymentCardNumber", "import-moves"),
    "PaymentCardBrand": ("PaymentCardBrand", "import-moves"),
    "dataclass": ("dataclasses", "dataclasses"),
    "__get_validators__": ("__get_validators__", "custom-types"),
    "__modify_schema__": ("__modify_schema__", "custom-types"),
    "parse_obj_as": ("parse_obj_as TypeAdapter", None),
    "update_forward_refs": ("update_forward_refs model_rebuild", None),
    "from_orm": ("from_orm from_attributes", "base-model"),
    "model_validate": ("model_validate", "base-model"),
}
DERIVED_HEADINGS = {
    "pydantic-v2-dataclasses": "Changes to dataclasses",
    "pydantic-v2-custom-types": "Defining custom types",
}
SQLALCHEMY_SYMBOL_TOPICS = {
    "connect": ("explicit connection close context manager transaction scope", "connectionless"),
    "select": ("select positional expressions", "select"),
    "execute": ("Engine execute transaction commit rollback", "connectionless"),
    "MetaData": ("bound metadata", "connectionless"),
    "create_engine": ("autocommit commit rollback engine begin", "autocommit"),
    "text": ("execute text SQL", "execute"),
    "Session": ("Session autocommit autobegin", "session-autocommit"),
    "sessionmaker": ("Session autocommit autobegin", "session-autocommit"),
    "Query": ("Query legacy supported", "query"),
    "query": ("Query legacy supported", "query"),
}
SQLALCHEMY_EXECUTE_ANCHORS = (
    "sqlalchemy-v2-execute",
    "sqlalchemy-v2-execute-raw-versus-text",
)
WERKZEUG_SYMBOL_TOPICS = {
    "get_json": ("get_json content type application json BadRequest", "request-json"),
    "is_json": ("get_json content type application json", "request-json"),
    "request": ("request json content type", None),
}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _terms(query: str) -> list[str]:
    if not isinstance(query, str) or len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("Evidence query must be text within 1024 UTF-8 bytes")
    terms = list(dict.fromkeys(re.findall(r"\w+", query, flags=re.UNICODE)))
    if len(terms) > 64:
        raise ValueError("Evidence query exceeds 64 lexical terms")
    return terms


class EvidenceIndex:
    """索引可丢弃；原文、版本和行号是权威。每次从核验字节重建，避免缓存漂移。"""

    def __init__(self, case: LoadedCase) -> None:
        self.bundle = load_version_evidence(case)
        self.method = METHOD if self.bundle["binding"]["package"] == "pydantic" else "sqlite_fts5_bm25_rst_sections_v1"
        self.parents: list[dict] = []
        self.children: list[dict] = []
        self.anchors = {item["evidence_key"]: item for item in self.bundle["entries"]}
        sources = {item["path"]: item["sha256"] for item in self.bundle["entries"]}
        if len(sources) > 16:
            raise CaseValidationError("Version corpus exceeds 16 registered documents")
        for path, sha in sorted(sources.items()):
            raw = read_verified_file(case.root, path, sha)
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise CaseValidationError("Version document exceeds its byte limit")
            self._add_document(path, sha, raw.decode("utf-8"))
        identity = {"method": self.method, "bundle": self.bundle["bundle_sha256"],
                                 "sources": sources, "child_lines": 32,
                                 "tokenizer": "porter unicode61", "weights": [6, 1],
                                 "sqlite_version": sqlite3.sqlite_version,
                                 "markdown_it_version": version("markdown-it-py")}
        if self.bundle["binding"]["package"] in {"sqlalchemy", "werkzeug"}:
            identity.update(child_lines=None, layout="complete_rst_sections", docutils_version=version("docutils"))
        self.identity = _digest(identity)

    def _add_document(self, path: str, sha: str, text: str) -> None:
        if path.endswith(".rst"):
            self._add_rst_document(path, sha, text)
            return
        lines = text.splitlines(keepends=True)
        tokens = MarkdownIt("commonmark").parse(text)
        headings = []
        for index, token in enumerate(tokens):
            if token.type == "heading_open" and token.level == 0 and token.map:
                headings.append((token.map[0], int(token.tag[1]), tokens[index + 1].content))
        if not headings or headings[0][0] != 0:
            headings.insert(0, (0, 0, "Document introduction"))
        stack: list[tuple[int, str]] = []
        for index, (start, level, title) in enumerate(headings):
            end = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
            if start == end:
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            base = {"path": path, "sha256": sha, "heading": title,
                    "heading_path": [item[1] for item in stack]}
            parent = self._entry(base, lines, start, end)
            self.parents.append(parent)
            # block token 自带源行号，代码围栏内的 # 不是标题；大代码块不从中间拆开。
            ends = sorted({token.map[1] for token in tokens
                           if token.level == 0 and token.map
                           and start <= token.map[0] < token.map[1] <= end} | {end})
            child_start = start
            previous_end = start
            for block_end in ends:
                if block_end - child_start > 32 and previous_end > child_start:
                    self.children.append({**self._entry(base, lines, child_start, previous_end),
                                          "parent": parent})
                    child_start = previous_end
                previous_end = block_end
            if child_start < end:
                self.children.append({**self._entry(base, lines, child_start, end), "parent": parent})
            # 新规则可由完整原文恢复同名节，不改历史 bundle 或伪造登记范围。
            for key, expected in DERIVED_HEADINGS.items():
                if key not in self.anchors and title == expected:
                    subtree_end = next((other_start for other_start, other_level, _ in headings[index + 1:]
                                        if other_level <= level), len(lines))
                    self.anchors[key] = {**self._entry(base, lines, start, subtree_end),
                                         "evidence_key": key, "origin": "derived_verified_heading"}

    def _add_rst_document(self, path: str, sha: str, text: str) -> None:
        """成熟 RST 解析器只读识别章节；禁用文件插入，原始行号和字节不转换。"""
        from docutils import nodes
        from docutils.core import publish_doctree

        tree = publish_doctree(text, settings_overrides={
            "file_insertion_enabled": False, "raw_enabled": False, "doctitle_xform": False,
            "report_level": 5, "halt_level": 6, "warning_stream": io.StringIO(),
        })
        lines = text.splitlines(keepends=True)
        headings = []
        for title in tree.findall(nodes.title):
            if not isinstance(title.parent, nodes.section) or title.line is None:
                continue
            ancestors, parent = [], title.parent
            while isinstance(parent, nodes.section):
                ancestors.append(parent[0].astext())
                parent = parent.parent
            # docutils 的 title.line 指向下划线末行；rawsource 给出标题文本行数。
            start = title.line - 1 - len(title.rawsource.splitlines())
            if start < 0 or "".join(lines[start:title.line - 1]).strip() != title.rawsource.strip():
                raise CaseValidationError("RST heading location cannot be reconciled with source")
            headings.append((start, title.astext(), list(reversed(ancestors))))
        if not headings or headings[0][0] != 0:
            headings.insert(0, (0, "Document introduction", ["Document introduction"]))
        for index, (start, title, ancestry) in enumerate(headings):
            end = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
            if start >= end:
                continue
            base = {"path": path, "sha256": sha, "heading": title, "heading_path": ancestry}
            parent = self._entry(base, lines, start, end)
            self.parents.append(parent)
            # RST 首版按完整章节作检索块，避免切断 Sphinx 指令内的多行代码。
            self.children.append({**parent, "parent": parent})

    def _entry(self, base: dict, lines: list[str], start: int, end: int) -> dict:
        binding = self.bundle["binding"]
        identity = _digest({"path": base["path"], "sha256": base["sha256"],
                            "start": start + 1, "end": end})[:24]
        url = f'{binding["repository"]}/blob/{binding["revision"]}/{binding["upstream_path"]}'
        return {**base, "evidence_key": f"retrieved-{identity}",
                "start_line": start + 1, "end_line": end,
                "excerpt": "".join(lines[start:end]), "url": f"{url}#L{start + 1}-L{end}"}

    def rank_children(self, query: str) -> list[dict]:
        """同一子块空间供 BM25 与 dense 融合；父节去重必须留到排序之后。"""
        terms = _terms(query)
        ranked = []
        if terms:
            # 只使用 FTS5 的成熟排名实现；词项加引号且参数绑定，查询不是 SQL/FTS 程序。
            with closing(sqlite3.connect(":memory:")) as connection:
                try:
                    connection.execute("CREATE VIRTUAL TABLE chunks USING fts5(title, body, tokenize='porter unicode61')")
                except sqlite3.OperationalError as error:
                    raise RuntimeError("SQLite FTS5 is required; no silent retrieval fallback") from error
                connection.executemany("INSERT INTO chunks(rowid, title, body) VALUES (?, ?, ?)",
                    [(index + 1, " / ".join(child["heading_path"]), child["excerpt"])
                     for index, child in enumerate(self.children)])
                # 下划线随 unicode61 分词；整个标识符作为短语匹配，避免任意一个子词误命中。
                match = " OR ".join('"' + term + '"' for term in terms)
                rows = connection.execute(
                    "SELECT rowid, bm25(chunks, 6.0, 1.0) AS score FROM chunks "
                    "WHERE chunks MATCH ? ORDER BY score, rowid", (match,),
                ).fetchall()
                for rowid, score in rows:
                    child = self.children[rowid - 1]
                    ranked.append({"key": child["evidence_key"], "score": score})
        return ranked

    def search(self, query: str, *, top_k: int = 5) -> dict:
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k must be an integer between 1 and 20")
        terms = _terms(query)
        hits, seen = [], set()
        children = {child["evidence_key"]: child for child in self.children}
        for row in self.rank_children(query):
            child = children[row["key"]]
            parent = child["parent"]
            if parent["evidence_key"] in seen:
                continue
            seen.add(parent["evidence_key"])
            hits.append({"rank": len(hits) + 1, "bm25_score": row["score"],
                         "child": {key: val for key, val in child.items() if key != "parent"},
                         "parent": parent})
            if len(hits) == top_k:
                break
        return {"schema_version": 1, "method": self.method, "corpus_sha256": self.identity,
                "case_fingerprint": self.bundle["case_fingerprint"],
                "binding": self.bundle["binding"], "query": query, "terms": terms,
                "status": "matched" if hits else "no_match", "hits": hits,
                "counts": {"parents": len(self.parents), "children": len(self.children)},
                "scope": "lexical_relevance_not_migration_acceptance"}


def retrieve_evidence(case: LoadedCase, query: str, *, top_k: int = 5,
                      semantic_config: dict | None = None) -> dict:
    index = EvidenceIndex(case)
    if semantic_config is not None:
        from .semantic import configured_search

        return configured_search(index, query, top_k=top_k, config=semantic_config)
    return index.search(query, top_k=top_k)


def public_failure_queries(runtime: dict | None, revision: str) -> list[dict]:
    """只从已核验的当前公开异常构造检索线索；模型假说不能成为事实输入。"""
    if runtime is None:
        return []
    selected: dict[str, dict] = {}
    for observation in reversed(runtime.get("observations", [])):
        if (observation.get("kind") != "public_checks"
                or observation.get("revision") != revision
                or observation.get("stale") is not False):
            continue
        result = observation.get("result", {})
        if result.get("scope") != "public":
            continue
        observation_id = observation.get("id")
        if not isinstance(observation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", observation_id):
            raise ValueError("Public failure query requires a bound observation ID")
        # 同次报告的直接升级失败仍是有效参照，不能混称为当前候选反例。
        for stage_name in ("new_candidate", "new_original"):
            stage = result.get("stages", {}).get(stage_name, {})
            if stage.get("status") != "failed":
                continue
            for evidence in stage.get("critical_evidence", []):
                if evidence.get("kind") != "exception":
                    continue
                text = evidence.get("text")
                if (not isinstance(text, str)
                        or hashlib.sha256(text.encode("utf-8")).hexdigest() != evidence.get("sha256")):
                    raise ValueError("Public failure exception differs from its recorded hash")
                # 原始异常仅作查询数据；限制词数和字节，不执行其中的任何指令。
                terms = list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z_0-9]{0,63}", text)))[:64]
                while terms and len(" ".join(terms).encode("utf-8")) > MAX_QUERY_BYTES:
                    terms.pop()
                # 缺失属性/导入名是更精确的检索词，避免通用报错措辞淹没API标识符。
                named = re.findall(r"\b(?:attribute|name|member|method)\s+['\"]([A-Za-z_][\w.]{0,127})['\"]", text)
                for query in list(dict.fromkeys(named))[:2] + [" ".join(terms)]:
                    if not query or (query not in selected and len(selected) >= 4):
                        continue
                    row = selected.setdefault(query, {"query": query, "sources": []})
                    source = {"observation_id": observation_id, "revision": revision,
                              "stage": stage_name, "exception_sha256": evidence["sha256"]}
                    if source not in row["sources"]:
                        row["sources"].append(source)
    return list(selected.values())


def evidence_for_sources(case: LoadedCase, blocks: list[dict], *, max_bytes: int,
                          semantic_config: dict | None = None,
                          failure_queries: list[dict] | None = None) -> dict:
    """词法符号锚点保底、BM25 补充；不使用 AST finding、测试或最终评分构造查询。"""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Evidence byte capacity must be positive")
    index = EvidenceIndex(case)
    family = index.bundle["binding"]["package"]
    topics = {"sqlalchemy": SQLALCHEMY_SYMBOL_TOPICS, "pydantic": SYMBOL_TOPICS,
              "werkzeug": WERKZEUG_SYMBOL_TOPICS}[family]
    symbols: set[str] = set()
    lexical_unknowns = []
    for block in blocks:
        if not block["path"].endswith(".py"):
            continue
        try:
            for token in tokenize.generate_tokens(io.StringIO(block["text"]).readline):
                if token.type == tokenize.NAME and token.string in topics:
                    symbols.add(token.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            lexical_unknowns.append(block["path"])
    queries = sorted({topics[symbol][0] for symbol in symbols})
    prefix = "werkzeug-v21-" if family == "werkzeug" else family + "-v2-"
    keys = sorted({prefix + topics[symbol][1] for symbol in symbols
                   if topics[symbol][1] is not None})
    if family == "sqlalchemy" and "execute" in symbols:
        # execute 的连接生命周期与 SQL 文本解析分属不同迁移依据。
        keys = sorted(set(keys) | {key for key in SQLALCHEMY_EXECUTE_ANCHORS if key in index.anchors})
    # 引号内的前向注解不产生 NAME token；模型源码必须保留必填/可空语义的保底依据。
    if family == "pydantic" and ("BaseModel" in symbols or "BaseSettings" in symbols):
        keys = sorted(set(keys) | {"pydantic-v2-required-nullable-fields"})
    selected = [index.anchors[key] for key in keys if key in index.anchors]
    missing = [key for key in keys if key not in index.anchors]
    size = sum(len(item["excerpt"].encode("utf-8")) for item in selected)
    if size > max_bytes:
        raise ValueError("Required symbol evidence exceeds capacity; refusing silent truncation")
    failure_queries = failure_queries or []
    # 必需锚点保留；当前故障优先于宽泛主题，占用同一容量而不是扩大请求。
    search_queries = list(dict.fromkeys([row["query"] for row in failure_queries] + queries))
    if semantic_config is None:
        searches = [index.search(query, top_k=2) for query in search_queries]
    else:
        from .semantic import configured_search

        searches = [configured_search(index, query, top_k=2, config=semantic_config) for query in search_queries]
    omitted = []
    for search in searches:
        for hit in search["hits"]:
            parent = hit["parent"]
            # 父节过大时仅附命中子块；仍可由父行号恢复整节，且不截断块内文本。
            entry = parent if len(parent["excerpt"].encode("utf-8")) <= 8000 else hit["child"]
            if any(entry["path"] == item["path"] and entry["start_line"] <= item["end_line"]
                   and item["start_line"] <= entry["end_line"] for item in selected):
                continue
            entry_size = len(entry["excerpt"].encode("utf-8"))
            if size + entry_size > max_bytes:
                omitted.append({"key": entry["evidence_key"], "reason": "capacity"})
                continue
            selected.append(entry)
            size += entry_size
    report = {"method": index.method if semantic_config is None else "version_bound_semantic_selection_v1",
            "semantic_config": semantic_config,
            "corpus_sha256": index.identity, "binding": index.bundle["binding"],
            "source_hashes": {block["path"]: block["sha256"] for block in blocks},
            "query_source": "public_python_lexical_symbols_no_test_or_ast_findings",
            "symbols": sorted(symbols), "searches": searches, "entries": selected,
            "missing_symbol_anchors": missing, "lexical_unknowns": lexical_unknowns,
            "omitted": omitted, "evidence_bytes": size, "max_bytes": max_bytes}
    if failure_queries:
        report["public_failure_queries"] = failure_queries
        report["failure_query_policy"] = PUBLIC_FAILURE_QUERY_POLICY
        report["query_source"] = "public_python_symbols_and_verified_public_exceptions"
    return report
