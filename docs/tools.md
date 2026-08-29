# 工具定义与约定

> 本文档是工具的权威定义源（JSON Schema 的书写依据），随实现逐步完善。

## 1. 总则

- 所有工具均为**本地执行**，不依赖任何服务端托管能力。当前工具数量共 7 个（read_file / write_file / edit_file / list_dir / run_command / glob / grep）。
- 工具通过 OpenAI 原生 tool calling 接口暴露给模型。
- 所有工具返回统一格式 `ToolResult`（纯文本，便于回填对话历史）。

## 2. 统一返回格式

```json
{
  "ok": true,
  "output": "文本结果",
  "truncated": false,
  "exit_code": 0
}
```

- `ok`：工具是否成功执行。
- `output`：文本输出，超长自动截断（默认上限见下）。
- `truncated`：是否被截断。
- `exit_code`：仅命令类工具使用。

## 3. 核心工具清单

### 3.1 read_file
- 用途：读取文本文件内容。
- 参数：
  - `path`（string，必填）：文件路径（相对工作目录）。
  - `offset`（int，可选）：起始行号（1 起）。
  - `limit`（int，可选）：读取行数。
- 返回：文件内容（带行号可选）；文件不存在/不可读时 `ok=false` 并说明原因。
- 输出：内容整体读取后按输出长度上限（见 §4，默认 8000 字符）以"头+尾"方式截断并插入截断标记；`offset`/`limit` 提供行范围读取。

### 3.2 write_file
- 用途：创建新文件或整体覆盖已有文件。
- 参数：
  - `path`（string，必填）。
  - `content`（string，必填）。
- 返回：写入结果；父目录不存在时自动创建。
- 注意：覆盖是原子的（先写临时文件再 rename），避免写一半。

### 3.3 edit_file
- 用途：精确替换文件中的一段内容。
- 参数：
  - `path`（string，必填）。
  - `old_string`（string，必填）：待替换原文（必须唯一匹配）。
  - `new_string`（string，必填）：替换后的内容。
- 返回：替换结果；`old_string` 不存在或非唯一匹配时 `ok=false`，并回读上下文提示模型修正。
- 安全性：替换前后做校验，避免破坏文件。

### 3.4 list_dir
- 用途：列出目录内容。
- 参数：
  - `path`（string，可选，默认 `.`）。
- 返回：条目列表（名称 + 类型 + 大小）。跳过常见噪音（如 `.git` 内部、超大目录），防止输出爆炸。

### 3.5 run_command
- 用途：在工作目录下执行 shell 命令。
- 参数：
  - `command`（string，必填）。
- 返回：stdout、stderr、exit_code。
- 保护：
  - 默认超时（如 120s），超时终止并返回提示。
  - 输出长度上限，超长截断并标记 `truncated`。
  - 默认在项目工作目录内执行。

### 3.6 glob
- 用途：按 glob 通配符查找文件（支持 `**` 递归）。
- 参数：
  - `pattern`（string，必填）：glob 模式，如 `**/*.py`。
  - `path`（string，可选，默认 workdir）：搜索起点目录。
- 返回：匹配的【文件】路径列表（相对 workdir），排序确定性；只返回文件。
- 保护：跳过受保护路径（`.git`/`.env*` 除 `.env.example`）；结果上限 `MAX_SEARCH_RESULTS=500`，超出截断并标记 `truncated`。

### 3.7 grep
- 用途：在文件中做正则搜索。
- 参数：
  - `pattern`（string，必填）：正则表达式。
  - `path`（string，可选，默认 workdir）：文件或目录。
  - `include`（string，可选）：对文件名做 fnmatch 过滤（如 `*.py`）。
  - `ignore_case`（boolean，可选，默认 false）。
  - `output_mode`（string，可选，默认 `content`）：`content` / `files_with_matches` / `count`。
- 返回：
  - `content`：`相对workdir路径:行号:行内容`；单行超 200 字符截断。
  - `files_with_matches`：每行一个相对路径。
  - `count`：`相对路径:命中数`。
- 遍历：不跟随 symlink；跳过 `.git`、受保护路径、二进制文件（前 8192 字节含 NUL）、gitignore 忽略项。
- 限制：结果上限 500 条，超出截断并标记 `truncated`；无匹配返回 `(no matches)`。

#### 3.7.1 gitignore 基础支持
- 逐目录读取 `.gitignore`，规则沿路径从根向下累积，后加规则优先（最后匹配生效）。
- 支持：`#` 注释/空白行跳过；`!` 取反；`/` 锚定目录根；`dir/` 仅目录；普通 glob（`*` 不跨 `/`）。
- 不支持（本期限制）：`**` 特殊模式、反斜杠转义完整集。

## 4. 输出长度与安全约定

- 单次工具输出文本上限：默认 8000 字符（可配置），超出部分以"头+尾"方式截断并插入截断标记。
- 命令类工具禁止交互式等待（无 TTY），避免阻塞循环。
- 禁止执行破坏性高危操作策略由 system prompt 约束，不在此层强制（遵循题目允许范围）。

## 5. 未来扩展（暂不实现）

- 搜索性能：必要时可在工具内部将 grep 引擎替换为 ripgrep（封装不变，对外 schema 不动）。
- 并行工具调用：模型一次返回多个 tool_calls 时，串行→并行（按依赖性）。

## 6. 变更流程

- 修改本文件 → 同步更新 `tools.py` 中 JSON Schema → 更新 `tests` 中的用例。
