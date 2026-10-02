# Coding Task: 恢复 RelayForge 生产级 Webhook 平台

## 背景

RelayForge 是支付、账单和订阅系统共用的多租户 Webhook 平台。它使用 Python
3.11 与 SQLite，支持多个独立 worker 进程共享数据库，完成事件接收、订阅路由、
公平调度、带 fencing 的租约、签名投递、重试、死信、重放和审计。管理面同时提供
WSGI API 与 `relayforge-admin` CLI。

近期一次恢复演练发现：仓库回退到一个“能够启动、部分测试可过，但不能安全上线”的
版本。演练期间出现过准入策略与实际投递脱节、旧 worker 写回、熔断恢复流量异常、
跨租户管理请求、修复任务误改变化中的数据、备份不可恢复，以及监控输出携带高基数或
敏感字段等现象。单项功能在顺序执行时往往正常，问题主要出现在多连接竞争、精确时间
边界、故障恢复和跨模块组合中。

当前 Workspace 是完整的离线工程复现。所有 fixtures 都是明确标记的全合成数据，结构
模仿生产导出，但不包含真实客户数据或生产凭据。请把它修复到可以通过发布门禁的状态。
你需要自行阅读代码、契约、事故材料和测试，建立故障模型并决定实现方式；不要把任务
简化成只满足当前可见断言。

## 已提供材料

- `src/relayforge/`：基础数据面、企业控制面和生产运维扩展代码。
- `docs/architecture.md`、`docs/persistence-contract.md`、
  `docs/retry-contract.md`、`docs/http-contract.md`、`docs/cli-contract.md`：基础投递契约。
- `docs/control-plane-architecture.md`、`docs/subscriptions-contract.md`、
  `docs/admission-contract.md`、`docs/operations-contract.md`、
  `docs/management-contract.md`：企业控制面契约。
- `docs/production-architecture.md`、`docs/production-dispatch-contract.md`、
  `docs/recovery-contract.md`、`docs/security-observability-contract.md`：生产运行、恢复、
  安全与可观测性契约。
- `docs/incident-2026-09.md` 与 `docs/incident-2026-10-control-plane.md`：脱敏事故时间线。
- `fixtures/legacy_v0.sql`、`fixtures/control_v1.sql`：历史 schema 快照。
- `fixtures/incident_snapshot/`、`fixtures/control_incident/`：可关联的 synthetic incident
  导出。
- `fixtures/production_incident/`：认证、worker drain、dispatch admission、repair 与 backup
  的可关联 synthetic production exercise 导出。
- `tests/`：公开的单元、集成、并发、迁移、恢复和安全回归测试。
- `scripts/`：跨平台检查和 synthetic demo 初始化脚本。

契约文档定义外部行为，事故材料用于调查背景。若 README 示例与契约冲突，以契约为准；
若你认为公开测试与契约冲突，请在交付说明中提供可复核证据，不能静默改变契约或删改测试。

## 任务目标

### 1. 恢复端到端投递正确性

系统必须把公平选择、原子 claim、租约续期、租户限流、租户/端点容量、端点熔断、
secret version、v2 签名、HTTP 结果分类、重试和最终状态串成一致的投递生命周期。
准入拒绝不能产生虚假的 HTTP attempt；实际请求结果必须正确反馈给 delivery、attempt、
dead letter、admission 和 circuit。过期或被替换的 lease/probe/worker generation 不能覆盖
新所有者。

运行时应支持有界线程并发、worker heartbeat、长请求续租和优雅 drain。收到停机请求后
停止领取新任务，允许活跃工作在 deadline 内完成；未完成工作依靠持久化租约恢复。多个
SQLite connection 同时工作时，不能超发 token/capacity、产生多个 half-open probe 或
形成两个有效 delivery owner。

### 2. 恢复多租户管理面安全

管理 API 需要 bearer credential、scope 与资源 tenant 三者同时授权，并保证 tenant 凭证
不能通过省略 tenant 获得全局访问。凭证只能在签发时返回原文，数据库不得保存可复用
token。撤销必须立即生效。

统一处理请求 ID、body 大小、JSON 类型、资源不存在和状态冲突，保持稳定的
400/401/403/404/409/500 语义。授权中读取 body 后，下游仍应读取到相同内容。liveness
与 readiness 是独立探针：前者只判断进程和只读数据库路径，后者覆盖写路径、schema、
迁移和 worker 状态。API 与 CLI 必须复用相同服务语义。

### 3. 恢复可审计的一致性检查与修复

提供只扫描不修改业务状态的一致性检查，覆盖 delivery/attempt/dead-letter、replay、
subscription tenant、secret lifecycle、rate bucket、circuit probe 和 dispatch admission
等关键不变量。结果需要稳定排序、规范序列化、确定性 fingerprint，并持久化运行记录。

自动修复必须经过“scan -> plan -> apply”工作流。plan 快照来源状态和动作；apply 前重新
验证来源，目标发生变化时整个计划失效，不允许部分套用旧结论。能够自动修复的动作必须
带版本或等价 fencing，并在一个事务中全部成功或全部回滚；语义不明确的异常只报告，
不能擅自猜测。

