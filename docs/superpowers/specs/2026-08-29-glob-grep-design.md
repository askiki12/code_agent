# 迭代设计：新增 glob / grep 搜索工具

> 日期：2026-08-29 ｜ 状态：已批准 ｜ 关联 ADR：ADR-011（实现时追加）
> 本 spec 是开发过程文档，随实现计划落地，最终以 `docs/tools.md` 为权威 schema 源。

## 1. 背景与目标

当前 agent 只有 5 个工具（read_file / write_file / edit_file / list_dir / run_command），
缺少搜索能力：无法按文件名模式查找，也无法在多个文件中检索内容（找定义、找调用点）。
这显著限制真实编程任务的完成率（定位问题常需"在哪个文件里出现了某符号"）。

**目标**：新增 `glob` 与 `grep` 两个本地搜索工具，纯标准库自实现，零新依赖，
与现有工具风格、返回格式、安全策略完全一致。提升真实任务完成率。

## 2. 范围

**In scope**
- 新工具 `glob`：按通配符（含 `**` 递归）查找文件。
- 新工具 `grep`：在文件中做正则搜索，三种输出模式（content / files_with_matches / count）。
- 基础 gitignore 支持（grep 遍历时跳过被忽略文件）。
- 安全：受保护路径跳过、二进制文件跳过、symlink 不跟随、结果数与行内容截断。
- 测试（`test_tools.py` 新增约 15 用例）、文档同步（tools.md / design.md / architecture.md）、ADR-011。
- 真实 API 冒烟一次。

**Out of scope（本期不做）**
- 基于模型语义的搜索、索引缓存。
- 复杂 gitignore 语法（`**` 模式、转义规则完整集）。
- 并行工具调用。
- 语义搜索 / embeddings。

## 3. 工具定义（权威 schema 将落于 `docs/tools.md`）

### 3.1 glob

```
参数:
  pattern (string, 必填)  glob 模式, 支持 * ? [...] 与 ** 递归
  path    (string, 可选, 默认 workdir)  搜索起点目录
返回:
  匹配的【文件】路径列表（相对 workdir）, 排序确定性
行为:
  基于 glob.glob(os.path.join(path, pattern), recursive=True)
  pattern 一律视为相对 path 的相对模式（若为绝对路径则以其为根, 等价于 path="." 场景）
  只返回文件（过滤目录）；跳过受保护路径（.git / .env* 除 .env.example）
限制:
  结果上限 MAX_SEARCH_RESULTS=500, 超出附加截断标记并 truncated=true
```

### 3.2 grep

```
参数:
  pattern      (string, 必填)  正则表达式
  path         (string, 可选, 默认 workdir)  目录或单文件
  include      (string, 可选)  fnmatch 过滤文件名, 如 "*.py"
  ignore_case  (boolean, 可选, 默认 false)
  output_mode  (string, 可选, 默认 "content")
               枚举: content | files_with_matches | count
返回: 依 output_mode
  content           → "相对workdir路径:行号:行内容" 多行
  files_with_matches → 每行一个相对 workdir 路径
  count             → "相对workdir路径:命中数"
无匹配: "(no matches)"
限制:
  MAX_SEARCH_RESULTS=500 条结果, 超出截断并 truncated=true
  content 模式单行内容超 200 字符截断加 "..."
  结果按 (路径, 行号) 排序保证确定性
```

### 3.3 遍历与过滤规则（grep）

- `path` 指向文件：直接搜索该文件（同样走受保护/二进制/gitignore 检查）。
- 目录遍历：不跟随 symlink（防环）。
- 跳过目录：`.git`、受保护路径、gitignore 忽略的路径。
- 跳过文件：受保护路径、二进制（读前 8192 字节含 `\x00` 即跳过）。
- `include`：对文件名做 fnmatch，不匹配则跳过。

## 4. gitignore 基础支持

实现 `_parse_gitignore(lines)` 与目录树累积匹配。逐目录读取 `.gitignore`，
规则沿路径从根向下累积（父目录规则 + 本目录规则），后加规则优先（最后匹配生效）。

支持：
- `#` 注释、空白行 → 跳过
- `!pattern` → 取反（豁免此前忽略）
- `/pattern` → 锚定该 .gitignore 所在目录
- `pattern/` → 仅目录
- `pattern` → 文件/目录 glob（fnmatch 匹配，`*` 不跨 `/`，与 git 行为一致）

不支持（标注限制）：`**` 特殊模式、反斜杠转义的完整集。

## 5. 错误处理

- 参数缺失/非法 → `ToolResult(ok=False, output="...")`（不抛异常）。
- 搜索根不存在 → `ok=False` 并说明。
- 正则编译失败（非法 pattern）→ `ok=False` 并返回错误信息。
- 读取文件 I/O 错误 → 跳过该文件（不中断整体搜索），不回退为失败。
- 极端情况：空目录、无权限目录 → 正常返回空/部分结果。

## 6. 与现有代码的衔接

- 复用：`ToolResult` / `truncate` / `_is_protected_path` / `_resolve` / `_schema` / `MAX_OUTPUT_CHARS`。
- 新增常量：`MAX_SEARCH_RESULTS = 500`、`MAX_GREP_LINE_CHARS = 200`。
- 注册：`TOOL_SCHEMAS` 追加 2 个 schema；`_HANDLERS` 追加 `glob`/`grep`。
- `agent.py` 的 `SYSTEM_PROMPT` 工具列表追加 glob/grep 一行说明。
- 不改变现有 5 工具行为与接口。

## 7. 测试计划（test_tools.py 新增）

**glob**
1. `*.py` 匹配
2. `**` 递归匹配
3. `path` 子目录搜索
4. 受保护路径排除（`.git`/`.env`）
5. 无匹配返回说明
6. 结果数截断

**grep**
7. content 格式与行号正确
8. files_with_matches 只列文件
9. count 统计命中数
10. ignore_case 生效
11. include 过滤
12. 多文件多命中排序确定性
13. 无匹配返回 "(no matches)"
14. 二进制文件跳过
15. 受保护路径跳过
16. gitignore：普通规则 / 注释 / 取反 / 目录规则 / 锚定根
17. 单行超长截断
18. 结果数截断
19. 非法正则返回 ok=false
20. path 为单文件直接搜

全部离线，不依赖真实 API / 网络。

## 8. 文档同步

- `docs/tools.md`：§3 追加 glob/grep 定义（权威源），更新总则与未来扩展。
- `docs/design.md`：§6 功能范围勾选 glob/grep；§8 开发路线追加步骤。
- `docs/architecture.md`：tools 模块职责描述追加搜索工具。
- `docs/development.md`：测试清单提及新用例数（可选，保持一致）。
- `.agent/03-decisions.md`：新增 ADR-011（glob/grep 纯 stdlib 实现 + gitignore 基础支持）。

## 9. 开发顺序（小步推进，每步可验证）

1. 写失败测试（test_tools.py 新增用例）→ 运行确认失败
2. tools.py 实现 glob + grep + gitignore 解析
3. `uv run pytest tests/ -v` 全绿
4. 更新 SYSTEM_PROMPT
5. 同步文档（tools.md / design.md / architecture.md）+ 追加 ADR-011
6. 真实 API 冒烟一次（demo 任务中使用 glob/grep）
7. 凭据 grep 复核 + 提交（保留完整历史）
