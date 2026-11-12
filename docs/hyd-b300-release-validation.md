# HYD B300 0.2.0 离线交付验收

## 结果

- 验收时的基线提交：`3b37123945f5f9e3b5ed9c99a9efa957a4dbf034`。这次 0.2.0 实现随后单独提交。
- 包版本与已安装 CLI 版本：`0.2.0`。
- 全量离线回归：**213 tests，0 failures，0 errors，0 skipped**；耗时以本地复跑输出为准。
- 4 个示例配置均为 disabled，并通过本地校验。
- OpenSpec `add-hyd-b300-reliable-spot` 严格校验通过。
- `git diff --check` 通过；Python 模块入口、安装后的 `h100-spot-controller` 入口和 `--state-region` help 均验证。

复现入口：[release-check](<../scripts/release-check.py>)。

```sh
.venv/bin/python scripts/release-check.py --openspec --quiet
.venv/bin/h100-spot-controller validate-target config/hyd-b300.example.yaml
```

OpenSpec 参数要求 CLI 在 PATH；不加 `--openspec` 时仍运行完整 Python/CDK 回归。CDK 合成需要受支持的本地 Node.js。

## 安全边界验证

1. HYD 配置固定 ap-south-2 / p6-b300.48xlarge，拒绝其他 Region、类型、Local Zone、EKS、自动换区、surge 和可变 Launch Template 别名。
2. B300/8 GPU、Spot usage class、AZ offering、subnet/SG VPC、launch contract、P Spot 配额与定价预检；缺失/不支持/读取失败分类，未把 eligibility 或总 quota 说成库存/空闲 headroom。
3. 云请求前持久化冻结 intent；云成功后状态失败、响应丢失、不可见结果、并发调和、请求参数变化、过期重放均未产生第二个请求身份。
4. 返回 FleetId 持久化；旧 Fleet 仅在权威 terminal 证据与实例归零后允许新 generation；未知结果不自动更换 token。
5. 所有非 terminated 残留状态、重复 Fleet、活跃 Fleet 加额外 orphan 超量，均阻止受影响写操作，不隐式终止。
6. 多次模拟 interruption 后仍复用同一 maintain Fleet；单 Region 长时间短缺不进入跨区计划。此项是 fake 容量状态回归，不是 AWS 实机中断测试。
7. 显式客户 cap 与 On-Demand ceiling 取 min；元数据 hydrate 不改变操作员配置指纹。
8. customer A/B 的名称、state、runtime customer/target 绑定、IAM 标签/精确 Launch Template/PassRole、单写者并发、Lambda asset 排除均通过合成与策略测试。
9. 状态表 Region 显式可选，HYD 文件型命令固定 ap-south-2；配置更新与 cleanup 使用 writer claim，容量写者取得 claim 后重新校验持久配置版本；预检期间发生 disable 或配置文件在 CLI 校验期间改变均不能越过审批创建。cleanup 拒绝 persisted target 仍 enabled、未知创建结果和不可见旧 Fleet。
10. 中断事件验证实际 Region 和 ownership；事件 Lambda 不创建容量。容量 API 响应不明时日志暴露 uncertain，不伪称未写。

## 未执行，不能据此宣称成功

| 项目 | 状态 |
|---|---|
| 客户账户 HYD B300 Spot 支持 / offering / quota / IAM 只读检查 | NOT RUN |
| AWS 控制平面部署、目标持久化或启用 | NOT RUN |
| 真实 B300 Spot 获配 | NOT RUN |
| B300 驱动、CUDA、GPU/业务就绪 | NOT RUN |
| AWS 真实回收后的再次获配与任务恢复 | NOT RUN |
| GitHub commit / push | NOT RUN |

测试入口使用 fake credentials、关闭 IMDS 凭据发现、禁用本机 AWS config/credentials 文件并阻断所有 botocore HTTP 请求；CDK jsii 的本地 Node 通信保留。回归不靠客户凭据，不调用 AWS 账户 API。

## 保留与清理

保留源代码、OpenSpec 计划、[客户手册](<hyd-b300-quickstart.md>)、[disabled 示例](<../config/hyd-b300.example.yaml>)、验收记录和正常开发依赖环境。批准安装的全局 OpenSpec CLI 不属于测试清理对象。

CDK/fixture 使用任务拥有的 TemporaryDirectory 并 teardown；源树不保留 cdk.out、测试数据库、复制的依赖或私有客户输入。交付源码包不包含 .git、.venv、.agents、privateinputs、testoutputs、缓存或实际客户配置。