### 4. 恢复可验证的在线备份与安全恢复

在线备份必须包含一致的 SQLite/WAL 状态，在发布前完成关闭/flush、integrity check、
foreign-key check、schema version、SHA-256 与大小校验，并使用同目录原子发布。manifest
只能包含恢复所需元数据，不能暴露数据库源路径和 secret。

恢复必须先验证 manifest 与源文件，写入全新目标并再次检查；已有目标一律拒绝覆盖。
任何失败都不能留下看似有效的最终文件或散落的临时文件。实现必须同时适用于 Windows
和 Linux。

### 5. 恢复有界可观测性和信息安全

提供固定 label 集合、硬性 series 上限、单调 counter 和固定 bucket histogram，覆盖投递
结果、重试、熔断、队列年龄、投递耗时和 SQLite contention。tenant、endpoint、event、
delivery、request、credential、worker 等实体 ID 不得成为无界 metric label。

结构化日志、audit、diagnostics、health、metrics、API、CLI、manifest 和异常响应必须共享
一致的敏感信息边界。不得出现 payload、credential/secret、signature、lease/probe/
generation token、cursor secret、URL userinfo/query 或可能含凭据的异常文本。保留足够的
稳定类别和 correlation ID 以支持排障。

### 6. 保持现有兼容性与历史数据

原有事件幂等、fan-out snapshot、公平调度、retry、dead-letter、replay、retention、audit
cursor、subscription/filter、secret rotation、delivery v0 migration 和 control v1 migration
行为必须继续满足契约。初始化可重复执行，迁移失败完整回滚，`foreign_key_check` 为空。
除契约明确禁止泄漏的字段外，不得破坏现有同步 API、命令、表结构和 JSON 字段。

## 工程约束

- 运行时代码只使用 Python 3.11+ 标准库和 SQLite 3.35+。
- 不引入外部数据库、消息队列、缓存、Web 框架或真实网络服务。
- 测试不得访问真实网络；使用真实临时 SQLite 文件和独立 connection 验证事务竞争。
- 不关闭 foreign keys，不降低 durability，不用进程内锁代替跨进程持久化协调。
- 所有持久化时间使用 UTC `Z` 格式并移除微秒，精确边界按契约处理。
- 不得删除、skip、放宽、改写或按环境绕过测试。
- 不得针对 fixture ID、测试名、固定时间或固定签名硬编码答案。
- 不得留下 `TODO`、空实现、`pass` stub 或 `NotImplementedError`。
- 保持 Windows/Linux 兼容，不依赖 `fork`、`/tmp`、POSIX signal 或平台 skip。
- 可以重构内部实现，但应保持清晰的事务所有权，避免复制业务规则形成第二套语义。

## 交付物

直接修改当前 Workspace，最终提交：

1. 可运行的完整生产代码与必要 schema 变更；
2. 针对边界、竞态、恢复和泄漏风险新增的自动化测试，保留全部现有测试；
3. 必要的 fixture 与契约说明更新，fixtures 必须继续标明 synthetic；
4. 根目录 `IMPLEMENTATION_NOTES.md`，至少说明确认的根因、跨模块影响、事务与 fencing
   设计、恢复/回滚设计、新增测试证据，以及仍存在的 at-least-once/SQLite 运维边界。

不要只交分析、计划、补丁片段或截图。最终 Workspace 必须可直接安装、编译和运行验收。

## 公共验收

在仓库根目录执行：

```powershell
$env:PYTHONPATH = (Resolve-Path "src")
python -m compileall -q src tests
python -m unittest discover -s tests -v
```

公开测试全部通过只是最低门槛。评审会使用不包含在 Workspace 中的独立测试检查：多线程/
多 connection 竞争、精确过期边界、stale response fencing、tenant IDOR、撤销即时性、
scan determinism、stale repair、事务回滚、备份篡改、restore refusal、metric cardinality、
secret scanning、schema 兼容和 Windows/Linux 可移植性。

以下任一情况属于阻断缺陷：

- 两个有效 owner、token/capacity 超发或多个 half-open probe；
- 旧 lease/probe/generation 能写回，或准入拒绝仍发送网络请求；
- repair/replay/migration/retention 发生部分提交、历史丢失或跨租户影响；
- restore 覆盖现有文件，或未校验的备份被发布/恢复；
- 任一用户可见或运维输出泄漏敏感信息；
- 通过修改测试、硬编码 fixture 或改变契约制造绿灯。

## 提交前自检

- 从干净环境运行 compileall、全部公开测试和 synthetic seed。
- 用多个独立 SQLite connection 验证 claim、rate、circuit、admission、replay 和 repair 竞争。
- 检查 lease/cooldown/overlap/cursor/worker/retention 的精确边界。
- 对正常备份、篡改备份、损坏数据库、已存在目标和中途失败分别验证清理行为。
- 扫描 stdout、stderr、API、audit、diagnostics、health、metrics、manifest 和 notes 中的 secret。
- 阅读最终 diff，确认没有缓存、数据库、临时文件、测试绕过或参考答案内容。
