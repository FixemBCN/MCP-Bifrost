# Original specification — the starting proposal (2026-08-23)

> Received as written. Left untouched, deliberately: it is the fixed
> reference the iterations in [`plan.md`](./plan.md) are measured against.
>
> Reading it next to what was actually built is the point. Several of its
> assumptions turned out to be wrong, and one of its central guarantees —
> the perimeter check — was found to be structurally incapable of failing.
> None of that is visible unless the original survives unedited.

# PROJECT SPECIFICATION: MCP AST-Driven Targeted Code Patcher

## 1. SYSTEM ARCHITECTURE & ROLES

- **Orchestrator (Claude Code CLI)**: High-level reasoning, code analysis, bug identification, test execution, final validation.
- **Context & Operations Engine (Python MCP Server)**: File IO, AST/lexer parsing, dependency mapping, range extraction, synthetic payload compression, atomic patching, structured disk logging.
- **Specialized Worker (DeepSeek API)**: Low-cost execution of isolated code modification tasks via compact, machine-optimized JSON inputs/outputs.
- **Documentation Extractor (Async CLI/Tool)**: Non-blocking parser that compiles local disk logs into human-readable documentation (CHANGELOG, PRs, release notes).

```
[Claude Code] ──(MCP Call)──> [MCP Server (Python)] ──(AST/Lexer)──> [Compressed Payload]
     │                                    │
[Claude Code] <──(Status OK)── [Atomic Patch] <──(Synthetic JSON)─── [DeepSeek API]
     │
     └──(Disk Audit Log)──────────> [deepseek_history.jsonl]
                                              │
                                              └──> [Async Doc Extractor]
```

## 2. AI-TO-AI PROTOCOL (SYNTHETIC SCHEMA)

All inter-agent communication uses dense, key-shortened JSON schemas
(`response_format={"type": "json_object"}`) to minimize token overhead.

### Payload Schema (Claude Code ➔ MCP ➔ DeepSeek)

```json
{
  "lang": "py|php|java",
  "sym": "target_symbol_or_range",
  "intent": "compact_micro_instruction",
  "ctx": ["dep_signature_1", "dep_signature_2"],
  "src": "raw_code_block"
}
```

### Response Schema (DeepSeek ➔ MCP ➔ Claude Code)

```json
{
  "out": "modified_raw_code_block",
  "why": "concise_technical_rationale",
  "diff_stat": "+L/-L"
}
```

## 3. FUNCTIONAL WORKFLOW

1. **Diagnosis**: Claude Code locates target file and defect (symbol identifier or line scope).
2. **MCP Tool Call**: Claude Code invokes `fix_symbol` or `fix_range`.
3. **Deterministic Parsing**:
   - Python MCP loads target file.
   - AST/Lexer extracts exact code block + top-level context headers (imports, namespaces, signatures).
4. **Synthetic Payload Generation**: MCP builds compressed AI-to-AI JSON payload.
5. **Worker Execution**:
   - DeepSeek API processes payload with static system prompt (leveraging DeepSeek Prompt Caching).
   - DeepSeek returns synthetic JSON containing modified snippet and technical rationale (`why`).
6. **Atomic Ingestion & Logging**:
   - MCP overwrites target line ranges in source file.
   - MCP appends execution record to `deepseek_history.jsonl` (0 context impact on Claude).
   - MCP returns 1-line status (`OK`) + `diff_stat` to Claude Code.
7. **Validation**: Claude Code executes local test suite/linter to verify change.

## 4. DUAL-STREAM LOGGING & HUMAN DOC EXTRACTION

Execution logs separate live machine-to-machine interactions from human
documentation needs to prevent context bloat during interactive coding
sessions.

### Audit Log Record (`deepseek_history.jsonl`)

```json
{
  "id": "uuid4",
  "timestamp": "ISO-8601",
  "file": "path/to/file.ext",
  "symbol": "target_symbol",
  "intent": "original_intent",
  "rationale": "why_field_from_deepseek",
  "before_hash": "sha256",
  "after_hash": "sha256",
  "token_metrics": {"in": 120, "out": 45, "saved_est": 1850}
}
```

### Human Extraction Pipeline

- **Runtime Cost**: 0 tokens in active Claude Code session.
- **Execution**: On-demand CLI command or MCP tool (`export_docs`) reads
  `deepseek_history.jsonl` and formats entries into Markdown release notes,
  CHANGELOG updates, or commit messages explaining What, How, and Why.

## 5. MULTI-LANGUAGE PARSER MATRIX

| Language | Primary Parsing Strategy | Fallback Strategy | Context Header Extraction |
|---|---|---|---|
| Python | Native `ast` module | Line-range extraction | Top-level imports & module docstring |
| PHP | `tree-sitter-php` / Regex block parser | Tokenizer (`token_get_all`) | Namespace & use statements |
| Java | `tree-sitter-java` / Method Regex | Braced-block matcher | Package declaration & import statements |

## 6. MCP TOOL SCHEMAS

### Tool 1: `fix_symbol`
- **Inputs**: `file_path` (string, required), `symbol_name` (string, required),
  `instruction` (string, required), `language` (string, optional; auto-detected
  if omitted).
- **Output**: String (`OK: Symbol updated` or `ERROR: Reason`).

### Tool 2: `fix_range`
- **Inputs**: `file_path` (string, required), `start_line` (integer, required),
  `end_line` (integer, required), `instruction` (string, required).
- **Output**: String (`OK: Lines X-Y updated`).

### Tool 3: `export_docs`
- **Inputs**: `output_path` (string, optional; default `CHANGELOG_GENERATED.md`),
  `since` (string, optional, ISO date filter).
- **Output**: String (`OK: Exported N logs to path`).

## 7. OPTIMIZATION BENCHMARKS (TARGETS)

- Token Reduction: 85% to 95% vs full-file re-writing.
- Context Overhead: 0 additional tokens added to active Claude Code history
  for raw LLM interactions.
- Diff Safety: 100% preservation of unedited file contents outside target
  scope.

---

*(The original also carried an `<ElicitationsGroup>` block offering two
next actions — generate the base MCP server, and write the documentation
extractor. Neither was taken: the request was to validate the project and
its blueprint first, before writing any code. That ordering is what
surfaced the byte-offset bug and the tautological gate before either could
reach production.)*
