# 建设节日坚守人员贡献核验与荣誉公示后台

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

`festival_honor` 包在基础层之上实现节日坚守人员贡献核验与荣誉公示：多家单位带来源的值守事实按事件指纹合并（不丢失各方贡献）、候选人可公开范围登记、推荐资格与利益回避检查、有期限的核验分派、采信/补证/排除结论、另一名授权人员终局确认、规则版本冻结（变化只作用于未完成批次）、公开同意撤回（不入新公示但内部决定留痕），以及公开投影与内部完整依据两类查询。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/festival_honor/`：规则冻结、批次、事实合并、回避核验、补证、终审、公示投影与完整依据查询；
- `tests/`：基础规则、事务边界、接口路由、合并/回避/补证/终局/公示和端到端验收测试。

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
PYTHONPATH=src python3 -m festival_foundation.acceptance
PYTHONPATH=src python3 -m festival_honor.acceptance
```

验收命令会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。荣誉验收额外核对多单位事实合并、回避拦截、限期补证、他人终审、敏感字段裁剪以及撤回同意后新公示排除而内部留痕。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m festival_honor.api --database festival_honor.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

荣誉后台的主要接口（均为 POST，除查询外）：

- `/rules`：冻结评选规则版本；`/batches` 开批次并固化规则快照，`/batches/rebind-rule` 仅允许未完成且无终局决定的批次改绑新规则，`/batches/close` 结束批次；
- `/conflicts`：登记操作者之间的直接利益关系；`/consents/grant`、`/consents/revoke`：候选人公开同意台账；
- `/facts`：提交带来源值守事实（含协作单位证明、推荐理由、公开范围、敏感标记），同人同场所同时段自动合并为一个事件，同来源重复提交稳定回放；
- `/assignments`：分派有期限核验人（先做资格与回避检查），`/assignments/cancel`、`/assignments/complete`；
- `/fact-decisions`：核验人作出 `accepted`/`excluded`/`supplement` 结论；`/supplements` 限期补证，迟到证明标记 `late_rejected` 并排除事实；
- `/final-decisions`：核验完成后由另一名授权人员终局确认，推荐人本人与原核验人均被回避；
- `/publications`：POST 发布公示，GET 为公开接口，只输出候选人获准且非敏感的字段；
- `GET /internal/events?event_id=...`：仅管理员/审计员可查，还原合并来源、回避依据、补证过程与终局决定的完整留痕。
