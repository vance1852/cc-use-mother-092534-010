# 建设节日坚守人员贡献核验与荣誉公示后台

本项目提供节日公共服务场景的后台系统，包含两层：

1. **通用基础层**：组织、服务站点、操作者与结构化参考资料登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。
2. **贡献核验与荣誉公示层**：接收带来源的值守事实、协作单位证明、推荐理由与候选人可公开范围；按冻结的评选规则合并同一事件而不丢失各方贡献；先做推荐资格与回避检查，再分派有期限核验人；核验可采信、要求补证或排除事实；最终入选由另一名授权人员确认；公示只输出获准字段。

## 核心规则

- **规则冻结**：规则以版本发布，批次创建时绑定版本；开放批次可显式刷新到新版本，冻结/终局后版本随决定锁定——规则变化只影响未完成批次。
- **同一事件合并**：同一候选人 + 同一值守日期 + 同一场所归并为一条候选记录；各部门、各来源的事实与推荐理由作为独立贡献全部保留。
- **资格与回避先行**：推荐前候选人必须有生效中的公开同意；核验人、确认人与候选人、任一推荐人（及核验人）存在本人关系或已申报回避关系时一律拦截。任何人都不能批准自己的推荐。
- **有期限核验**：分派带截止时间；可逐条采信 / 要求补证 / 排除（排除须填理由）；及时补证顺延期限，逾期补证标记为迟到、留痕但不改变终局。
- **终局确认**：所有事实处理完毕、且采信的**独立来源单位数**达到规则下限，另一名无关联授权人员方可确认入选。
- **撤回同意**：候选人撤回公开同意后不出现在新公示中，公开接口实时过滤；但候选记录、终局决定与公示快照在内部完整留痕。
- **稳定结果**：所有写操作请求幂等；重复提交去重；并发核验通过事务串行化、条件更新与唯一索引保证只有一个结论生效。

## 目录

- `src/festival_foundation/`
  - 基础层：`models.py`、`storage.py`、`service.py`、`audit.py`、`clock.py`、`api.py`、`acceptance.py`
  - 荣誉层：`honor_rules.py`（冻结规则/合并键/公开裁剪）、`honor_storage.py`（领域表）、
    `honor_service.py`（工作流）、`honor_api.py`（HTTP 边界）、`honor_acceptance.py`（离线验收）
- `tests/`：基础规则、荣誉工作流、HTTP 路由、并发稳定性与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
# 基础层
PYTHONPATH=src python3 -m festival_foundation.acceptance
# 荣誉核验与公示层
PYTHONPATH=src python3 -m festival_foundation.honor_acceptance
```

验收命令在临时 SQLite 数据库中跑通完整链路，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。荣誉验收覆盖：规则冻结、回避拦截、多方合并、重复去重、有期限核验、补证与迟到补证、他人终局确认、规则版本锁定、撤回同意后公开过滤而内部留痕。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.honor_api --database honor.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入与内部接口通过 `X-Actor-Id` 标识操作者。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/honor/rules` | 发布新版本评选规则（admin） |
| POST | `/conflicts` | 申报回避关系（admin） |
| POST | `/honor/batches` | 创建批次，绑定当前规则版本（admin） |
| POST | `/honor/batches/refresh-rules` | 开放批次刷新到最新规则（admin） |
| POST | `/consents`、`/consents/revoke` | 授予 / 撤回候选人公开同意 |
| POST | `/nominations` | 提交带来源事实的推荐；同一事件自动合并 |
| POST | `/verifications/assign` | 资格与回避检查后分派有期限核验人（admin） |
| POST | `/facts/decide` | 核验人采信 / 要求补证 / 排除单条事实（reviewer） |
| POST | `/supplements` | 提交补证；迟到自动标记 |
| POST | `/confirmations` | 另一名授权人员终局确认入选（admin，禁止本人推荐） |
| POST | `/honor/batches/finalize`、`/publish` | 冻结批次并生成公示快照 |
| GET | `/public/batches`、`/public/honors?batch_id=` | **公开接口**，免登录，只输出获准字段 |
| GET | `/internal/batches`、`/internal/nominations` | **内部查询**（admin/auditor），还原合并、回避、补证与终局完整依据 |

所有写接口接受 `request_id` 实现幂等；同一 `request_id` 重放返回首次结果，内容不同则返回冲突。
